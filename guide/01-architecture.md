> 🇰🇷 한국어 | 🇺🇸 [English](en/01-architecture.md)

# 01. 아키텍처

## 전체 구성

```
┌─────────────────┐
│ Claude Desktop  │  ← 일반 사용자 PC (Windows/macOS)
│  (일반 사용자)   │
└────────┬────────┘
         │ ① Okta 로그인 (PKCE, 시스템 브라우저)
         │ ② GET /portal/bootstrap  (Bearer: Okta 토큰)
         ▼
┌─────────────────────────────────────────────┐
│ ALB (HTTPS, 사내 CIDR 제한)                    │
│  ├─ /portal/*   → Key Portal Lambda           │
│  └─ /v1/*, /*   → ECS Fargate (LiteLLM)        │
└───────┬──────────────────────┬────────────────┘
        │                      │
        ▼                      ▼
┌───────────────┐    ┌──────────────────────┐
│ Key Portal    │    │ LiteLLM Proxy (ECS)   │
│ Lambda        │    │  ├─ Virtual Key 인증   │
│ ├─ Okta 검증   │    │  ├─ 사용자별 예산/추적  │
│ ├─ 키 발급     │───▶│  └─ Bedrock 라우팅     │
│ └─ 설정 JSON   │    └──────────┬────────────┘
└───────────────┘               │ VPC Endpoint (PrivateLink)
        │                        ▼
        │              ┌──────────────────┐
        │              │ Amazon Bedrock   │  (Claude Opus/Sonnet/Haiku)
        │              └──────────────────┘
        │
        ▼ 사용자/키 캐시
┌───────────────┐        ┌────────────────────────────┐
│ DynamoDB      │        │ Okta Event Hook → API GW →   │
│ (키 캐시)      │        │ Lambda (자동 오프보딩)         │
│ Aurora        │        └────────────────────────────┘
│ (LiteLLM DB)  │
└───────────────┘

별도 경로: Claude Desktop ──MCP(Okta OAuth)──▶ AgentCore Gateway ──▶ 사내 에이전트
```

## 인증 경로: 주력과 백업

| | **주력** — Claude Desktop Bootstrap | **백업** — 웹 포털 |
|---|---|---|
| 사용자 경험 | 앱 실행 → Okta 로그인 → 끝 | 브라우저 로그인 → 키 복사 → 붙여넣기 |
| 인증 체인 | 앱 → Okta 직접(PKCE) → `/portal/bootstrap` → 자동 적용 | 브라우저 → Cognito Hosted UI → Okta → 키 화면 |
| Cognito | **불필요** | 필요 (`-c enableWebPortal=true`일 때만 생성) |
| 키 노출 | 사용자가 키를 보지 않음 | 화면에 표시(복사) |
| 배포물 | `.reg`/`.mobileconfig` 1개 | 없음 (URL 안내만) |
| 대상 | Claude Desktop 1.10270.0+ | 구버전 앱, Claude Code CLI, 비상용 |

> **왜 두 경로인가**: bootstrap은 최신 Claude Desktop의 "3P" 기능이라 구버전·CLI에서는
> 안 됩니다. 웹 포털은 브라우저만 있으면 되는 폴백입니다. 둘 다 신원은 Okta, 키는 동일한
> LiteLLM Virtual Key라 병행·전환이 자유롭습니다.

## 왜 Cognito가 주력 경로엔 없는가

- **웹 포털(백업)**: 서버가 OAuth 코드를 교환해야 하는 전통적 웹앱 구조라 Cognito가
  브로커로 유용합니다. Lambda에 Okta 자격증명을 넣지 않고 토큰 검증을 AWS 관리형에 위임.
- **bootstrap(주력)**: 앱이 스스로 PKCE 플로우를 수행하므로 브로커가 불필요합니다.
  Lambda가 Okta userinfo로 토큰을 직접 검증합니다.

파트너에게 설명할 때: "주력 경로는 앱이 Okta와 직접 통신하고, Cognito는 브라우저
폴백 경로에만 쓰인다."

## 구성 요소

| 구성 요소 | 역할 | 노출 |
|-----------|------|------|
| ALB | 진입점. `/portal/*`→Lambda, 그 외→LiteLLM | 인터넷-페이싱 + 사내 CIDR 화이트리스트 |
| LiteLLM (ECS Fargate) | Virtual Key 인증, 예산/사용량 추적, Bedrock 라우팅 | ALB 뒤 프라이빗 |
| Key Portal Lambda | bootstrap·웹 포털·인증서 다운로드·오프보딩 로직 | ALB `/portal/*` 경로 |
| Okta Events Lambda | Event Hook 수신 → 키 자동 회수 | API Gateway (별도, 인터넷) |
| DynamoDB | 사용자별 Virtual Key 캐시 | 프라이빗 |
| Aurora Serverless v2 | LiteLLM 사용량/예산 DB | 격리 서브넷 |
| Amazon Bedrock | Claude 모델 (Opus/Sonnet/Haiku) | VPC Endpoint |

## 인증서 관련

ALB는 HTTPS를 쓰며, 자체서명 인증서를 쓰는 경우 사용자 PC에 인증서 신뢰 설치가
선행되어야 합니다(앱이 TLS를 검증하므로). **프로덕션에서는 정식 도메인 + ACM 인증서를
강력 권장**합니다 — 그러면 사용자 쪽 인증서 작업이 완전히 사라집니다.
