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
  /**
   * Okta 테넌트 issuer URL — bootstrap 토큰 검증 및 (웹 포털 사용 시) Cognito IdP 연동에 사용
   * Okta tenant issuer URL — used for bootstrap token verification and (when the web portal is used) Cognito IdP integration
   */
  oktaIssuer: string;
  /**
   * 웹 포털(백업 플랜) 활성화 여부.
   * 주력 경로는 Claude Desktop bootstrap(앱 네이티브 OIDC)이며 Cognito가 필요 없다.
   * bootstrap 미지원 환경(구버전 앱, Claude Code CLI 등)을 위한 브라우저 키 발급이
   * 필요할 때만 true — Cognito User Pool + Hosted UI + Okta Web App 연동이 생성된다.
   *
   * Whether to enable the web portal (backup plan).
   * The primary path is Claude Desktop bootstrap (app-native OIDC), which needs no Cognito.
   * Set to true only when browser-based key issuance is needed for environments without
   * bootstrap support (older apps, Claude Code CLI, etc.) — this creates a Cognito User Pool
   * + Hosted UI + Okta Web App integration.
   */
  enableWebPortal: boolean;
  /**
   * 웹 포털용 Okta Web App 자격증명 (enableWebPortal=true일 때만 사용)
   * Okta Web App credentials for the web portal (used only when enableWebPortal=true)
   */
  oktaClientId: string;
  oktaClientSecret: string;
  models: ModelIds;
  // Lambda를 VPC 안에 두어, ALB가 CIDR로 제한돼도 NAT EIP를 통해 LiteLLM 호출 가능
  // Place the Lambda inside the VPC so it can call LiteLLM via the NAT EIP even when the ALB is CIDR-restricted
  vpc: ec2.IVpc;
  lambdaSg: ec2.ISecurityGroup;
  /**
   * 포털 Lambda를 노출할 ALB 리스너와 base URL.
   * 공개 Function URL(Principal:* 정책)은 보안 스캐너에 차단되므로
   * ALB 경로 라우팅(/portal)으로 노출한다 — allowedCidrs 제한도 함께 적용됨.
   *
   * ALB listener and base URL where the portal Lambda is exposed.
   * A public Function URL (Principal:* policy) gets blocked by security scanners,
   * so it is exposed via ALB path routing (/portal) — allowedCidrs restrictions apply as well.
   */
  albListener: elbv2.IApplicationListener;
  gatewayUrl: string;
}

/**
 * Self-Service Key Portal
 *
 * 주력 경로 — Claude Desktop bootstrap (앱 네이티브 OIDC):
 *   앱이 Okta에 직접 PKCE 로그인 → GET /portal/bootstrap (Bearer: Okta 토큰)
 *     → Lambda가 Okta 토큰 검증 → Virtual Key 포함 설정 JSON 반환 → 앱이 자동 적용
 *   사용자는 키를 보지도 입력하지도 않는다. Cognito 불필요.
 *
 * 백업 플랜 — 웹 포털 (enableWebPortal=true 시에만 생성):
 *   브라우저 → /portal → Cognito Hosted UI → Okta 로그인 → 키 화면 표시/복사
 *   bootstrap 미지원 환경(구버전 앱, Claude Code CLI)용.
 *
 * Primary path — Claude Desktop bootstrap (app-native OIDC):
 *   The app performs PKCE login directly against Okta → GET /portal/bootstrap (Bearer: Okta token)
 *     → Lambda verifies the Okta token → returns config JSON including a Virtual Key → the app applies it automatically
 *   Users never see or type the key. No Cognito required.
 *
 * Backup plan — web portal (created only when enableWebPortal=true):
 *   Browser → /portal → Cognito Hosted UI → Okta login → key shown/copied on screen
 *   For environments without bootstrap support (older apps, Claude Code CLI).
 */
export class PortalStack extends cdk.NestedStack {
  public readonly portalFunction: lambda.Function;
  public readonly portalUrl: string;
  /** 웹 포털(백업 플랜) 활성화 시에만 생성 / Created only when the web portal (backup plan) is enabled */
  public readonly userPool?: cognito.UserPool;
  /** Okta Event Hook 수신 Lambda (자동 오프보딩) / Lambda receiving Okta Event Hooks (auto offboarding) */
  public readonly oktaEventsFunction: lambda.Function;
  public readonly oktaEventsWebhookSecret: secretsmanager.Secret;

  constructor(scope: Construct, id: string, props: PortalStackProps) {
    super(scope, id);

    // --- 웹 포털용 Cognito (백업 플랜, 옵션) / Cognito for the web portal (backup plan, optional) ---
    let cognitoDomainUrl = '';
    let oktaProvider: cognito.UserPoolIdentityProviderOidc | undefined;
    let useOkta = false;
    if (props.enableWebPortal) {
      this.userPool = new cognito.UserPool(this, 'UserPool', {
        userPoolName: `${PROJECT_NAME}-portal`,
        selfSignUpEnabled: false,
        signInAliases: { email: true },
        standardAttributes: {
          email: { required: true, mutable: true },
        },
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      });

      // Hosted UI 도메인 (전역 고유 프리픽스 필요) / Hosted UI domain (requires a globally unique prefix)
      const domainPrefix = `${PROJECT_NAME}-${this.account}`;
      this.userPool.addDomain('Domain', {
        cognitoDomain: { domainPrefix },
      });
      cognitoDomainUrl = `https://${domainPrefix}.auth.${this.region}.amazoncognito.com`;

      useOkta = !!(props.oktaIssuer && props.oktaClientId && props.oktaClientSecret);
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
          '웹 포털이 Okta 연동 정보 없이 활성화되어 Cognito 자체 사용자 풀로 배포됩니다 (테스트 전용). ' +
          '프로덕션에서는 -c oktaClientId=... -c oktaClientSecret=... 을 함께 지정하세요.',
        );
      }
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
        // 주력: Claude Desktop bootstrap (/portal/bootstrap) — 둘 다 있어야 활성화
        // Primary: Claude Desktop bootstrap (/portal/bootstrap) — enabled only when both values are set
        OKTA_ISSUER: props.oktaIssuer,
        DESKTOP_OIDC_CLIENT_ID: this.node.tryGetContext('desktopOidcClientId') || '',
        // 백업: 웹 포털(브라우저 키 발급) 활성화 여부 / Backup: whether the web portal (browser key issuance) is enabled
        WEB_PORTAL_ENABLED: props.enableWebPortal ? 'true' : '',
        // MCP 커넥터 카탈로그: DDB config 테이블에서 조회 + 그룹 필터링 (재배포 불필요)
        // MCP connector catalog: looked up from the DDB config table + group filtering (no redeploy needed)
        MCP_CATALOG_ENABLED: 'true',
        CONFIG_SK_INDEX: 'sk-index',
      },
    });

    // ALB 경로 라우팅으로 노출: /portal*
    // (공개 Function URL은 Principal:* 리소스 정책이 필요해 보안 스캐너가 차단함.
    //  ALB target 방식은 ALB만 invoke 권한을 가지며, allowedCidrs 제한도 함께 적용된다.)
    // 주의: listener.addTargets()를 쓰면 규칙이 Gateway 스택에 생겨
    // Gateway <-> Portal 순환 참조가 발생한다. 타겟그룹/규칙 모두 이 스택에 생성.
    // Exposed via ALB path routing: /portal*
    // (A public Function URL requires a Principal:* resource policy, which security scanners block.
    //  With the ALB target approach only the ALB has invoke permission, and allowedCidrs restrictions apply too.)
    // Note: using listener.addTargets() would create the rule in the Gateway stack,
    // causing a Gateway <-> Portal circular reference. Both target group and rule are created in this stack.
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

    // --- 웹 포털용 Cognito App Client (백업 플랜, 옵션) / Cognito App Client for the web portal (backup plan, optional) ---
    if (props.enableWebPortal && this.userPool) {
      // 주의: Lambda 환경변수에 client ID/secret을 직접 넣으면
      // Lambda -> Client -> callback URL -> Lambda 순환 참조가 생긴다.
      // Lambda가 런타임에 Cognito API로 client 설정을 조회하도록 하여 사이클을 끊는다.
      // Note: putting the client ID/secret directly into Lambda environment variables
      // creates a Lambda -> Client -> callback URL -> Lambda circular reference.
      // The cycle is broken by having the Lambda look up the client config via the Cognito API at runtime.
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

      // Lambda 환경변수 (순환 참조가 없는 값만) / Lambda environment variables (only values without circular references)
      this.portalFunction.addEnvironment('COGNITO_DOMAIN', cognitoDomainUrl);
      this.portalFunction.addEnvironment('USER_POOL_ID', this.userPool.userPoolId);
      this.portalFunction.addEnvironment('COGNITO_CLIENT_NAME', clientName);
      this.portalFunction.addEnvironment('IDP_NAME', useOkta ? 'Okta' : '');

      // 런타임 client ID/secret 조회 권한 / Permission to look up the client ID/secret at runtime
      this.portalFunction.addToRolePolicy(new iam.PolicyStatement({
        sid: 'CognitoClientLookup',
        actions: [
          'cognito-idp:ListUserPoolClients',
          'cognito-idp:DescribeUserPoolClient',
        ],
        resources: [this.userPool.userPoolArn],
      }));
    }

    // --- Okta Event Hook: 자동 오프보딩 (SCIM deprovisioning 대체) ---
    // Okta가 인터넷에서 호출해야 하므로 CIDR 제한된 ALB가 아닌 API Gateway로 노출.
    // 인증: Okta가 보내는 Authorization 헤더를 전용 웹훅 시크릿과 비교.
    // --- Okta Event Hook: auto offboarding (replaces SCIM deprovisioning) ---
    // Okta must call this from the internet, so it is exposed via API Gateway instead of the CIDR-restricted ALB.
    // Auth: the Authorization header sent by Okta is compared against a dedicated webhook secret.
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
        // For filtering app unassignment events (must match the Okta app's display name)
        OKTA_APP_LABEL: 'LLM Gateway Key Portal',
        // 그룹 제거 이벤트 필터링용 (쉼표 구분 복수 가능, -c oktaGroupLabel=... 로 지정)
        // For filtering group removal events (multiple allowed, comma-separated; set via -c oktaGroupLabel=...)
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
    hookResource.addMethod('GET', integration);   // 훅 등록 시 원타임 검증 / One-time verification when registering the hook
    hookResource.addMethod('POST', integration);  // 이벤트 수신 / Event delivery

    // --- Outputs ---
    new cdk.CfnOutput(this, 'BootstrapUrl', {
      value: `${portalUrl}/bootstrap`,
      description: 'Claude Desktop bootstrapUrl (주력 경로 — templates/claude-desktop-bootstrap.reg 참고)',
    });
    new cdk.CfnOutput(this, 'OktaEventHookUrl', {
      value: `${eventsApi.url}okta-events`,
      description: 'Okta Admin > Workflow > Event Hooks에 등록할 URL',
    });
    new cdk.CfnOutput(this, 'OktaWebhookSecretArn', {
      value: this.oktaEventsWebhookSecret.secretArn,
      description: 'Okta Event Hook의 Authorization 헤더 값 (Secrets Manager에서 조회)',
    });
    if (props.enableWebPortal) {
      new cdk.CfnOutput(this, 'PortalUrl', {
        value: portalUrl,
        description: '웹 포털 (백업 플랜 — 브라우저 키 발급)',
      });
      new cdk.CfnOutput(this, 'CognitoDomain', { value: cognitoDomainUrl });
      new cdk.CfnOutput(this, 'OktaRedirectUri', {
        value: `${cognitoDomainUrl}/oauth2/idpresponse`,
        description: '웹 포털용 Okta Web App의 Sign-in redirect URI에 등록할 값',
      });
    }
  }
}
