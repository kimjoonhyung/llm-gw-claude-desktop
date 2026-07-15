import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';
import { PROJECT_NAME } from '../config/constants';

export interface NetworkStackProps {
  /**
   * ALB 인바운드(80/443)를 허용할 CIDR 목록.
   * 비어 있으면 0.0.0.0/0 (전체 개방, 경고 출력).
   *
   * List of CIDRs allowed for ALB inbound (80/443).
   * If empty, 0.0.0.0/0 (fully open, warning emitted).
   */
  allowedCidrs: string[];
}

export class NetworkStack extends cdk.NestedStack {
  public readonly vpc: ec2.Vpc;
  public readonly albSg: ec2.SecurityGroup;
  public readonly ecsSg: ec2.SecurityGroup;
  public readonly rdsSg: ec2.SecurityGroup;
  public readonly lambdaSg: ec2.SecurityGroup;
  public readonly vpcEndpointSg: ec2.SecurityGroup;
  /** NAT Gateway 고정 EIP (VPC 내부發 아웃바운드 소스 IP) / Fixed NAT Gateway EIP (source IP for outbound traffic originating inside the VPC) */
  public readonly natEipPublicIp: string;

  constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id);

    // NAT Gateway EIP를 직접 할당해 주소를 고정한다.
    // ALB를 CIDR로 제한해도 VPC 내부(포털 Lambda)發 트래픽은
    // NAT EIP를 소스로 ALB에 도달하므로, 이 EIP를 허용 목록에 추가할 수 있다.
    // Allocate the NAT Gateway EIP explicitly to pin its address.
    // Even when the ALB is CIDR-restricted, traffic originating inside the VPC (portal Lambda)
    // reaches the ALB with the NAT EIP as its source, so this EIP can be added to the allowlist.
    const natEip = new ec2.CfnEIP(this, 'NatEip', {
      tags: [{ key: 'Name', value: `${PROJECT_NAME}-nat-eip` }],
    });
    this.natEipPublicIp = natEip.attrPublicIp;

    // VPC: 2 AZ, 3 subnet tiers, 1 NAT Gateway (cost optimized)
    this.vpc = new ec2.Vpc(this, 'Vpc', {
      vpcName: `${PROJECT_NAME}-vpc`,
      maxAzs: 2,
      natGateways: 1,
      natGatewayProvider: ec2.NatProvider.gateway({
        eipAllocationIds: [natEip.attrAllocationId],
      }),
      // 이 계정의 ap-northeast-2a는 NAT GW가 AZ당 한도(5)에 도달해 있어 두 번째 AZ에 배치
      // In this account, ap-northeast-2a has hit the per-AZ NAT GW limit (5), so it is placed in the second AZ
      natGatewaySubnets: {
        subnetType: ec2.SubnetType.PUBLIC,
        availabilityZones: [cdk.Stack.of(this).availabilityZones[1]],
      },
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
        },
        {
          cidrMask: 24,
          name: 'Private',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        },
        {
          cidrMask: 24,
          name: 'Isolated',
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
        },
      ],
    });

    // --- Security Groups ---

    // ALB: inbound 80/443 - allowedCidrs가 지정되면 해당 CIDR + NAT EIP만 허용
    // ALB: inbound 80/443 - when allowedCidrs is set, only those CIDRs + the NAT EIP are allowed
    this.albSg = new ec2.SecurityGroup(this, 'AlbSg', {
      vpc: this.vpc,
      securityGroupName: `${PROJECT_NAME}-alb-sg`,
      description: 'ALB security group - HTTP/HTTPS inbound',
      allowAllOutbound: true,
    });

    if (props.allowedCidrs.length > 0) {
      for (const cidr of props.allowedCidrs) {
        this.albSg.addIngressRule(
          ec2.Peer.ipv4(cidr),
          ec2.Port.tcp(80),
          `Allow HTTP from ${cidr}`,
        );
        this.albSg.addIngressRule(
          ec2.Peer.ipv4(cidr),
          ec2.Port.tcp(443),
          `Allow HTTPS from ${cidr}`,
        );
      }
      // 포털 Lambda(VPC 내부) -> NAT GW -> ALB 경로 허용 / Allow the portal Lambda (inside the VPC) -> NAT GW -> ALB path
      const natCidr = `${natEip.attrPublicIp}/32`;
      this.albSg.addIngressRule(
        ec2.Peer.ipv4(natCidr),
        ec2.Port.tcp(80),
        'Allow HTTP from NAT EIP (portal Lambda)',
      );
      this.albSg.addIngressRule(
        ec2.Peer.ipv4(natCidr),
        ec2.Port.tcp(443),
        'Allow HTTPS from NAT EIP (portal Lambda)',
      );
    } else {
      cdk.Annotations.of(this).addWarning(
        'allowedCidrs가 지정되지 않아 ALB가 0.0.0.0/0에 개방됩니다. ' +
        '프로덕션에서는 -c allowedCidrs=... (쉼표 구분)을 지정하세요.',
      );
      this.albSg.addIngressRule(
        ec2.Peer.anyIpv4(),
        ec2.Port.tcp(80),
        'Allow HTTP from anywhere',
      );
      this.albSg.addIngressRule(
        ec2.Peer.anyIpv4(),
        ec2.Port.tcp(443),
        'Allow HTTPS from anywhere',
      );
    }

    // ECS: inbound 4000 from ALB only
    this.ecsSg = new ec2.SecurityGroup(this, 'EcsSg', {
      vpc: this.vpc,
      securityGroupName: `${PROJECT_NAME}-ecs-sg`,
      description: 'ECS tasks security group - LiteLLM port',
      allowAllOutbound: true,
    });
    this.ecsSg.addIngressRule(
      this.albSg,
      ec2.Port.tcp(4000),
      'Allow LiteLLM traffic from ALB',
    );

    // Lambda (키 포털): outbound only / Lambda (key portal): outbound only
    this.lambdaSg = new ec2.SecurityGroup(this, 'LambdaSg', {
      vpc: this.vpc,
      securityGroupName: `${PROJECT_NAME}-lambda-sg`,
      description: 'Portal Lambda security group - outbound only',
      allowAllOutbound: true,
    });

    // RDS: inbound 5432 from ECS
    this.rdsSg = new ec2.SecurityGroup(this, 'RdsSg', {
      vpc: this.vpc,
      securityGroupName: `${PROJECT_NAME}-rds-sg`,
      description: 'RDS security group - PostgreSQL port',
      allowAllOutbound: false,
    });
    this.rdsSg.addIngressRule(
      this.ecsSg,
      ec2.Port.tcp(5432),
      'Allow PostgreSQL from ECS',
    );

    // VPC Endpoint SG: inbound 443 from ECS
    this.vpcEndpointSg = new ec2.SecurityGroup(this, 'VpcEndpointSg', {
      vpc: this.vpc,
      securityGroupName: `${PROJECT_NAME}-vpce-sg`,
      description: 'VPC Endpoint security group - HTTPS from ECS',
      allowAllOutbound: false,
    });
    this.vpcEndpointSg.addIngressRule(
      this.ecsSg,
      ec2.Port.tcp(443),
      'Allow HTTPS from ECS tasks',
    );

    // --- VPC Endpoints ---

    // Interface Endpoint: bedrock-runtime (traffic stays inside AWS network)
    this.vpc.addInterfaceEndpoint('BedrockRuntimeEndpoint', {
      service: ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
      subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [this.vpcEndpointSg],
    });

    // Gateway Endpoints: S3 and DynamoDB (free)
    this.vpc.addGatewayEndpoint('S3Endpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3,
    });

    this.vpc.addGatewayEndpoint('DynamoDbEndpoint', {
      service: ec2.GatewayVpcEndpointAwsService.DYNAMODB,
    });
  }
}
