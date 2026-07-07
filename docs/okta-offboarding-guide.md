# Okta Event Hook 자동 오프보딩 가이드

Okta에서 사용자가 **비활성화/정지**되거나 게이트웨이 앱의 **어사인이 해제**되면
LiteLLM Virtual Key를 자동으로 회수하는 기능입니다.
**SCIM 없이** (플랜 업그레이드 없이) SCIM deprovisioning과 같은 효과를 냅니다.

## 동작 방식

```
Okta 이벤트 발생 (deactivate / suspend / 앱 어사인 해제)
  → Okta Event Hook이 API Gateway로 POST (Authorization 헤더 인증)
  → Lambda (llm-gw-gs-okta-events):
      ├─ LiteLLM /user/info로 대상 사용자의 모든 키 조회
      ├─ /key/delete로 키 전부 삭제 (즉시 API 접근 차단)
      ├─ DynamoDB 캐시(USER#{email}) 삭제
      └─ LiteLLM 사용자 레코드는 사용량 감사를 위해 보존
```

처리 대상 이벤트:

| Okta 이벤트 | 처리 |
|-------------|------|
| `user.lifecycle.deactivate` | 무조건 키 회수 |
| `user.lifecycle.suspend` | 무조건 키 회수 |
| `user.lifecycle.delete.initiated` | 무조건 키 회수 |
| `application.user_membership.remove` | 대상 앱이 `OKTA_APP_LABEL`(기본: "LLM Gateway Key Portal")과 일치할 때만 회수 |

> 앱 이름이 다르면 `lib/stacks/portal-stack.ts`의 `OKTA_APP_LABEL` 값을
> Okta 앱의 display name과 정확히 일치하도록 수정 후 재배포하세요.

## Okta 등록 절차 (1회)

1. **웹훅 URL과 시크릿 확인** (배포 출력):
   ```bash
   # URL
   aws cloudformation describe-stacks --stack-name LlmGatewayStackV2 --region ap-northeast-2 \
     --query "Stacks[0].Outputs[?OutputKey=='KeyPortalUrl']" --output text
   # 시크릿 값
   aws secretsmanager get-secret-value --secret-id llm-gw-gs/okta-webhook-secret \
     --region ap-northeast-2 --query SecretString --output text
   ```

2. **Okta Admin → Workflow → Event Hooks → Create Event Hook**:

   | 필드 | 값 |
   |------|-----|
   | Name | `LLM Gateway Offboarding` (자유) |
   | URL | 배포 출력의 `OktaEventHookUrl` |
   | Authentication field | `Authorization` |
   | Authentication secret | 위에서 조회한 시크릿 값 |
   | Subscribe to events | `User deactivated`, `User suspended`, `User deleted`, `User removed from application membership` |

3. **Verify** 클릭 — Okta가 GET 챌린지를 보내고 Lambda가 자동 응답합니다 (원타임 검증).

4. **테스트**: 테스트 사용자를 앱에서 어사인 해제 → CloudWatch 로그
   (`/aws/lambda/llm-gw-gs-okta-events`)에서 회수 로그 확인 → 해당 사용자의
   Virtual Key로 API 호출 시 401 확인.

## 보안 설계

- 웹훅 엔드포인트는 API Gateway로 노출 (Okta는 고정 IP가 아니므로 CIDR 제한 불가).
  대신 40자 랜덤 시크릿을 Authorization 헤더로 검증하며, 타이밍 공격에 안전한
  `hmac.compare_digest`로 비교합니다.
- 인증 실패 시 401, 처리 오류 시에도 200을 반환해 Okta의 불필요한 재시도를 막고
  오류는 CloudWatch 로그로 추적합니다.
- Lambda는 VPC 내부에서 NAT EIP를 통해 LiteLLM(ALB)을 호출합니다.

## 한계와 보완

- Event Hook은 **최선 노력(at-least-once) 전달**입니다. Okta 장애 등으로 이벤트가
  유실될 극단적 경우를 대비하려면 주기적 리컨실(하루 1회 Okta 어사인 목록과
  LiteLLM 사용자 대조) Lambda를 추가할 수 있습니다.
- 이미 진행 중인 스트리밍 응답은 끊기지 않습니다 (다음 요청부터 401).
- 사용자가 Okta에서 **재활성화**되면 포털에 다시 로그인하는 것만으로
  새 키가 자동 발급됩니다 (별도 복구 절차 불필요).
