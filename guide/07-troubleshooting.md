# 07. 트러블슈팅

실제 구축에서 마주친 함정과 해결을 증상별로 정리했습니다. 대부분 에러 메시지가
실제 원인과 다르게 표시되므로 이 표가 시간을 크게 아껴줍니다.

## 인증서 / 연결

| 증상 | 원인 | 해결 |
|------|------|------|
| `ERR_CERT_AUTHORITY_INVALID` | 자체서명 인증서를 PC가 신뢰 안 함 | 인증서 신뢰 설치 ([04](04-claude-desktop.md)). 앱 완전 재시작 |
| 인증서 설치했는데도 실패 | 인증서 SAN이 ALB 호스트명과 불일치 | SAN을 실제 ALB DNS로 재발급. `*.elb.amazonaws.com`은 한 레벨만 매칭 |
| `/v1/models` 연결 테스트 타임아웃 | ALB CIDR 제한에 현재 네트워크 미포함 | `allowedCidrs`에 대역 추가 재배포 |

## 게이트웨이 / 모델

| 증상 | 원인 | 해결 |
|------|------|------|
| 연결 테스트가 99개 모델 발견 후 400 | `bedrock/*` 와일드카드가 호출 불가 모델까지 노출 | model_list에 지정 모델만 (와일드카드 제거) |
| `provided model identifier is invalid` | 리전에 없는 프리픽스(예: apac 4.6) | `modelPrefix=global` |
| `JWT Auth is an enterprise only feature` | `enable_jwt_auth`는 Enterprise 전용 | OSS는 bootstrap(Virtual Key) 방식 사용 |

## Bootstrap / Claude Desktop

| 증상 | 원인 | 해결 |
|------|------|------|
| 설정 UI에 bootstrap 필드 없음 | UI는 게이트웨이 설정만 지원 | 관리 구성(`.reg`/plist)으로 배포 |
| `.../portal/bootstrap/v1/models` 호출 | bootstrap URL을 게이트웨이 URL 필드에 입력 | bootstrap은 `bootstrapUrl` 키에, 게이트웨이 base URL과 구분 |
| Chat 탭 안 뜸 | boolean을 정수/native로 넣음 | 문자열 `"true"`로 (dword/plist-boolean 무시됨) |
| 로그인 창 없이 바로 실패 | 이전 AS의 refresh token 캐시 | 커넥터/설정 이름 변경으로 새 로그인 강제 |
| "조직에서 관리합니다" 잠금 | 관리 구성 존재 = 설계상 UI 잠금 | 정책 키를 허용적으로 내려주면 기능은 열림. 잠금 자체는 못 품 |

## Okta OAuth

| 증상 | 원인 | 해결 |
|------|------|------|
| `redirect_uri_mismatch` | 앱이 `127.0.0.1`로 콜백, Okta엔 `localhost`만 | 둘 다 등록 (8123, MCP는 8124도) |
| `Policy evaluation failed` 400 | custom AS에 앱 허용 Access Policy 없음 | Security→API→default→Access Policies에 규칙 추가 (Scopes: Any) |
| Cognito 로그인 화면이 뜸(Okta 아님) | Okta IdP 미배포/미전환 | `oktaIssuer` 등 지정해 재배포 |

## AgentCore MCP

| 증상 | 원인 | 해결 |
|------|------|------|
| `insufficient_scope` 403 | **scope 아님** — 클레임 검증 실패 | 토큰 `aud`/`cid` 디코딩해 authorizer와 대조 ([05](05-agentcore-mcp.md)) |
| ↳ audience 불일치 | org AS는 aud를 clientId로 못 발급 | custom AS + `allowedAudience: api://default` |
| ↳ client 불일치 | `allowedClients`는 `client_id` 검사, Okta는 `cid` | `customClaims`로 `cid` 검증 |
| `grant was issued for another authorization server` | AS 변경 전 refresh token 캐시 | 커넥터 이름 변경 후 재배포 |
| 도구 호출 후 `text content blocks must be non-empty` | tool_use 턴의 빈 text+thinking 블록을 Bedrock이 거부 | sanitize 훅 포함 (아래) |

## 빈 콘텐츠 블록 400 (도구 호출 후)

**증상**: MCP 도구는 정상 호출·응답하는데, 그 결과를 받은 다음 턴에서
`text content blocks must be non-empty` 400.

**원인**: Claude Desktop이 tool_use 턴 이력을 이렇게 만듭니다 —
```
assistant: [ {빈 text}, {빈 thinking, signature 없음}, {tool_use} ]
```
Bedrock Converse는 빈 text·빈 thinking을 거부합니다. LiteLLM `modify_params`는 빈 text
하나는 잡지만 이 **조합**은 못 잡습니다.

**해결**: 커스텀 pre-call 훅 `litellm/sanitize_hook.py`가 모든 메시지에서 빈
text/thinking/redacted_thinking 블록을 제거합니다. 공식 이미지에 base64 env로 주입되며,
LiteLLM이 콜백 모듈을 찾도록 **config와 같은 디렉토리(`/tmp`)에 기록**하는 것이 핵심
(다른 경로면 `Could not find module file` 로 기동 실패).

## 진단 도구

| 도구 | 용도 |
|------|------|
| LiteLLM 컨테이너 로그 (`/ecs/llm-gw-gs/litellm`) | Bedrock 요청/응답, 인증 에러 |
| Claude Desktop 로그 (macOS `~/Library/Logs/Claude-3p/main.log`) | `custom3p-mcp` 태그로 MCP/OAuth 흐름 |
| AgentCore `exceptionLevel: DEBUG` | authorizer 클레임 검증 실패 상세 |
| 토큰 클레임 디코딩 | `aud`/`cid`/`scp` 확인 — insufficient_scope 원인 판별 |

> 임시 디버그 로깅(`LITELLM_LOG=DEBUG`)은 사용자 요청 본문이 로그에 남으므로 진단 후
> 반드시 제거하세요.
