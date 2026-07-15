> 🇰🇷 [한국어](../06-operations.md) | 🇺🇸 English

# 06. Operations

## User Lifecycle

| Event | Admin action | Result |
|--------|-------------|------|
| **Onboarding** | Add to the Okta `llm-gateway-users` group | Key is issued automatically (JIT) when the user signs in to the app |
| **Offboarding** | Remove from the group (or deactivate the account) | Event Hook revokes the key within seconds |
| **Reactivation** | Add back to the group | A new key is issued automatically on next sign-in |
| **Key leak response** | Delete the key in the LiteLLM UI + delete `USER#{email}` in DynamoDB | Reissued on next sign-in |

Admins never create or distribute keys by hand. The Okta group is the source of truth for identity.

## Budgets / Usage

- A per-user budget is set automatically when a Virtual Key is issued (default $1,000/month, resetting on a 30-day cycle).
  Adjust the values in `BUDGET` in `lib/config/constants.ts`.
- When the budget is exhausted, calls are rejected; access is restored automatically at the cycle reset. In a pinch, an admin can raise the limit in the LiteLLM UI.
- Usage/key management UI: `{GatewayUrl}/ui/` (sign in with the master key, stored in Secrets Manager
  `llm-gw-gs/litellm-master-key`).

## Monitoring

- CloudWatch dashboard `llm-gw-gs-operations` (ECS CPU/memory, ALB requests/5xx/latency)
- Alarms: ECS CPU>80%, ALB 5xx>10/5min → SNS `llm-gw-gs-alerts`
- Logs: `/ecs/llm-gw-gs/litellm` (gateway), `/aws/lambda/llm-gw-gs-*` (portal/offboarding)

## Should You Adopt SCIM?

**Verdict: usually unnecessary.** The current setup delivers SCIM's practical benefits without the license.

| SCIM feature | What this setup does | Verdict |
|-----------|----------------|------|
| User creation | JIT on first sign-in (`/user/new` is idempotent) | Same effect |
| User deactivation | Immediate revocation via Event Hook + bootstrap re-auth blocking (redundant controls) | Same effect |
| Attribute sync | Email refreshed on every sign-in | Near real-time |
| Group→team mapping | Not implemented (extensible via group-claim JIT) | On demand |

SCIM is justified when "propagation without a sign-in" is a documented audit requirement, or when team-level pre-provisioning is policy. Even then, it requires **both** Okta LCM and (for LiteLLM team SCIM) an Enterprise license, so it warrants a separate procurement decision.

## Should You Adopt LiteLLM Enterprise?

The deciding factor is **accountability**, not features:

| Question | If yes |
|------|-------|
| Do you have 2+ operators, and does sharing the master key violate policy? | Enterprise (Admin UI SSO/RBAC) |
| Must permission-change history be submitted for audits? | Enterprise (Audit Logs) |
| Is a vendor SLA for gateway outages a procurement requirement? | Enterprise (support) |
| None of the above — you only need the features? | **Stay on OSS** — teams/offboarding/onboarding are all covered by the current architecture |

Note: LiteLLM's **direct JWT auth (`enable_jwt_auth`) is Enterprise-only**. Enabling it
on OSS blocks all authentication, so this setup implements app-native OIDC via the
bootstrap (Virtual Key) approach — it works without Enterprise.

## Cleaning Up the Parallel Stack

If you deployed alongside the original blueprint stack, you are paying twice for VPC/NAT/Aurora.
Delete the old stack once it is no longer needed.
