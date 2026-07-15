#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { DEFAULT_REGION } from '../lib/config/constants';
import { RootStack } from '../lib/stacks/root-stack';

const app = new cdk.App();

// 배포 리전: -c region=... > CDK_DEFAULT_REGION > ap-northeast-2 (기본값)
// Deployment region: -c region=... > CDK_DEFAULT_REGION > ap-northeast-2 (default)
const region =
  app.node.tryGetContext('region') ||
  process.env.CDK_DEFAULT_REGION ||
  DEFAULT_REGION;

// 기존 원본 블루프린트 스택(LlmGatewayStack)과 병행 운영을 위해 V2로 분리
// Split off as V2 to run alongside the existing original blueprint stack (LlmGatewayStack)
new RootStack(app, 'LlmGatewayStackV2', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region,
  },
});

app.synth();
