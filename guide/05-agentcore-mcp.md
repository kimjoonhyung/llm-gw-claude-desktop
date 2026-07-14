# 05. MCP 커넥터 (AgentCore 에이전트 · 외부 SaaS)

Claude Desktop에 **조직 관리 MCP 커넥터**를 배포합니다. bootstrap 응답의
`managedMcpServers`로 내려가므로 사용자가 URL을 입력하지 않아도 커넥터 목록에 자동으로
나타나고, **재배포 없이** DynamoDB 카탈로그(CLI)만 수정하면 추가/회수됩니다.

## 카탈로그 방식 (재배포 불필요)

MCP 목록은 config 테이블(`pk=MCP#<name>, sk=CATALOG`)에 저장되고, bootstrap Lambda가
매 요청 시 읽어 **사용자 Okta 그룹으로 필터링**해 응답에 넣습니다.

```bash
scripts/mcp_catalog.py list
scripts/mcp_catalog.py enable <name> / disable <name> / remove <name>
scripts/mcp_catalog.py autoapprove <name> [all|read|ask]
```

- **추가**: 아래 두 유형 중 하나로 등록
- **회수**: `disable`(카탈로그 제외) 또는 `remove`(삭제) → **다음 앱 재시작 시** 커넥터 사라짐. PC는 안 건드림
- **그룹 필터**: 카탈로그 항목의 `allowed_groups`와 사용자 그룹의 교집합만 노출. 비우면 전원 허용

> 그룹 필터가 동작하려면 Okta custom AS에 `groups` 스코프+클레임이 있어야 하고,
> bootstrap 로그인 스코프에 `groups`가 포함돼야 합니다 — [03. Okta 설정](03-okta-setup.md).

---

## 유형 A — AgentCore 게이트웨이 (Okta 인증)

사내 AgentCore Runtime 에이전트를 노출합니다. Okta로 인증이 통일됩니다.

```
Claude Desktop ──MCP(Okta OAuth)──▶ AgentCore Gateway ──▶ AgentCore Runtime (에이전트)
```

등록 (OAuth는 bootstrap과 같은 Okta Native App 재사용, custom AS로 로그인):
```bash
scripts/mcp_catalog.py add <name> \
  https://{gateway-id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp \
  {OKTA_NATIVE_CLIENT_ID} https://{org}.okta.com {group}
```
콜백 포트 8124 — Native App redirect URI에 `http://127.0.0.1:8124/callback` 추가 필요.

### AgentCore Gateway authorizer 구성 (함정 2개 — 필독)

> 아래 둘 중 하나라도 어긋나면 게이트웨이가 **`insufficient_scope`(403)**를 반환합니다.
> **이름과 달리 scope 문제가 아닙니다** — AgentCore는 서명 검증 후의 모든 클레임 검증
> 실패를 이 에러 하나로 뭉뚱그립니다.

**함정 1 — audience**: Okta **org AS**(`https://{org}.okta.com`)의 access token은
`aud`를 클라이언트 ID로 발급할 수 없습니다. 반드시 **custom AS**(`/oauth2/default`)를
쓰고 게이트웨이 `allowedAudience`를 `api://default`로 설정합니다. custom AS에는 해당
앱을 허용하는 **Access Policy**가 있어야 합니다(없으면 로그인 단계에서
`Policy evaluation failed` 400).

**함정 2 — 클레임 이름(`cid` vs `client_id`)**: 게이트웨이의 `allowedClients`는 토큰의
`client_id` 클레임을 검사하는데 **Okta는 `cid`에 담습니다**. AND 조건이라 aud가 맞아도
실패합니다. `allowedClients` 대신 `customClaims`로 `cid`를 검증합니다:

```json
{"customJWTAuthorizer": {
  "discoveryUrl": "https://{org}.okta.com/oauth2/default/.well-known/openid-configuration",
  "allowedAudience": ["api://default"],
  "customClaims": [{
    "inboundTokenClaimName": "cid",
    "inboundTokenClaimValueType": "STRING",
    "authorizingClaimMatchValue": {
      "claimMatchValue": {"matchValueString": "{NATIVE_CLIENT_ID}"},
      "claimMatchOperator": "EQUALS"
    }
  }]
}}
```
authorizer는 boto3 `bedrock-agentcore-control` `update_gateway`로 수정. `exceptionLevel:
DEBUG`를 켜면 이후 클레임 검증 실패 원인이 구체적으로 보입니다.

---

## 유형 B — 외부 SaaS MCP (서비스 자체 OAuth)

Notion·GitHub 등 이미 호스티드 MCP 서버가 있는 SaaS. 사용자가 **그 서비스에 각자
로그인**합니다(Okta 아님). 서버가 DCR(동적 클라이언트 등록)을 지원하면 client ID 미리
등록 불필요.

```bash
scripts/mcp_catalog.py add-external notion https://mcp.notion.com/mcp https://mcp.notion.com
```

동작: Claude Desktop이 그 서비스의 OAuth authorization server로 직접 로그인 →
브라우저 세션이 이미 있으면 동의 화면 없이 통과. Okta·AgentCore를 거치지 않습니다.

> **AgentCore로 감쌀 수도 있으나**(3LO outbound OAuth), 그건 해당 SaaS의 **Public OAuth
> 앱**이 필요합니다. Notion은 개인 계정에서 Public OAuth 앱 생성이 막히는 경우가 있어,
> 호스티드 MCP에 직접 붙는 유형 B가 더 단순합니다. 또한 Notion 호스티드 MCP는 internal
> integration 토큰(`ntn_...`)을 거부하고 자체 OAuth만 받습니다.

---

## 도구 승인 정책 (자동 승인)

기본적으로 Claude Desktop은 MCP 도구 호출마다 "수락/거절"을 묻습니다. 카탈로그에서
서버별로 승인 정책을 내려주면(관리 구성) 자동 승인할 수 있습니다.

```bash
scripts/mcp_catalog.py autoapprove notion all    # 전 도구 자동 승인  {"*":"allow"}
scripts/mcp_catalog.py autoapprove notion read   # 읽기류만 자동, 쓰기·삭제는 확인
scripts/mcp_catalog.py autoapprove notion ask    # 정책 제거 → 매번 확인 (기본)
```

값은 도구명(또는 `*`/`read_*` 와일드카드)→`allow`/`ask`/`blocked` 매핑입니다.

> **보안 균형**: `all`은 편하지만 쓰기·삭제 도구까지 무조건 실행합니다. 문서를 수정할 수
> 있는 SaaS(Notion 등)는 실사용에서 **`read`**를 권장 — 읽기만 자동, 변경은 사용자 확인.
> 참고로 Microsoft 365의 send류 도구는 `allow`를 넣어도 `ask`로 강제되는 안전장치가 있습니다.

정책은 bootstrap 응답으로 내려가므로 **앱 재시작 시 반영**됩니다.

---

## 도구 호출 후 400 (빈 콘텐츠 블록)

MCP 도구는 정상 호출되는데 **다음 턴**에서 `text content blocks must be non-empty` 400이
날 수 있습니다. Claude Desktop이 tool_use 턴 이력에 빈 text+빈 thinking 블록을 담는데
Bedrock Converse가 거부하기 때문입니다. 이 구성에는 LiteLLM 커스텀 훅
(`litellm/sanitize_hook.py`)이 포함되어 자동 제거합니다 —
[07. 트러블슈팅](07-troubleshooting.md).

## 검증 순서

1. 커넥터가 목록에 뜨는가 (bootstrap `managedMcpServers` + 그룹 필터)
2. Connect → 로그인 통과 (유형 A: Okta Access Policy / 유형 B: 서비스 OAuth)
3. (유형 A) 게이트웨이가 토큰 수락 (audience + cid 함정)
4. 도구 호출 → 응답 (자동 승인 정책 / sanitize 훅)

막히면 [07. 트러블슈팅](07-troubleshooting.md)의 증상별 표를 참고하세요.
