# LLM Gateway — Okta Self-Service Edition

> **출처 (Attribution)**: 이 프로젝트는 AWS Samples의
> [claude-code-bedrock-enterprise-blueprint](https://github.com/aws-samples/sample-aws-kr-enterprise/tree/main/ai-ml/claude-code-bedrock-enterprise-blueprint)
> (MIT-0 라이선스)에서 파생(fork)된 것입니다. 원본의 CDK 스택 구조(Network/Database/Gateway/Monitoring)와
> LiteLLM 기반 게이트웨이 컨셉을 가져와, 아래 표의 항목들을 재설계했습니다.
> 원저작권: Amazon.com, Inc. or its affiliates.

원본 블루프린트를 기반으로, **AWS CLI를 전혀 사용하지 않는 일반 사용자**(Claude Desktop / Claude Code)를 위해 재설계한 버전입니다.

## 원본 대비 변경 사항

| 항목 | 원본 블루프린트 | 이 버전 |
|------|----------------|---------|
| 사용자 인증 | `aws sso login` + 로컬 `apiKeyHelper` 스크립트 | **브라우저에서 Okta 로그인만** (셀프서비스 키 포털) |
| Virtual Key 획득 | 로컬 셸 스크립트가 SigV4로 Token Service 호출 | **Lambda 포털이 클라우드에서 발급** — 사용자 로컬에서 스크립트 실행 없음 |
| IdP 연동 | IAM Identity Center 직접 로그인 | **Okta OIDC → Amazon Cognito** (AWS 관리형 인증 서비스) |
| 키 전달 | stdout (apiKeyHelper) | **인증된 HTTPS 세션 화면에 표시** (이메일 발송 불필요 — 아래 참고) |
| LiteLLM 이미지 | `main-latest` (불안정) | **`main-v1.83.14-stable`** (최신 안정화 태그, 컨텍스트로 오버라이드 가능) |
| 기본 리전 | `us-east-1` 하드코딩 | **`ap-northeast-2` 기본** + `-c region=...`으로 배포 시 결정 |
| 모델 프리픽스 | `us.` 하드코딩 | **리전에 따라 자동 결정** (`ap-*` → `apac.`) + `-c modelPrefix=...` 오버라이드 |
| Claude 설정 | Bedrock 모드 + AWS_PROFILE 필요 | **`ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`만** — AWS 도구 완전 불필요 |

### 왜 이메일 전송 대신 화면 표시인가?

요구사항은 "email로 virtual key를 안전하게 전달, 단 SES 대신 다른 AWS 인증 기능 활용"이었습니다. 검토 결과:

- AWS에서 임의 본문의 이메일을 보내려면 결국 SES(또는 SNS 이메일 구독)가 필요하고, **이메일은 평문 API 키가 메일함·중계 서버에 남는 전달 경로**라 보안상 권장되지 않습니다.
- 대신 **Amazon Cognito**(AWS의 관리형 인증 기능)를 Okta OIDC와 연동하여, 사용자가 Okta로 인증된 **HTTPS 세션 화면에서만** 키를 확인하도록 했습니다. 이메일 소유 검증은 Okta가 이미 수행하므로 별도 메일 발송 없이 "본인에게만 안전하게 전달"이 달성됩니다.
- 조직 정책상 이메일 통지가 꼭 필요하면 SNS 이메일 구독(키 발급 알림만, 키 값 미포함)을 추가하는 것을 권장합니다.

## 아키텍처

```
사용자 브라우저
  │
  ├─ ① 키 포털 접속 (ALB /portal → Lambda)
  │     └─ ② Cognito Hosted UI → Okta OIDC 로그인 (사용자는 Okta 암호만 입력)
  │     └─ ③ 포털 Lambda: 인증 코드 → 토큰 교환 → 이메일 확인
  │           ├─ LiteLLM Internal User 자동 생성 (/user/new, 멱등 — 이미 있으면 무시)
  │           ├─ DynamoDB 캐시 조회 (USER#{email}/VIRTUAL_KEY)
  │           └─ 캐시 미스 → LiteLLM /key/generate (예산 자동 설정) → 캐싱
  │     └─ ④ 화면에 Virtual Key + settings.json 템플릿 표시 (복사 버튼)
  │
Claude Desktop / Claude Code
  │
  └─ ⑤ ANTHROPIC_BASE_URL={ALB} + ANTHROPIC_AUTH_TOKEN={Virtual Key}
        └─ ALB → ECS Fargate (LiteLLM stable) → VPC Endpoint → Amazon Bedrock
              └─ Aurora PostgreSQL: 사용자별 사용량/예산 추적
```

CDK NestedStack 구조:

```
LlmGatewayStack (Root)
├── Network    — VPC (2 AZ), Security Groups, VPC Endpoints (Bedrock, S3, DynamoDB)
├── Database   — Aurora Serverless v2 (PostgreSQL, 0.5~4 ACU)
├── Gateway    — ALB + ECS Fargate + LiteLLM (stable 태그, model_list 포함 config)
├── Portal     — Cognito User Pool (+Okta OIDC IdP) + 키 포털 Lambda (ALB /portal 라우팅)
└── Monitoring — DynamoDB (Audit/Config), CloudWatch Dashboard/Alarms, SNS
```

## 사전 요구사항

- AWS 계정 + Bedrock Claude 모델 사용 승인 (배포 리전에서 Opus 4.6 / Sonnet 4.6 / Haiku 4.5)
- Okta OIDC Web App (아래 "Okta 설정" 참고)
- ACM 인증서 (ALB HTTPS용 — 미지정 시 HTTP로 배포되며 데모 용도로만 사용)
- Node.js 18+, AWS CDK v2

## 배포

```bash
npm install
npx cdk bootstrap   # 최초 1회

# 기본 배포 (ap-northeast-2, apac. 모델 프리픽스, stable LiteLLM)
npx cdk deploy LlmGatewayStack \
  -c certificateArn=arn:aws:acm:ap-northeast-2:123456789012:certificate/xxxx \
  -c allowedCidrs=10.0.0.0/8,203.0.113.0/24 \
  -c oktaIssuer=https://your-org.okta.com \
  -c oktaClientId=0oaXXXXXXXX \
  -c oktaClientSecret=XXXXXXXX
```

배포 시 결정 가능한 컨텍스트 값:

| 컨텍스트 키 | 기본값 | 설명 |
|-------------|--------|------|
| `region` | `ap-northeast-2` | 배포 리전 (`-c region=us-east-1` 등) |
| `modelPrefix` | `global` | Bedrock inference profile 프리픽스 (`us`/`eu`/`apac`/`global`). Claude 4.6/4.5 세대는 대부분 리전에서 `global` 프로필로만 제공됨 |
| `litellmImageTag` | `main-v1.83.14-stable` | LiteLLM GHCR 이미지 태그 |
| `certificateArn` | (없음) | ALB HTTPS 인증서. 미지정 시 HTTP-only (데모 전용) |
| `allowedCidrs` | (없음 = 전체 개방) | ALB 인바운드 허용 CIDR 목록, 쉼표 구분 (예: `-c allowedCidrs=10.0.0.0/8,203.0.113.0/24`) |
| `oktaIssuer` | (없음) | Okta 도메인 URL. 미지정 시 Cognito 자체 사용자 풀 (테스트 전용) |
| `oktaClientId` / `oktaClientSecret` | (없음) | Okta OIDC 앱 자격증명 (포털용 Web App) |
| `desktopOidcClientId` | (없음) | Claude Desktop 앱 네이티브 OIDC용 Native App Client ID. 지정 시 LiteLLM JWT 인증 활성화 — [docs/claude-desktop-oidc-guide.md](docs/claude-desktop-oidc-guide.md) |

> Okta client secret을 셸 히스토리에 남기고 싶지 않다면 `cdk.json`의 `context`에 넣거나 환경변수 `OKTA_CLIENT_SECRET`으로 전달하세요.

### 배포 후 Okta 설정 (1회)

상세 절차는 **[docs/okta-integration-guide.md](docs/okta-integration-guide.md)** 참고. 요약:

1. Okta Admin에서 OIDC **Web Application** 생성 (Grant: Authorization Code)
2. Sign-in redirect URI에 배포 출력의 `OktaRedirectUri` 값 등록
3. Client ID/Secret을 `-c oktaClientId=... -c oktaClientSecret=... -c oktaIssuer=...`로 재배포
4. Assignments에 게이트웨이 사용을 허용할 사용자/그룹 할당

## 사용자 온보딩 (일반 사용자 — AWS CLI 불필요)

1. 관리자가 공유한 **키 포털 URL** (배포 출력 `KeyPortalUrl`)을 브라우저에서 연다.
2. 자동으로 **Okta 로그인** 화면으로 이동 → 회사 계정으로 로그인.
3. 화면에 표시된 **Virtual Key**와 **settings.json 설정**을 복사한다.
4. `~/.claude/settings.json`에 붙여넣기:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://{ALB_DNS}",
    "ANTHROPIC_AUTH_TOKEN": "sk-...",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "global.anthropic.claude-opus-4-6-v1",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "global.anthropic.claude-sonnet-4-6",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "global.anthropic.claude-haiku-4-5-20251001-v1:0"
  }
}
```

5. Claude Code/Desktop 실행. 끝 — `aws sso login`, apiKeyHelper, AWS 프로필 설정이 모두 필요 없습니다.

같은 계정으로 포털에 다시 로그인하면 항상 동일한 키가 표시됩니다(DynamoDB 캐시). 키 유출 시 관리자가 LiteLLM UI(`/ui/`)에서 해당 키를 폐기하고 DynamoDB 캐시 항목(`USER#{email}`)을 삭제하면 다음 로그인 시 새 키가 발급됩니다.

## 예산/사용량 관리

Virtual Key 발급 시 사용자별 예산이 자동 설정됩니다 (`lib/config/constants.ts`):

- `MONTHLY_LIMIT_USD: 1000` — 사용자당 최대 예산
- `BUDGET_DURATION: '30d'` — 예산 리셋 주기

LiteLLM Admin UI(`{GatewayUrl}/ui/`, master key로 로그인)에서 사용자별 사용량 확인 및 키 관리가 가능합니다.

## 디렉토리 구조

```
├── bin/app.ts                        # CDK 진입점 (리전 컨텍스트 처리)
├── lib/
│   ├── config/constants.ts           # 리전/모델 프리픽스/LiteLLM 태그/예산
│   └── stacks/
│       ├── root-stack.ts             # NestedStack 오케스트레이션 + Outputs
│       ├── network-stack.ts          # VPC, SG, VPC Endpoints
│       ├── database-stack.ts         # Aurora Serverless v2
│       ├── gateway-stack.ts          # ALB + ECS + LiteLLM (stable, inline config)
│       ├── portal-stack.ts           # Cognito(+Okta OIDC) + 키 포털 Lambda
│       └── monitoring-stack.ts       # DynamoDB, CloudWatch, SNS
├── lambda/key-portal/
│   ├── handler.py                    # OAuth 콜백 + Virtual Key 발급/표시
│   └── tests/test_handler.py         # 단위 테스트 (python3 -m unittest)
├── templates/claude-settings.json    # 사용자 배포용 settings.json 템플릿
├── cdk.json
└── package.json
```

## 테스트

```bash
npx tsc --noEmit                                   # CDK 타입 체크
npx cdk synth --quiet                              # 템플릿 합성 검증
cd lambda/key-portal && python3 -m unittest discover -s tests   # Lambda 단위 테스트
```

## 보안 노트

- Virtual Key는 이메일/로그에 남기지 않고 인증된 세션 화면에만 표시합니다 (`Cache-Control: no-store`, `noindex`).
- OAuth 콜백은 HMAC 서명된 state 쿠키로 CSRF를 방어합니다.
- 포털 Lambda는 공개 Function URL이 아닌 **ALB 경로 라우팅(`/portal`)**으로 노출됩니다. Function URL(NONE 인증)은 `Principal: *` 리소스 정책이 필요해 보안 스캐너(예: Amazon 내부 Palisade)가 world-accessible Lambda로 탐지/차단합니다. ALB 방식은 `elasticloadbalancing.amazonaws.com` 서비스만 invoke 권한을 가지며, `allowedCidrs` 제한도 포털에 동일하게 적용됩니다.
- 키 발급은 Cognito 토큰 교환(서버 측 client secret 필요)을 통과해야만 가능합니다.
- LiteLLM master key는 Secrets Manager에만 존재하며 포털 Lambda와 ECS task만 읽을 수 있습니다.
- 프로덕션에서는 반드시 `certificateArn`을 지정해 ALB를 HTTPS로 노출하세요.
- **ALB 인바운드 제한**: `allowedCidrs`에 회사 네트워크/VPN CIDR을 쉼표로 나열하면 ALB 80/443이 해당 대역에서만 접근 가능합니다. 미지정 시 0.0.0.0/0 개방(데모 전용, synth 경고 출력). 잘못된 CIDR 형식은 synth 단계에서 실패합니다.
  - 포털 Lambda는 VPC 내부(프라이빗 서브넷)에서 고정 NAT EIP를 통해 LiteLLM을 호출하며, 이 EIP는 허용 목록에 자동 추가됩니다 — 별도 설정 불필요.
  - 배포 출력 `NatEip`로 실제 EIP를 확인할 수 있습니다 (외부 서비스 방화벽 등록 등에 활용).
  - 참고: Okta/Cognito 로그인 트래픽은 ALB를 거치지 않으므로 CIDR 제한과 무관하게 동작합니다. 단, **포털에서 키를 받는 것은 어디서나 가능하되, 게이트웨이 사용은 허용된 네트워크에서만** 가능해집니다.
