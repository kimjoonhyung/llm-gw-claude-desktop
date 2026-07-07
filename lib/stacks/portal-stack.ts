import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as targets from 'aws-cdk-lib/aws-elasticloadbalancingv2-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import { PROJECT_NAME, BUDGET, ModelIds } from '../config/constants';

export interface PortalStackProps {
  /** Okta OIDC 연동 정보. 비어 있으면 Cognito 자체 사용자 풀로 동작 (테스트용) */
  oktaIssuer: string;
  oktaClientId: string;
  oktaClientSecret: string;
  models: ModelIds;
  // Lambda를 VPC 안에 두어, ALB가 CIDR로 제한돼도 NAT EIP를 통해 LiteLLM 호출 가능
  vpc: ec2.IVpc;
  lambdaSg: ec2.ISecurityGroup;
  /**
   * 포털을 노출할 ALB 리스너와 base URL.
   * 공개 Function URL(Principal:* 정책)은 보안 스캐너에 차단되므로
   * ALB 경로 라우팅(/portal)으로 노출한다 — allowedCidrs 제한도 함께 적용됨.
   */
  albListener: elbv2.IApplicationListener;
  gatewayUrl: string;
}

/**
 * Self-Service Key Portal
 *
 * 일반 사용자(Claude Desktop/Code)가 AWS CLI 없이 브라우저에서
 * Okta 로그인만으로 LiteLLM Virtual Key를 발급받는 포털.
 *
 * 흐름:
 *   사용자 브라우저 → 포털 URL → Cognito Hosted UI → Okta(OIDC) 로그인
 *     → 포털 Lambda가 인증 코드를 토큰으로 교환 (Cognito가 AWS 측 인증을 담당)
 *     → 이메일 기반으로 LiteLLM Virtual Key 조회/자동생성 (기존 apiKeyHelper 로직의 클라우드 버전)
 *     → 인증된 세션 화면에 Virtual Key + 설정 가이드 표시 (이메일 발송 불필요)
 */
export class PortalStack extends cdk.NestedStack {
  public readonly portalFunction: lambda.Function;
  public readonly portalUrl: string;
  public readonly userPool: cognito.UserPool;
  /** Okta Event Hook 수신 Lambda (자동 오프보딩) */
  public readonly oktaEventsFunction: lambda.Function;
  public readonly oktaEventsWebhookSecret: secretsmanager.Secret;

  constructor(scope: Construct, id: string, props: PortalStackProps) {
    super(scope, id);

    // --- Cognito User Pool ---
    this.userPool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: `${PROJECT_NAME}-portal`,
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      standardAttributes: {
        email: { required: true, mutable: true },
      },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Hosted UI 도메인 (전역 고유 프리픽스 필요)
    const domainPrefix = `${PROJECT_NAME}-${this.account}`;
    this.userPool.addDomain('Domain', {
      cognitoDomain: { domainPrefix },
    });
    const cognitoDomainUrl = `https://${domainPrefix}.auth.${this.region}.amazoncognito.com`;

    // --- Okta OIDC IdP (설정된 경우에만) ---
    const useOkta = !!(props.oktaIssuer && props.oktaClientId && props.oktaClientSecret);
    let oktaProvider: cognito.UserPoolIdentityProviderOidc | undefined;
    if (useOkta) {
      oktaProvider = new cognito.UserPoolIdentityProviderOidc(this, 'OktaIdp', {
        userPool: this.userPool,
        name: 'Okta',
        issuerUrl: props.oktaIssuer,
        clientId: props.oktaClientId,
        clientSecret: props.oktaClientSecret,
        scopes: ['openid', 'email', 'profile'],
        attributeMapping: {
          email: cognito.ProviderAttribute.other('email'),
          fullname: cognito.ProviderAttribute.other('name'),
        },
      });
    } else {
      cdk.Annotations.of(this).addWarning(
        'Okta 연동 정보(oktaIssuer/oktaClientId/oktaClientSecret)가 없어 Cognito 자체 사용자 풀로 배포됩니다. ' +
        '프로덕션에서는 -c oktaIssuer=... -c oktaClientId=... -c oktaClientSecret=... 을 지정하세요.',
      );
    }

    // --- Portal Lambda ---
    this.portalFunction = new lambda.Function(this, 'PortalFn', {
      functionName: `${PROJECT_NAME}-key-portal`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset('lambda/key-portal'),
      memorySize: 256,
      timeout: cdk.Duration.seconds(15),
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [props.lambdaSg],
      logGroup: new logs.LogGroup(this, 'PortalLogGroup', {
        logGroupName: `/aws/lambda/${PROJECT_NAME}-key-portal`,
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        DEFAULT_MAX_BUDGET_USD: String(BUDGET.MONTHLY_LIMIT_USD),
        BUDGET_DURATION: BUDGET.BUDGET_DURATION,
        DEFAULT_MODEL: props.models.opus,
        MODEL_OPUS: props.models.opus,
        MODEL_SONNET: props.models.sonnet,
        MODEL_HAIKU: props.models.haiku,
        // Claude Desktop bootstrap (/portal/bootstrap) — 둘 다 있어야 활성화
        OKTA_ISSUER: props.oktaIssuer,
        DESKTOP_OIDC_CLIENT_ID: this.node.tryGetContext('desktopOidcClientId') || '',
      },
    });

    // ALB 경로 라우팅으로 노출: /portal*
    // (공개 Function URL은 Principal:* 리소스 정책이 필요해 보안 스캐너가 차단함.
    //  ALB target 방식은 ALB만 invoke 권한을 가지며, allowedCidrs 제한도 함께 적용된다.)
    // 주의: listener.addTargets()를 쓰면 규칙이 Gateway 스택에 생겨
    // Gateway <-> Portal 순환 참조가 발생한다. 타겟그룹/규칙 모두 이 스택에 생성.
    const portalUrl = `${props.gatewayUrl}/portal`;
    const portalTargetGroup = new elbv2.ApplicationTargetGroup(this, 'PortalTg', {
      targetType: elbv2.TargetType.LAMBDA,
      targets: [new targets.LambdaTarget(this.portalFunction)],
    });
    new elbv2.ApplicationListenerRule(this, 'PortalRule', {
      listener: props.albListener as elbv2.ApplicationListener,
      priority: 10,
      conditions: [elbv2.ListenerCondition.pathPatterns(['/portal', '/portal/*'])],
      action: elbv2.ListenerAction.forward([portalTargetGroup]),
    });
    this.portalUrl = portalUrl;

    // --- Cognito App Client (포털용, Authorization Code Flow) ---
    // 주의: Lambda 환경변수에 client ID/secret을 직접 넣으면
    // Lambda -> Client -> Function URL(callback) -> Lambda 순환 참조가 생긴다.
    // Lambda가 런타임에 Cognito API로 client 설정을 조회하도록 하여 사이클을 끊는다.
    const clientName = `${PROJECT_NAME}-portal-client`;
    const appClient = this.userPool.addClient('PortalClient', {
      userPoolClientName: clientName,
      generateSecret: true,
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
        callbackUrls: [portalUrl],
        logoutUrls: [portalUrl],
      },
      supportedIdentityProviders: useOkta
        ? [cognito.UserPoolClientIdentityProvider.custom('Okta')]
        : [cognito.UserPoolClientIdentityProvider.COGNITO],
      preventUserExistenceErrors: true,
    });
    if (oktaProvider) {
      appClient.node.addDependency(oktaProvider);
    }

    // Lambda 환경변수 (순환 참조가 없는 값만)
    this.portalFunction.addEnvironment('COGNITO_DOMAIN', cognitoDomainUrl);
    this.portalFunction.addEnvironment('USER_POOL_ID', this.userPool.userPoolId);
    this.portalFunction.addEnvironment('COGNITO_CLIENT_NAME', clientName);
    this.portalFunction.addEnvironment('IDP_NAME', useOkta ? 'Okta' : '');

    // 런타임 client ID/secret 조회 권한
    this.portalFunction.addToRolePolicy(new iam.PolicyStatement({
      sid: 'CognitoClientLookup',
      actions: [
        'cognito-idp:ListUserPoolClients',
        'cognito-idp:DescribeUserPoolClient',
      ],
      resources: [this.userPool.userPoolArn],
    }));

    // --- Okta Event Hook: 자동 오프보딩 (SCIM deprovisioning 대체) ---
    // Okta가 인터넷에서 호출해야 하므로 CIDR 제한된 ALB가 아닌 API Gateway로 노출.
    // 인증: Okta가 보내는 Authorization 헤더를 전용 웹훅 시크릿과 비교.
    this.oktaEventsWebhookSecret = new secretsmanager.Secret(this, 'OktaWebhookSecret', {
      secretName: `${PROJECT_NAME}/okta-webhook-secret`,
      description: 'Okta Event Hook authentication secret',
      generateSecretString: {
        passwordLength: 40,
        excludePunctuation: true,
        includeSpace: false,
      },
    });

    this.oktaEventsFunction = new lambda.Function(this, 'OktaEventsFn', {
      functionName: `${PROJECT_NAME}-okta-events`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset('lambda/okta-events'),
      memorySize: 256,
      timeout: cdk.Duration.seconds(15),
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [props.lambdaSg],
      logGroup: new logs.LogGroup(this, 'OktaEventsLogGroup', {
        logGroupName: `/aws/lambda/${PROJECT_NAME}-okta-events`,
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
      environment: {
        WEBHOOK_SECRET_ARN: this.oktaEventsWebhookSecret.secretArn,
        // 앱 어사인 해제 이벤트 필터링용 (Okta 앱의 display name과 일치해야 함)
        OKTA_APP_LABEL: 'LLM Gateway Key Portal',
        // 그룹 제거 이벤트 필터링용 (쉼표 구분 복수 가능, -c oktaGroupLabel=... 로 지정)
        OKTA_GROUP_LABEL: this.node.tryGetContext('oktaGroupLabel') || 'llm-gateway-users',
      },
    });
    this.oktaEventsWebhookSecret.grantRead(this.oktaEventsFunction);

    const eventsApi = new apigateway.RestApi(this, 'OktaEventsApi', {
      restApiName: `${PROJECT_NAME}-okta-events`,
      description: 'Okta Event Hook receiver - auto offboarding',
      deployOptions: { stageName: 'v1' },
    });
    const hookResource = eventsApi.root.addResource('okta-events');
    const integration = new apigateway.LambdaIntegration(this.oktaEventsFunction);
    hookResource.addMethod('GET', integration);   // 훅 등록 시 원타임 검증
    hookResource.addMethod('POST', integration);  // 이벤트 수신

    // --- Outputs ---
    new cdk.CfnOutput(this, 'PortalUrl', { value: portalUrl });
    new cdk.CfnOutput(this, 'OktaEventHookUrl', {
      value: `${eventsApi.url}okta-events`,
      description: 'Okta Admin > Workflow > Event Hooks에 등록할 URL',
    });
    new cdk.CfnOutput(this, 'OktaWebhookSecretArn', {
      value: this.oktaEventsWebhookSecret.secretArn,
      description: 'Okta Event Hook의 Authorization 헤더 값 (Secrets Manager에서 조회)',
    });
    new cdk.CfnOutput(this, 'CognitoDomain', { value: cognitoDomainUrl });
    new cdk.CfnOutput(this, 'OktaRedirectUri', {
      value: `${cognitoDomainUrl}/oauth2/idpresponse`,
      description: 'Okta OIDC App의 Sign-in redirect URI에 등록할 값',
    });
  }
}
