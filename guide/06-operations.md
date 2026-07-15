> 🇰🇷 한국어 | 🇺🇸 [English](en/06-operations.md)

# 06. 운영

## 사용자 라이프사이클

| 이벤트 | 관리자 작업 | 결과 |
|--------|-------------|------|
| **온보딩** | Okta `llm-gateway-users` 그룹에 추가 | 사용자가 앱 로그인 시 키 자동 발급(JIT) |
| **오프보딩** | 그룹에서 제거 (또는 계정 비활성화) | Event Hook이 수 초 내 키 회수 |
| **재활성화** | 그룹에 다시 추가 | 재로그인 시 새 키 자동 발급 |
| **키 유출 대응** | LiteLLM UI에서 키 삭제 + DynamoDB `USER#{email}` 삭제 | 다음 로그인 시 재발급 |

관리자가 키를 직접 만들거나 배포할 일은 없습니다. 신원의 원천은 Okta 그룹입니다.

## 예산 / 사용량

- Virtual Key 발급 시 사용자별 예산이 자동 설정됩니다 (기본 월 $1,000, 30일 주기 리셋).
  값은 `lib/config/constants.ts`의 `BUDGET`에서 조정.
- 예산 소진 시 호출이 거부되고, 주기 리셋 시 자동 복구. 급하면 관리자가 LiteLLM UI에서 상향.
- 사용량/키 관리 UI: `{GatewayUrl}/ui/` (master key로 로그인, Secrets Manager
  `llm-gw-gs/litellm-master-key`).

## 모니터링

- CloudWatch 대시보드 `llm-gw-gs-operations` (ECS CPU/메모리, ALB 요청/5xx/지연)
- 알람: ECS CPU>80%, ALB 5xx>10/5분 → SNS `llm-gw-gs-alerts`
- 로그: `/ecs/llm-gw-gs/litellm` (게이트웨이), `/aws/lambda/llm-gw-gs-*` (포털/오프보딩)

## SCIM 도입 판단

**결론: 대부분 불필요.** 현재 구성이 SCIM의 실질 효과를 라이선스 없이 제공합니다.

| SCIM 기능 | 이 구성의 대응 | 판정 |
|-----------|----------------|------|
| 사용자 생성 | 첫 로그인 JIT (`/user/new` 멱등) | 동일 효과 |
| 사용자 비활성화 | Event Hook 즉시 회수 + bootstrap 재인증 차단 (이중화) | 동일 효과 |
| 속성 동기화 | 로그인 시마다 email 갱신 | 준실시간 |
| 그룹→팀 매핑 | 미구현 (그룹 클레임 JIT로 확장 가능) | 요구 시 |

SCIM이 정당한 경우: "로그인 없이도 즉시 반영"이 감사 요건으로 문서화되고, 팀 단위
사전 프로비저닝이 정책일 때. 이 경우에도 Okta LCM + (LiteLLM 팀 SCIM은) Enterprise
라이선스가 **양쪽 다** 필요하므로 별도 품의 사안입니다.

## LiteLLM Enterprise 도입 판단

기능이 아니라 **책임 소재**가 결정 요인입니다:

| 질문 | Yes면 |
|------|-------|
| 운영자 2명+이고 master key 공유가 정책 위반인가? | Enterprise (Admin UI SSO/RBAC) |
| 권한 변경 이력이 감사 제출 대상인가? | Enterprise (Audit Logs) |
| 게이트웨이 장애 시 벤더 SLA가 품의 요건인가? | Enterprise (지원) |
| 위 셋 다 아니고 기능만 필요한가? | **OSS 유지** — 팀/오프보딩/온보딩 전부 현재 구조로 커버 |

참고: LiteLLM의 **JWT 직접 인증(`enable_jwt_auth`)은 Enterprise 전용**입니다. OSS에서
켜면 전체 인증이 차단되므로, 이 구성은 앱 네이티브 OIDC를 bootstrap(Virtual Key) 방식으로
구현했습니다 — Enterprise 없이 동작합니다.

## 병행 스택 정리

원본 블루프린트 스택과 병행 배포한 경우 VPC/NAT/Aurora가 이중으로 과금됩니다.
불필요해지면 구 스택을 삭제하세요.
