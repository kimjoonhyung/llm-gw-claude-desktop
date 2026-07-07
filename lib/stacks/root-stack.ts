import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import { NetworkStack } from './network-stack';
import { DatabaseStack } from './database-stack';
import { GatewayStack } from './gateway-stack';
import { PortalStack } from './portal-stack';
import { MonitoringStack } from './monitoring-stack';
import {
  AUDIT_TABLE_NAME,
  CONFIG_TABLE_NAME,
  LITELLM_IMAGE_TAG,
  modelsFor,
  resolveModelPrefix,
} from '../config/constants';

export class RootStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const ctx = (key: string): string =>
      this.node.tryGetContext(key) || process.env[key.replace(/([A-Z])/g, '_$1').toUpperCase()] || '';

    const modelPrefix = resolveModelPrefix(this.region, this.node.tryGetContext('modelPrefix'));
    const models = modelsFor(modelPrefix);
    const litellmImageTag = this.node.tryGetContext('litellmImageTag') || LITELLM_IMAGE_TAG;

    // ALB 인바운드 허용 CIDR 목록 (쉼표 구분: -c allowedCidrs=10.0.0.0/8,203.0.113.0/24)
    const allowedCidrs = ctx('allowedCidrs')
      .split(',')
      .map((c: string) => c.trim())
      .filter((c: string) => c.length > 0);
    for (const cidr of allowedCidrs) {
      if (!/^\d{1,3}(\.\d{1,3}){3}\/\d{1,2}$/.test(cidr)) {
        throw new Error(`allowedCidrs에 잘못된 CIDR 형식이 있습니다: "${cidr}" (예: 203.0.113.0/24)`);
      }
    }

    const network = new NetworkStack(this, 'Network', { allowedCidrs });

    const database = new DatabaseStack(this, 'Database', {
      vpc: network.vpc,
      rdsSg: network.rdsSg,
    });

    const gateway = new GatewayStack(this, 'Gateway', {
      vpc: network.vpc,
      albSg: network.albSg,
      ecsSg: network.ecsSg,
      dbCluster: database.cluster,
      certificateArn: ctx('certificateArn'),
      litellmImageTag,
      models,
      oktaIssuer: ctx('oktaIssuer'),
      desktopOidcClientId: ctx('desktopOidcClientId'),
    });

    // 웹 포털(Cognito 기반 브라우저 키 발급)은 백업 플랜 — 기본 비활성.
    // 주력은 Claude Desktop bootstrap (desktopOidcClientId로 활성화).
    const enableWebPortal = ctx('enableWebPortal') === 'true';

    const portal = new PortalStack(this, 'Portal', {
      oktaIssuer: ctx('oktaIssuer'),
      enableWebPortal,
      oktaClientId: ctx('oktaClientId'),
      oktaClientSecret: ctx('oktaClientSecret'),
      models,
      vpc: network.vpc,
      lambdaSg: network.lambdaSg,
      albListener: gateway.listener,
      gatewayUrl: gateway.gatewayUrl,
    });

    // Portal Lambda: LiteLLM 연동 환경변수
    portal.portalFunction.addEnvironment('CONFIG_TABLE_NAME', CONFIG_TABLE_NAME);
    portal.portalFunction.addEnvironment('GATEWAY_URL', gateway.gatewayUrl);
    portal.portalFunction.addEnvironment('LITELLM_ENDPOINT', gateway.gatewayUrl);
    portal.portalFunction.addEnvironment('LITELLM_MASTER_KEY_ARN', gateway.litellmMasterKeySecret.secretArn);

    // Portal Lambda: Secrets Manager 읽기 권한 (LiteLLM Master Key)
    gateway.litellmMasterKeySecret.grantRead(portal.portalFunction);

    // Portal Lambda: DynamoDB config 테이블 읽기+쓰기 권한 (Virtual Key 캐시)
    // 고정 테이블 이름 + ARN 사용 (Portal <-> Monitoring 순환 참조 방지)
    portal.portalFunction.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ConfigTableReadWrite',
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem'],
      resources: [`arn:aws:dynamodb:${this.region}:${this.account}:table/${CONFIG_TABLE_NAME}`],
    }));

    // Okta Events Lambda (자동 오프보딩): LiteLLM 연동 + 캐시 삭제 권한
    portal.oktaEventsFunction.addEnvironment('CONFIG_TABLE_NAME', CONFIG_TABLE_NAME);
    portal.oktaEventsFunction.addEnvironment('LITELLM_ENDPOINT', gateway.gatewayUrl);
    portal.oktaEventsFunction.addEnvironment('LITELLM_MASTER_KEY_ARN', gateway.litellmMasterKeySecret.secretArn);
    gateway.litellmMasterKeySecret.grantRead(portal.oktaEventsFunction);
    portal.oktaEventsFunction.addToRolePolicy(new iam.PolicyStatement({
      sid: 'ConfigTableDelete',
      actions: ['dynamodb:DeleteItem'],
      resources: [`arn:aws:dynamodb:${this.region}:${this.account}:table/${CONFIG_TABLE_NAME}`],
    }));

    new MonitoringStack(this, 'Monitoring', {
      ecsClusterName: gateway.ecsService.cluster.clusterName,
      ecsServiceName: gateway.ecsService.serviceName,
      albFullName: gateway.alb.loadBalancerFullName,
    });

    // ECS task role: audit 테이블 쓰기 권한 (custom callback용, 고정 ARN으로 순환 참조 방지)
    gateway.taskDefinition.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      sid: 'AuditTableWriteAccess',
      actions: [
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:BatchWriteItem',
      ],
      resources: [
        `arn:aws:dynamodb:${this.region}:${this.account}:table/${AUDIT_TABLE_NAME}`,
      ],
    }));

    gateway.taskDefinition.defaultContainer!.addEnvironment(
      'AUDIT_TABLE_NAME',
      AUDIT_TABLE_NAME,
    );

    // --- Outputs ---
    new cdk.CfnOutput(this, 'GatewayUrl', {
      value: gateway.gatewayUrl,
      description: 'LLM Gateway base URL (ANTHROPIC_BASE_URL)',
    });
    new cdk.CfnOutput(this, 'KeyPortalUrl', {
      value: enableWebPortal
        ? portal.portalUrl
        : `${portal.portalUrl} (웹 포털 비활성 — 주력은 bootstrap, -c enableWebPortal=true로 활성화)`,
      description: 'Self-service key portal URL (백업 플랜)',
    });
    new cdk.CfnOutput(this, 'DeployedRegion', { value: this.region });
    new cdk.CfnOutput(this, 'NatEip', {
      value: network.natEipPublicIp,
      description: 'NAT Gateway EIP (VPC 내부 트래픽의 소스 IP — ALB 허용 목록에 자동 포함)',
    });
    new cdk.CfnOutput(this, 'AlbAllowedCidrs', {
      value: allowedCidrs.length > 0 ? allowedCidrs.join(', ') : '0.0.0.0/0 (전체 개방 — 데모 전용)',
    });
    new cdk.CfnOutput(this, 'ModelPrefix', { value: modelPrefix });
    new cdk.CfnOutput(this, 'LitellmImage', { value: `ghcr.io/berriai/litellm:${litellmImageTag}` });
    new cdk.CfnOutput(this, 'DesktopOidcJwtAuth', {
      value: ctx('desktopOidcClientId')
        ? `enabled (audience: ${ctx('desktopOidcClientId')})`
        : 'disabled (-c desktopOidcClientId=... 로 활성화)',
      description: 'Claude Desktop 앱 네이티브 OIDC(JWT) 인증 상태',
    });
  }
}
