# 03. Okta 설정

이 구성은 Okta에 최대 3가지를 만듭니다. 쓰는 경로에 따라 필요한 것만 만드세요.

| Okta 객체 | 용도 | 필요 시점 |
|-----------|------|-----------|
| **Native App** (OIDC) | 주력 bootstrap + AgentCore MCP 로그인 | 항상 (주력) |
| **Web App** (OIDC) | 백업 웹 포털 | `enableWebPortal=true`일 때만 |
| **Event Hook** | 자동 오프보딩 | 권장 (전 경로 공통) |
| **그룹** `llm-gateway-users` | 접근 제어 + 온/오프보딩 단위 | 항상 |

---

## 1. 그룹 생성

Directory → Groups → Add Group → `llm-gateway-users`.
게이트웨이를 쓸 사용자를 이 그룹에 넣습니다. 아래 앱들의 Assignment도 이 그룹으로 합니다.

> **중요**: 앱 어사인은 개인이 아닌 **그룹 단위**로 하세요. 실측 결과 개인 앱 어사인
> 해제 이벤트는 전송되지 않았고 **그룹 제거 이벤트는 즉시 전송**됐습니다. 온/오프보딩을
> 그룹 멤버십으로 관리하는 것이 300명 규모의 정석입니다.

## 2. Native App 생성 (주력)

Applications → Create App Integration:
- Sign-in method: **OIDC**
- Application type: **Native Application** (PKCE, client secret 없음)

설정:
| 항목 | 값 |
|------|-----|
| Grant type | **Authorization Code** + **Refresh Token** |
| Sign-in redirect URIs | `http://localhost:8123/callback`, `http://127.0.0.1:8123/callback` (**둘 다**) |
| (AgentCore MCP도 쓰면) | `http://127.0.0.1:8124/callback` 추가 |
| Assignments | `llm-gateway-users` 그룹 |

저장 후 **Client ID** 복사 → 배포의 `-c desktopOidcClientId=`에 사용.

> **redirect URI 함정**: 앱이 콜백을 `127.0.0.1`로 받는데 Okta에 `localhost`만
> 등록하면 `redirect_uri_mismatch`가 납니다. 둘 다 등록하세요.

## 3. Issuer 확인

```bash
curl -s https://{your-org}.okta.com/.well-known/openid-configuration | head
```
`"issuer"` 값이 열리면 그게 org authorization server issuer입니다.
404면 `/oauth2/default`를 붙여 시도(custom AS). AgentCore MCP 연동에는 custom AS가
필요합니다 — [05. AgentCore MCP 연동](05-agentcore-mcp.md) 참고.

## 4. Web App 생성 (백업 웹 포털 쓸 때만)

- Application type: **Web Application**, Grant: Authorization Code
- Sign-in redirect URI: 배포 출력 `OktaRedirectUri`
  (`https://{prefix}.auth.{region}.amazoncognito.com/oauth2/idpresponse`)
- Client ID/Secret → `-c oktaClientId=` / `-c oktaClientSecret=`

## 5. Event Hook — 자동 오프보딩 (권장)

Okta에서 사용자가 비활성화되거나 그룹에서 빠지면 게이트웨이 키를 자동 회수합니다.
**모든 Okta 플랜에 포함**되며 SCIM 라이선스가 필요 없습니다.

### 등록 절차

1. 배포 출력에서 URL·시크릿 확인:
   ```bash
   # 시크릿
   aws secretsmanager get-secret-value --secret-id llm-gw-gs/okta-webhook-secret \
     --region {REGION} --query SecretString --output text
   ```
2. Okta Admin → Workflow → **Event Hooks** → Create Event Hook:
   | 필드 | 값 |
   |------|-----|
   | URL | 배포 출력 `OktaEventHookUrl` |
   | Authentication field | `Authorization` (헤더 **이름**) |
   | Authentication secret | 위에서 조회한 시크릿 값 |
3. 구독 이벤트 (검색창에 `membership`으로 찾기):
   - `User removed from group membership` (`group.user_membership.remove`) ← 그룹 기반 오프보딩
   - `User deactivated` / `User suspended` / `User deleted` (계정 단위)
4. **Verify** 클릭 → Lambda가 챌린지에 자동 응답 → 등록 완료

### 동작

- 그룹 제거/비활성화 이벤트 수신 → LiteLLM 키 삭제 + DynamoDB 캐시 삭제 (수 초 내)
- 필터: `OKTA_GROUP_LABEL`(기본 `llm-gateway-users`)과 일치하는 그룹만 회수
  (다른 그룹 제거는 무시 — 오회수 방지)
- 재추가 시: 사용자가 다시 로그인하면 새 키 자동 발급 (관리자 개입 불필요)

## SCIM은 필요한가?

**대부분 불필요합니다.** 온보딩(첫 로그인 JIT) + 오프보딩(Event Hook)으로 SCIM의
실질 효과를 라이선스 없이 달성합니다. SCIM이 정당한 경우는 "로그인 없이도 즉시 반영"이
감사 요건으로 문서화되고, 팀 단위 사전 프로비저닝이 정책일 때뿐입니다. 자세한 판단
기준은 [06. 운영](06-operations.md).
