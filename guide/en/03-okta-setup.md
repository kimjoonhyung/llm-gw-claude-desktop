> 🇰🇷 [한국어](../03-okta-setup.md) | 🇺🇸 English

# 03. Okta Setup

This setup creates up to three objects in Okta. Create only what the paths you use require.

| Okta object | Purpose | Needed when |
|-------------|---------|-------------|
| **Native App** (OIDC) | Primary bootstrap + AgentCore MCP login | Always (primary) |
| **Web App** (OIDC) | Backup web portal | Only with `enableWebPortal=true` |
| **Event Hook** | Automatic offboarding | Recommended (shared across all paths) |
| **Group** `llm-gateway-users` | Access control + on/offboarding unit | Always |

---

## 1. Create the Group

Directory → Groups → Add Group → `llm-gateway-users`.
Add every gateway user to this group. Use this group for the app Assignments below as well.

> **Important**: assign apps at the **group level**, not per user. In practice,
> unassigning an app from an individual user produced no event, while removing a user
> from a group **sent an event immediately**. Managing on/offboarding through group
> membership is the standard approach at a 300-user scale.

## 2. Create the Native App (primary)

Applications → Create App Integration:
- Sign-in method: **OIDC**
- Application type: **Native Application** (PKCE, no client secret)

Settings:
| Field | Value |
|-------|-------|
| Grant type | **Authorization Code** + **Refresh Token** |
| Sign-in redirect URIs | `http://localhost:8123/callback`, `http://127.0.0.1:8123/callback` (**both**) |
| (If also using AgentCore MCP) | Add `http://127.0.0.1:8124/callback` |
| Assignments | `llm-gateway-users` group |

After saving, copy the **Client ID** → use it as `-c desktopOidcClientId=` at deploy time.

> **redirect URI pitfall**: the app receives the callback on `127.0.0.1`, so if only
> `localhost` is registered in Okta you'll get `redirect_uri_mismatch`. Register both.

## 3. Confirm the Issuer

```bash
curl -s https://{your-org}.okta.com/.well-known/openid-configuration | head
```
If this resolves, the `"issuer"` value is the org authorization server issuer.
If you get a 404, try appending `/oauth2/default` (custom AS). The AgentCore MCP
integration requires a custom AS — see [05. AgentCore MCP Integration](05-agentcore-mcp.md).

## 4. Create the Web App (only if using the backup web portal)

- Application type: **Web Application**, Grant: Authorization Code
- Sign-in redirect URI: the deployment output `OktaRedirectUri`
  (`https://{prefix}.auth.{region}.amazoncognito.com/oauth2/idpresponse`)
- Client ID/Secret → `-c oktaClientId=` / `-c oktaClientSecret=`

## 5. Event Hook — Automatic Offboarding (recommended)

When a user is deactivated in Okta or removed from the group, their gateway key is
revoked automatically. **Included in every Okta plan** — no SCIM license required.

### Registration Steps

1. Get the URL and secret from the deployment outputs:
   ```bash
   # Secret
   aws secretsmanager get-secret-value --secret-id llm-gw-gs/okta-webhook-secret \
     --region {REGION} --query SecretString --output text
   ```
2. Okta Admin → Workflow → **Event Hooks** → Create Event Hook:
   | Field | Value |
   |-------|-------|
   | URL | Deployment output `OktaEventHookUrl` |
   | Authentication field | `Authorization` (the header **name**) |
   | Authentication secret | The secret value retrieved above |
3. Subscribed events (search for `membership`):
   - `User removed from group membership` (`group.user_membership.remove`) ← group-based offboarding
   - `User deactivated` / `User suspended` / `User deleted` (account-level)
4. Click **Verify** → the Lambda answers the challenge automatically → registration complete

### Behavior

- On a group-removal/deactivation event → the LiteLLM key and DynamoDB cache entry are deleted (within seconds)
- Filter: only revokes for the group matching `OKTA_GROUP_LABEL` (default `llm-gateway-users`)
  (removals from other groups are ignored — prevents accidental revocation)
- On re-adding: the user logs in again and a new key is issued automatically (no admin intervention)

## Do You Need SCIM?

**Usually not.** Onboarding (JIT at first login) plus offboarding (Event Hook) delivers
the practical effect of SCIM without the license. SCIM is justified only when
"immediate propagation even without a login" is a documented audit requirement, or when
team-level pre-provisioning is policy. For detailed decision criteria, see
[06. Operations](06-operations.md).
