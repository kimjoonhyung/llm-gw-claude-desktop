> 🇰🇷 한국어 | 🇺🇸 [English](en/02-deployment.md)

# 02. 배포

## 사전 요구사항

- AWS 계정 + 배포 리전에서 **Bedrock Claude 모델 사용 승인** (Opus 4.8 / Sonnet 4.6 / Haiku 4.5)
- Node.js 18+, AWS CDK v2
- Okta 테넌트 (OIDC 앱 생성 권한) — [03. Okta 설정](03-okta-setup.md)
- ALB HTTPS용 인증서: 정식 도메인 + ACM 인증서(권장) 또는 자체서명 임포트

## 기본 배포 (주력: bootstrap)

```bash
npm install
npx cdk bootstrap   # 최초 1회

npx cdk deploy LlmGatewayStackV2 \
  -c certificateArn=arn:aws:acm:{REGION}:{ACCOUNT}:certificate/{CERT_ID} \
  -c allowedCidrs={사내대역1},{사내대역2} \
  -c oktaIssuer=https://{your-org}.okta.com \
  -c desktopOidcClientId={Okta Native App Client ID}
```

이 구성이 배포되는 것:
- VPC / ALB / ECS(LiteLLM) / Aurora / DynamoDB
- Key Portal Lambda + `/portal/bootstrap` 엔드포인트 (desktopOidcClientId 지정 시 활성)
- Okta Event Hook 수신 API + Lambda (자동 오프보딩)
- **Cognito는 생성되지 않음** (웹 포털 비활성 기본값)

## 컨텍스트 파라미터

| 컨텍스트 키 | 기본값 | 설명 |
|-------------|--------|------|
| `region` | `ap-northeast-2` | 배포 리전 |
| `certificateArn` | (없음) | ALB HTTPS 인증서. 미지정 시 HTTP-only(데모 전용) |
| `allowedCidrs` | (없음=전체개방) | ALB 인바운드 허용 CIDR, 쉼표 구분 |
| `oktaIssuer` | (없음) | Okta 테넌트 issuer URL |
| **`desktopOidcClientId`** | (없음) | **주력**: bootstrap용 Okta Native App Client ID. 지정 시 `/portal/bootstrap` 활성 |
| `enableWebPortal` | `false` | **백업**: Cognito 웹 포털 활성화 |
| `oktaClientId` / `oktaClientSecret` | (없음) | 웹 포털용 Okta Web App 자격증명 (`enableWebPortal=true`일 때만) |
| `modelPrefix` | `global` | Bedrock inference profile 프리픽스 (`us`/`eu`/`apac`/`global`) |
| `litellmImageTag` | `main-v1.83.14-stable` | LiteLLM 컨테이너 이미지 태그 |

> MCP 커넥터는 배포 컨텍스트가 아니라 **DDB 카탈로그**(`scripts/mcp_catalog.py`)로 관리합니다.
> 재배포 없이 추가/회수/그룹지정/승인정책 설정 — [05. MCP 커넥터](05-agentcore-mcp.md).

## 백업 경로(웹 포털)까지 배포

```bash
npx cdk deploy LlmGatewayStackV2 \
  ... 기본 컨텍스트 ... \
  -c enableWebPortal=true \
  -c oktaClientId={Okta Web App Client ID} \
  -c oktaClientSecret={Okta Web App Secret}
```

> Web App은 bootstrap용 Native App과 **별개**입니다. bootstrap만 쓸 거면 웹 포털은
> 배포하지 않아도 됩니다.

## 배포 출력

| 출력 | 용도 |
|------|------|
| `GatewayUrl` | LiteLLM 게이트웨이 base URL |
| `BootstrapUrl` | Claude Desktop `.reg`의 `bootstrapUrl` 값 |
| `OktaEventHookUrl` | Okta Event Hook에 등록할 URL |
| `OktaWebhookSecretArn` | Event Hook 인증 시크릿 (Secrets Manager) |
| `KeyPortalUrl` | 웹 포털 URL (백업 활성 시) |

## 자체서명 인증서 임포트 (정식 인증서가 없을 때)

```bash
# ALB DNS 이름을 CN/SAN으로 하는 자체서명 인증서 생성 후 ACM 임포트
openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
  -keyout gateway.key -out gateway.crt \
  -subj "/CN={ALB_DNS}" \
  -addext "subjectAltName=DNS:{ALB_DNS},DNS:*.{REGION}.elb.amazonaws.com"

aws acm import-certificate --certificate fileb://gateway.crt \
  --private-key fileb://gateway.key --region {REGION}
```

> **주의**: SAN이 실제 ALB 호스트명과 일치해야 합니다. 와일드카드 `*.elb.amazonaws.com`은
> 한 레벨만 매칭하므로 `xxx.{region}.elb.amazonaws.com` 형태에는 안 맞습니다.
> 사용자 PC에는 인증서 신뢰 설치가 필요합니다 — [04. Claude Desktop 배포](04-claude-desktop.md) 참고.

## 리전·모델 참고

Claude 4.6/4.5 세대는 대부분 리전에서 **`global.` inference profile로만** 제공됩니다
(예: ap-northeast-2에는 `apac.` 4.6 프로필이 없음). 그래서 기본 프리픽스는 `global`입니다.
`/v1/models`는 지정된 3개 모델만 노출합니다(와일드카드 미사용 — 거버넌스 + Claude Desktop
연결 테스트 호환).
