# Claude Desktop 앱 네이티브 OIDC 가이드 (수동 키 입력 제거)

"Claude Desktop on 3P" 기능을 이용해, 사용자가 **키 입력 없이 Okta 로그인만으로**
게이트웨이를 사용하는 구성입니다. 등록 페이지(Virtual Key) 방식과 병행 동작합니다.

> 상태: Claude Desktop on 3P는 2026-07 기준 베타 → GA 직후 단계입니다.
> 파일럿 그룹으로 실증 후 확산을 권장합니다. 앱 버전 1.6889.0+ 필요.
> 공식 문서: https://claude.com/docs/cowork/3p/gateway

## 두 방식 비교

| | 등록 페이지 + Virtual Key (기존) | 앱 네이티브 OIDC (이 가이드) |
|---|---|---|
| 사용자 행동 | 포털 로그인 → 키 복사 → 앱에 붙여넣기 (1회) | 앱에서 Okta 로그인만 |
| 배포 | settings 안내만 | 관리자 구성 파일 배포 (macOS `.mobileconfig` / Windows `.reg`) |
| 인증 갱신 | 키 영구 (오프보딩 훅으로 회수) | refresh token 자동 갱신, Okta 세션이 곧 인증 |
| 오프보딩 | Event Hook으로 키 삭제 | Okta에서 차단되면 토큰 갱신 불가 (더 즉각적) + 기존 Event Hook 병행 |
| 사용량 추적 | Virtual Key 단위 | JWT의 email 클레임 단위 (user_id_upsert로 자동 생성) |

## 1단계 — Okta: 네이티브 앱용 OIDC 클라이언트 생성

기존 포털용 Web App과 **별도로** 하나 더 만듭니다:

1. Applications → Create App Integration
   - Sign-in method: **OIDC**
   - Application type: **Native Application** (PKCE 사용, client secret 없음)
2. 설정:
   - Grant type: **Authorization Code** + **Refresh Token**
   - Sign-in redirect URIs: `http://localhost:{PORT}/callback`
     (PORT는 3단계의 `redirectPort`와 일치 — 예: `http://localhost:8123/callback`)
   - Assignments: `llm-gateway-users` 그룹
3. **Client ID** 복사 (secret은 없음 — PKCE라 불필요)

## 2단계 — 게이트웨이: JWT 인증 활성화 재배포

```bash
npx cdk deploy LlmGatewayStackV2 \
  ... 기존 컨텍스트 ... \
  -c oktaIssuer=https://{your-org}.okta.com \
  -c desktopOidcClientId={1단계의 Native App Client ID} \
  --require-approval never
```

이것이 LiteLLM에 다음을 설정합니다:

```yaml
general_settings:
  enable_jwt_auth: true
  litellm_jwtauth:
    public_key_url: https://{your-org}.okta.com/oauth2/v1/keys  # Okta JWKS
    audience: {desktopOidcClientId}   # 미고정 시 테넌트의 아무 토큰이나 통과 — 필수
    user_id_jwt_field: email
    user_email_jwt_field: email
    user_id_upsert: true              # 첫 호출 시 사용자 자동 생성 (포털 JIT와 동일)
```

Virtual Key(`sk-...`)와 JWT는 **동시에 유효**합니다 — LiteLLM이 토큰 형태로 구분합니다.

## 3단계 — Claude Desktop 관리 구성 배포

Windows (`.reg`) / macOS (`.mobileconfig`)로 전 PC에 배포. 핵심 값:

```json
{
  "inferenceGatewayBaseUrl": "https://{ALB_DNS}",
  "inferenceCredentialKind": "interactive",
  "inferenceGatewayOidc": {
    "issuer": "https://{your-org}.okta.com",
    "clientId": "{desktopOidcClientId}",
    "redirectPort": 8123,
    "scopes": "openid profile email offline_access",
    "bearerTokenType": "id_token"
  },
  "inferenceModels": [
    "global.anthropic.claude-opus-4-8",
    "global.anthropic.claude-sonnet-4-6",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0"
  ]
}
```

> 정확한 파일 포맷(레지스트리 키 경로 등)은 공식 문서의 배포 섹션을 따르세요.
> 앱 내 Developer → Configure Third-Party Inference… 에서 구성을 만들어 내보내는
> 방법이 가장 안전합니다 (베타 기간 중 스키마 변경 가능성).

## 4단계 — 검증

1. 구성이 배포된 PC에서 Claude Desktop 실행 → 브라우저로 Okta 로그인 창이 뜨는지
2. 로그인 후 대화 시도 → 응답 확인
3. 게이트웨이 확인: LiteLLM Admin UI Users 탭에 해당 이메일이 자동 생성됐는지
4. 오프보딩 확인: Okta에서 그룹 제거 → 토큰 만료 후 재인증 실패하는지

## 주의사항

- **자체서명 인증서**: OIDC 모드에서도 앱이 게이트웨이 TLS를 검증하므로,
  인증서 신뢰 설치(포털 3단계) 또는 정식 ACM 인증서가 여전히 필요합니다.
  OIDC 전환을 계기로 정식 도메인 + ACM 인증서로 가는 것을 강력 권장합니다.
- **JWT 사용자의 예산**: `user_id_upsert`로 생성된 사용자는 키 기반 예산이 아닌
  사용자 레벨 예산을 따릅니다. LiteLLM의 기본 internal user 예산 설정
  (`litellm_settings.max_internal_user_budget` 등)을 함께 검토하세요.
- **Event Hook과의 관계**: 기존 오프보딩 훅은 Virtual Key만 삭제합니다.
  JWT 경로는 Okta 세션 차단으로 자연 차단되므로 별도 회수가 불필요하지만,
  병행 기간에는 두 경로 모두 오프보딩이 동작하는지 확인하세요.
