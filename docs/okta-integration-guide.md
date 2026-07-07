# Okta 연동 가이드

키 포털의 인증을 Cognito 자체 사용자 풀(테스트용)에서 **Okta OIDC**로 전환하는 절차입니다.
연동 후 사용자는 회사 Okta 계정만으로 포털에 로그인해 Virtual Key를 받습니다.

연동 구조:

```
사용자 → 포털(/portal) → Cognito Hosted UI → Okta 로그인 화면
                              ↑                    │
                              └── OIDC 콜백 ←──────┘
       (identity_provider=Okta 파라미터로 Cognito 화면은 건너뛰고 바로 Okta로 이동)
```

Cognito는 Okta와 포털 사이의 브로커 역할만 합니다. 사용자 암호는 Okta에만 존재하고,
Cognito 풀에는 Okta에서 전달받은 프로필(이메일)만 저장됩니다.

---

## 배포 기준 값 예시

| 항목 | 값 (배포 출력에서 확인) |
|------|-----|
| Okta에 등록할 Redirect URI | `https://llm-gw-gs-{ACCOUNT_ID}.auth.{REGION}.amazoncognito.com/oauth2/idpresponse` |
| Cognito 도메인 | `https://llm-gw-gs-{ACCOUNT_ID}.auth.{REGION}.amazoncognito.com` |
| 포털 URL | `https://{ALB_DNS}/portal` |

> 정확한 값은 배포 출력의 `OktaRedirectUri`, `CognitoDomain`, `KeyPortalUrl`을 사용하세요.

---

## 1단계 — Okta 관리자: OIDC 앱 생성

Okta Admin Console (`https://{your-org}-admin.okta.com`) 에서:

1. **Applications → Applications → Create App Integration**
2. 옵션 선택:
   - Sign-in method: **OIDC - OpenID Connect**
   - Application type: **Web Application**
3. 앱 설정:

| 설정 | 값 |
|------|-----|
| App integration name | `LLM Gateway Key Portal` (자유) |
| Grant type | **Authorization Code** (기본값, 그 외 체크 불필요) |
| Sign-in redirect URIs | 배포 출력의 `OktaRedirectUri` 값 |
| Sign-out redirect URIs | (비워둠) |
| Controlled access | 게이트웨이 사용을 허용할 그룹 선택 (예: `llm-gateway-users`) |

4. 저장 후 **General 탭**에서 다음 두 값을 복사:
   - **Client ID** (예: `0oa1a2b3c4d5e6f7g8h9`)
   - **Client secret**

5. **Okta 도메인(Issuer) 확인**: Security → API → Authorization Servers에서
   확인하거나, 일반적으로 `https://{your-org}.okta.com` 입니다.
   - default authorization server를 쓰는 경우 `https://{your-org}.okta.com/oauth2/default`가 issuer일 수 있습니다.
     `https://{issuer}/.well-known/openid-configuration` 이 열리는 URL이 올바른 issuer입니다 (2단계 참고).

> **필요 클레임**: 포털은 `email`(필수), `name`(선택)을 사용합니다.
> Web App + `openid email profile` 스코프 기본 설정이면 충분하며 별도 클레임 매핑은 불필요합니다.

---

## 2단계 — Issuer URL 검증 (선택이지만 권장)

배포 전에 issuer가 올바른지 확인합니다:

```bash
curl -s https://{your-org}.okta.com/.well-known/openid-configuration | python3 -m json.tool | head -5
```

`"issuer": "https://{your-org}.okta.com"` 이 나오면 그 값을 그대로 씁니다.
404가 나오면 `https://{your-org}.okta.com/oauth2/default` 로 시도하세요.
Cognito는 이 URL 뒤에 `/.well-known/openid-configuration`을 붙여 엔드포인트를 자동 발견합니다.

---

## 3단계 — 재배포 (Okta 정보 주입)

```bash
cd llm-gw-gs

npx cdk deploy LlmGatewayStackV2 \
  -c certificateArn=arn:aws:acm:{REGION}:{ACCOUNT_ID}:certificate/{CERT_ID} \
  -c allowedCidrs={YOUR_ALLOWED_CIDRS} \
  -c oktaIssuer=https://{your-org}.okta.com \
  -c oktaClientId={1단계의 Client ID} \
  -c oktaClientSecret={1단계의 Client secret} \
  --require-approval never
```

배포가 하는 일:
- Cognito User Pool에 `Okta` OIDC Identity Provider 생성 (스코프: `openid email profile`)
- 포털 App Client의 IdP를 Cognito 자체 풀 → Okta로 전환
- 포털 Lambda의 `IDP_NAME=Okta` 설정 → 로그인 시 Cognito 화면을 건너뛰고 바로 Okta로 리다이렉트

> **secret을 셸 히스토리에 남기고 싶지 않으면** `cdk.json`의 `context`에 넣어도 됩니다.
> 단, 이 경우 `cdk.json`을 git에 커밋하지 않도록 주의하세요.

### 기존 사용자 영향

- 이미 발급된 **Virtual Key는 그대로 유효**합니다 (키는 LiteLLM/DynamoDB에 있고 IdP와 무관).
- 포털 재로그인 시부터 Okta 인증을 거치며, **이메일이 같으면 기존 키가 그대로 표시**됩니다.
  - 주의: Cognito 자체 풀 시절의 이메일과 Okta 이메일이 다르면 다른 사용자로 취급되어 새 키가 발급됩니다.

---

## 4단계 — 동작 확인

1. **허용된 네트워크에서** 포털 접속:
   ```
   https://{ALB_DNS}/portal
   ```
2. 자동으로 **Okta 로그인 화면**으로 이동하는지 확인 (Cognito 로그인 화면이 보이면 안 됨)
3. Okta 계정으로 로그인 → 포털에 Virtual Key + settings.json 표시
4. 표시된 설정으로 Claude Code 실행해 응답 확인

CLI로 리다이렉트만 빠르게 확인:

```bash
curl -sk -o /dev/null -w "%{redirect_url}\n" \
  "https://{ALB_DNS}/portal"
# 기대값: .../oauth2/authorize?...&identity_provider=Okta&... 포함
```

---

## 트러블슈팅

| 증상 | 원인 / 해결 |
|------|------------|
| Okta 로그인 후 `redirect_uri_mismatch` | Okta 앱의 Sign-in redirect URI가 Cognito의 `/oauth2/idpresponse`와 정확히 일치하는지 확인 (https, 후행 슬래시 없음) |
| Cognito 에러 `invalid_request: unauthorized client` | 배포가 IdP 전환 전이거나 App Client에 Okta가 연결 안 됨 → 스택 UPDATE_COMPLETE 확인 후 재시도 |
| `IdP에서 이메일 정보를 받지 못했습니다` 화면 | Okta 앱이 `email` 스코프/클레임을 반환하지 않음 → Okta 앱의 Sign On 정책 및 OpenID scope 확인 |
| Okta 로그인 화면이 아닌 Cognito 화면이 뜸 | `oktaIssuer` 등 컨텍스트 없이 배포됨 → 3단계 명령의 `-c okta*` 3개 모두 포함해 재배포 |
| Okta 로그인은 되는데 403/400 | Okta 앱 Assignments에 해당 사용자/그룹이 없음 → Okta 관리자에게 할당 요청 |
| Issuer 관련 배포 실패 | 2단계로 issuer URL 검증 (`/.well-known/openid-configuration`이 열리는 URL이어야 함) |

## 사용자 회수/차단

- **접근 차단**: Okta 앱 Assignments에서 사용자 제거 → 포털 재로그인 불가. **단, 이미 발급된 Virtual Key는 계속 동작**하므로 완전 차단하려면:
  1. LiteLLM Admin UI(`{GatewayUrl}/ui/`)에서 해당 키 삭제
  2. DynamoDB `llm-gw-gs-config` 테이블에서 `pk=USER#{email}` 항목 삭제
- **자동화 구현됨**: Okta Event Hook 기반 자동 오프보딩이 구현되어 있습니다.
  사용자 비활성화/정지/앱 어사인 해제 시 키가 자동 회수됩니다.
  설정 절차는 [okta-offboarding-guide.md](okta-offboarding-guide.md) 참고.
