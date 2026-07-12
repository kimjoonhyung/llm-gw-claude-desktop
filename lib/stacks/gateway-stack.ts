import * as cdk from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import type * as rds from 'aws-cdk-lib/aws-rds';
import { Construct } from 'constructs';
import { PROJECT_NAME, ModelIds } from '../config/constants';

export interface GatewayStackProps {
  vpc: ec2.IVpc;
  albSg: ec2.ISecurityGroup;
  ecsSg: ec2.ISecurityGroup;
  dbCluster: rds.DatabaseCluster;
  /** 비어 있으면 HTTP(80)로만 노출 (데모/테스트용). 프로덕션은 ACM 인증서 필수 */
  certificateArn: string;
  litellmImageTag: string;
  models: ModelIds;
  /**
   * Claude Desktop 앱 네이티브 OIDC(inferenceGatewayOidc)용 JWT 인증.
   * 둘 다 지정 시 LiteLLM에 enable_jwt_auth가 켜지고, Okta JWKS로 서명 검증 +
   * audience(클라이언트 ID) 고정 + 사용자 자동 생성(user_id_upsert)이 활성화된다.
   * Virtual Key 방식과 병행 동작한다.
   */
  oktaIssuer: string;
  desktopOidcClientId: string;
}

export class GatewayStack extends cdk.NestedStack {
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly ecsService: ecs.FargateService;
  public readonly taskDefinition: ecs.FargateTaskDefinition;
  public readonly litellmMasterKeySecret: secretsmanager.Secret;
  /** Lambda/포털에서 사용할 게이트웨이 base URL (인증서 유무에 따라 https/http) */
  public readonly gatewayUrl: string;
  /** 포털 등 추가 라우팅 규칙을 붙일 기본 리스너 (HTTPS 또는 HTTP) */
  public readonly listener: elbv2.ApplicationListener;

  constructor(scope: Construct, id: string, props: GatewayStackProps) {
    super(scope, id);

    // --- LiteLLM Master Key ---
    this.litellmMasterKeySecret = new secretsmanager.Secret(this, 'LitellmMasterKey', {
      secretName: `${PROJECT_NAME}/litellm-master-key`,
      description: 'LiteLLM proxy master key',
      generateSecretString: {
        passwordLength: 32,
        excludePunctuation: true,
        includeSpace: false,
      },
    });

    // --- ECS Cluster ---
    const cluster = new ecs.Cluster(this, 'Cluster', {
      clusterName: `${PROJECT_NAME}-cluster`,
      vpc: props.vpc,
      containerInsightsV2: ecs.ContainerInsights.ENHANCED,
    });

    // --- CloudWatch Log Group ---
    const logGroup = new logs.LogGroup(this, 'LitellmLogGroup', {
      logGroupName: `/ecs/${PROJECT_NAME}/litellm`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // --- Task Definition ---
    this.taskDefinition = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      cpu: 2048,
      memoryLimitMiB: 4096,
    });

    // Task Role: Bedrock (모든 cross-region inference profile 프리픽스 허용)
    this.taskDefinition.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: 'BedrockAccess',
      actions: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
      ],
      resources: [
        `arn:aws:bedrock:*:${this.account}:inference-profile/*.anthropic.claude-*`,
        'arn:aws:bedrock:*::foundation-model/anthropic.claude-*',
      ],
    }));

    this.taskDefinition.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: 'CloudWatchMetrics',
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
      conditions: {
        StringEquals: { 'cloudwatch:namespace': 'LLMGateway' },
      },
    }));

    // --- LiteLLM config.yaml ---
    // 공식 이미지를 그대로 사용하고(Docker 빌드 불필요), 컨테이너 시작 시 config를 인라인 생성.
    // Claude Code가 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN(Virtual Key)로 호출하면
    // /v1/messages 요청의 모델명을 model_list로 매핑해 Bedrock으로 라우팅한다.
    const { opus, sonnet, haiku } = props.models;

    // 주의: LiteLLM의 enable_jwt_auth(직접 JWT 인증, B안)는 Enterprise 전용 기능이다.
    // OSS 버전에서 켜면 모든 인증이 "enterprise only feature" 에러로 차단된다 (실측 확인).
    // Claude Desktop 앱 네이티브 OIDC는 bootstrap 방식(A안)으로 지원한다:
    // 포털 Lambda(/portal/bootstrap)가 Okta 토큰을 검증하고 Virtual Key를 내려주므로
    // LiteLLM 쪽 변경이 전혀 필요 없다. desktopOidcClientId는 Lambda에만 전달된다.

    const litellmConfig = [
      'model_list:',
      `  - model_name: ${opus}`,
      '    litellm_params:',
      `      model: bedrock/converse/${opus}`,
      `      aws_region_name: ${this.region}`,
      `  - model_name: ${sonnet}`,
      '    litellm_params:',
      `      model: bedrock/converse/${sonnet}`,
      `      aws_region_name: ${this.region}`,
      `  - model_name: ${haiku}`,
      '    litellm_params:',
      `      model: bedrock/converse/${haiku}`,
      `      aws_region_name: ${this.region}`,
      // 주의: "bedrock/*" 와일드카드를 넣으면 /v1/models가 호출 불가능한
      // 모델까지 수백 개 노출해 Claude Desktop 연결 테스트가 실패한다.
      // 지정된 모델만 노출/허용하는 것이 거버넌스 측면에서도 올바르다.
      'litellm_settings:',
      '  drop_params: true',
      // 도구 호출(MCP) 플로우에서 빈 텍스트 블록이 생기면 Bedrock Converse가
      // "text content blocks must be non-empty"로 거부한다. modify_params가 자동 보정.
      '  modify_params: true',
      'general_settings:',
      '  master_key: os.environ/LITELLM_MASTER_KEY',
      '  database_url: os.environ/DATABASE_URL',
    ].join('\n');

    // --- Container ---
    this.taskDefinition.addContainer('litellm', {
      // main-latest 대신 안정화(stable) 태그로 버전 고정
      image: ecs.ContainerImage.fromRegistry(`ghcr.io/berriai/litellm:${props.litellmImageTag}`),
      portMappings: [{ containerPort: 4000, protocol: ecs.Protocol.TCP }],
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: 'litellm',
      }),
      secrets: {
        DB_HOST: ecs.Secret.fromSecretsManager(props.dbCluster.secret!, 'host'),
        DB_PORT: ecs.Secret.fromSecretsManager(props.dbCluster.secret!, 'port'),
        DB_USERNAME: ecs.Secret.fromSecretsManager(props.dbCluster.secret!, 'username'),
        DB_PASSWORD: ecs.Secret.fromSecretsManager(props.dbCluster.secret!, 'password'),
        LITELLM_MASTER_KEY: ecs.Secret.fromSecretsManager(this.litellmMasterKeySecret),
      },
      environment: {
        DB_NAME: 'litellm',
        LITELLM_CONFIG_CONTENT: litellmConfig,
      },
      entryPoint: ['sh', '-c'],
      command: [
        'printf \'%s\' "$LITELLM_CONFIG_CONTENT" > /tmp/config.yaml && ' +
        'export DATABASE_URL="postgresql://${DB_USERNAME}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}" && ' +
        'exec litellm --config /tmp/config.yaml --port 4000',
      ],
      healthCheck: {
        command: ['CMD-SHELL', 'python -c "import urllib.request; urllib.request.urlopen(\'http://localhost:4000/health/liveliness\')" || exit 1'],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(10),
        retries: 3,
        startPeriod: cdk.Duration.seconds(120),
      },
    });

    // --- ECS Service ---
    this.ecsService = new ecs.FargateService(this, 'Service', {
      serviceName: `${PROJECT_NAME}-litellm`,
      cluster,
      taskDefinition: this.taskDefinition,
      desiredCount: 1,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [props.ecsSg],
      circuitBreaker: { enable: true, rollback: true },
      assignPublicIp: false,
    });

    // --- ALB ---
    this.alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      loadBalancerName: `${PROJECT_NAME}-alb`,
      vpc: props.vpc,
      internetFacing: true,
      securityGroup: props.albSg,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      idleTimeout: cdk.Duration.seconds(300),
    });

    const targetGroup = new elbv2.ApplicationTargetGroup(this, 'TargetGroup', {
      targetGroupName: `${PROJECT_NAME}-litellm-tg`,
      vpc: props.vpc,
      port: 4000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        path: '/health/liveliness',
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
      },
      deregistrationDelay: cdk.Duration.seconds(300),
    });

    targetGroup.addTarget(this.ecsService);

    if (props.certificateArn) {
      // HTTPS listener + HTTP->HTTPS redirect
      const certificate = acm.Certificate.fromCertificateArn(this, 'Certificate', props.certificateArn);
      // open: false — 인바운드 규칙은 NetworkStack의 allowedCidrs로만 관리
      this.listener = this.alb.addListener('HttpsListener', {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        sslPolicy: elbv2.SslPolicy.TLS13_RES,
        certificates: [certificate],
        defaultTargetGroups: [targetGroup],
        open: false,
      });
      this.alb.addListener('HttpListener', {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultAction: elbv2.ListenerAction.redirect({
          protocol: 'HTTPS',
          port: '443',
          permanent: true,
        }),
        open: false,
      });
      this.gatewayUrl = `https://${this.alb.loadBalancerDnsName}`;
    } else {
      // 인증서 미지정: HTTP-only (데모/테스트 전용)
      cdk.Annotations.of(this).addWarning(
        'certificateArn이 지정되지 않아 ALB가 HTTP(80)로만 노출됩니다. 프로덕션에서는 -c certificateArn=... 을 지정하세요.',
      );
      this.listener = this.alb.addListener('HttpListener', {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultTargetGroups: [targetGroup],
        open: false,
      });
      this.gatewayUrl = `http://${this.alb.loadBalancerDnsName}`;
    }
  }
}
