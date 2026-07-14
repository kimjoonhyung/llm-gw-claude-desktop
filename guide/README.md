# Claude Desktop × LLM Gateway 구축 가이드

Okta SSO만으로 일반 사용자가 Claude Desktop을 사내 LLM 게이트웨이(Amazon Bedrock)에
연결하고, AgentCore 에이전트까지 도구로 사용하게 만드는 엔터프라이즈 구성 가이드입니다.
**AWS CLI·개발자 모드·수동 키 입력이 전혀 필요 없습니다.**

> 이 문서는 실제 구축·검증 과정에서 확인한 내용을 정리한 것입니다.
> 코드 구현은 상위 저장소를, 개념·절차·함정은 이 가이드를 참고하세요.

---

## 이 구성이 해결하는 것

| 문제 | 해결 |
|------|------|
| 일반 사용자가 AWS CLI/SSO를 쓸 줄 모름 | **앱 실행 → Okta 로그인 → 끝** (bootstrap 자동 설정) |
| API 키를 수동으로 복사·입력해야 함 | 서버가 사용자별 키를 자동 발급·주입 (사용자는 키를 보지도 않음) |
| 300명 규모 PC에 설정 배포 | 전 PC 동일한 `.reg`/`.mobileconfig` 1개, 정책은 서버에서 중앙 관리 |
| 퇴사자/이동자 접근 회수 | Okta Event Hook으로 수 초 내 자동 회수 (SCIM 불필요) |
| 사내 에이전트(AgentCore) 활용 | MCP 커넥터로 조직 관리 도구 자동 배포 |

---

## 목차

| # | 문서 | 내용 |
|---|------|------|
| 01 | [아키텍처](01-architecture.md) | 전체 구성도, 인증 경로(주력/백업), 구성 요소 |
| 02 | [배포](02-deployment.md) | 사전 요구사항, CDK 배포, 컨텍스트 파라미터 |
| 03 | [Okta 설정](03-okta-setup.md) | OIDC 앱 생성, Event Hook 오프보딩, 그룹 운영 |
| 04 | [Claude Desktop 배포](04-claude-desktop.md) | bootstrap 방식, `.reg`/`.mobileconfig`, 탭·기능 정책 |
| 05 | [MCP 커넥터](05-agentcore-mcp.md) | 카탈로그 방식(재배포 없음)·그룹 필터·AgentCore/외부 SaaS·도구 승인 정책 |
| 06 | [운영](06-operations.md) | 온보딩/오프보딩, 예산·사용량, 모니터링, SCIM 판단 |
| 07 | [트러블슈팅](07-troubleshooting.md) | 실전에서 마주친 함정과 해결 (필독) |

---

## 3분 요약

**주력 경로 (Claude Desktop Bootstrap)** — 300명 일반 사용자 대상
1. IT가 PC에 최소 `.reg`(또는 `.mobileconfig`) 1개 배포 — bootstrap 서버 주소 + Okta 정보만
2. 사용자가 Claude Desktop 실행 → 브라우저로 Okta 로그인
3. 앱이 bootstrap 서버에서 개인 Virtual Key + 게이트웨이 설정을 자동 수신·적용
4. 바로 사용. Chat/Cowork/Code 탭, 사내 AgentCore 에이전트까지 활성

**백업 경로 (웹 포털)** — bootstrap 미지원 환경(구버전 앱, Claude Code CLI)
- 브라우저로 포털 접속 → Okta 로그인 → 화면의 키·설정을 복사해 붙여넣기

두 경로 모두 신원의 원천은 Okta이고, 게이트웨이 뒤는 LiteLLM → Amazon Bedrock으로 동일합니다.

---

## 검증 상태

- 주력 bootstrap 경로: 실기기(macOS, Claude Desktop 1.18286.0)에서 end-to-end 검증 완료
- Okta Event Hook 오프보딩: 그룹 제거 → 1초 내 키 회수 확인
- MCP 커넥터: AgentCore 날씨 에이전트(Okta) + Notion 호스티드 MCP(외부 OAuth) 연결·그룹 필터·자동 승인 확인
- 배포 리전: ap-northeast-2 (컨텍스트로 변경 가능)

## 라이선스·출처

이 프로젝트는 AWS Samples의
[claude-code-bedrock-enterprise-blueprint](https://github.com/aws-samples/sample-aws-kr-enterprise/tree/main/ai-ml/claude-code-bedrock-enterprise-blueprint)
(MIT-0)에서 파생되었습니다. 원본의 CDK 스택 구조와 LiteLLM 게이트웨이 컨셉을 재사용하되,
사용자 인증을 Okta 셀프서비스로 재설계했습니다.
