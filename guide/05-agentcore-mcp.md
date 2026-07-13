# 05. AgentCore MCP 연동

사내 AgentCore Runtime의 에이전트를 Claude Desktop의 **조직 관리 커넥터**로 노출합니다.
bootstrap 응답의 `managedMcpServers`로 중앙 배포되어, 사용자가 URL을 입력하지 않아도
커넥터 목록에 자동으로 나타납니다.

```
Claude Desktop ──MCP(Okta OAuth)──▶ AgentCore Gateway ──▶ AgentCore Runtime (에이전트)
```

## 배포

```bash
npx cdk deploy LlmGatewayStackV2 \
  ... 기존 컨텍스트 ... \
  -c mcpGatewayUrl=https://{gateway-id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp \
  -c mcpGatewayName={커넥터 표시 이름}
```

MCP OAuth는 bootstrap과 **같은 Okta Native App을 재사용**하되, **custom AS**
(`{issuer}/oauth2/default`)로 로그인합니다. 콜백 포트는 8124 — Native App의 redirect
URI에 `http://127.0.0.1:8124/callback` 추가가 필요합니다.

## AgentCore Gateway authorizer 구성 (함정 2개 — 필독)

> 아래 둘 중 하나라도 어긋나면 게이트웨이가 **`insufficient_scope`(403)**를 반환합니다.
> **이름과 달리 scope 문제가 아닙니다** — AgentCore는 서명 검증 후의 모든 클레임 검증
> 실패를 이 에러 하나로 뭉뚱그립니다.

### 함정 1 — audience

Okta **org AS**(`https://{org}.okta.com`)의 access token은 `aud`를 클라이언트 ID로
발급할 수 없습니다. 반드시 **custom AS**(`/oauth2/default`)를 쓰고 게이트웨이의
`allowedAudience`를 `api://default`로 설정합니다.

custom AS에는 해당 앱을 허용하는 **Access Policy**가 있어야 합니다. 없으면 로그인
단계에서 `Policy evaluation failed` 400이 납니다.
(Okta Admin → Security → API → default → Access Policies → 규칙의 Scopes를 `Any scopes`로)

### 함정 2 — 클레임 이름 (`cid` vs `client_id`)

게이트웨이의 `allowedClients`는 토큰의 **`client_id` 클레임**을 검사하는데, **Okta는
클라이언트 ID를 `cid` 클레임에 담습니다** (`client_id` 클레임 자체가 없음).
`allowedAudience`+`allowedClients`는 AND 조건이라 aud가 맞아도 항상 실패합니다.

**해결**: `allowedClients` 대신 `customClaims`로 `cid`를 검증합니다 (AWS 공식 Okta
워크숍 패턴):

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

> 대안: Okta custom AS에 `client_id` = `app.clientId` 커스텀 클레임을 추가하면
> `allowedClients`를 그대로 쓸 수 있습니다. 둘 중 하나만 하면 됩니다.

authorizer는 boto3로 수정합니다 (`bedrock-agentcore-control` `update_gateway`).
수정 후 `exceptionLevel: DEBUG`를 켜두면 이후 클레임 검증 실패 원인이 구체적으로 보입니다.

## 도구 호출 후 400 (빈 콘텐츠 블록)

MCP 도구는 정상 호출되는데 **그 다음 턴**에서
`text content blocks must be non-empty` 400이 날 수 있습니다. Claude Desktop이 tool_use
턴 이력에 **빈 text 블록 + 빈 thinking 블록**을 함께 담는데 Bedrock Converse가 이를
거부하기 때문입니다.

이 구성에는 LiteLLM 커스텀 pre-call 훅(`litellm/sanitize_hook.py`)이 포함되어 빈
text/thinking/redacted_thinking 블록을 자동 제거합니다. LiteLLM `modify_params`만으로는
이 조합이 정리되지 않습니다. 자세한 내용은 [07. 트러블슈팅](07-troubleshooting.md).

## 검증 순서

1. 커넥터가 목록에 뜨는가 (bootstrap `managedMcpServers` 반영)
2. Connect → Okta 로그인 통과 (Access Policy)
3. 게이트웨이가 토큰 수락 (audience + cid — 함정 2개)
4. 도구 호출 → 응답 (sanitize 훅)

각 단계는 독립적으로 실패할 수 있으니, 막히면 [07. 트러블슈팅](07-troubleshooting.md)의
증상별 표를 참고하세요.
