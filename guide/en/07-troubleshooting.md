> 🇰🇷 [한국어](../07-troubleshooting.md) | 🇺🇸 English

# 07. Troubleshooting

Pitfalls encountered during the actual build, organized by symptom with fixes. In most
cases the error message points away from the real cause, so these tables can save you a
lot of time.

## Certificates / Connectivity

| Symptom | Cause | Fix |
|------|------|------|
| `ERR_CERT_AUTHORITY_INVALID` | The PC does not trust the self-signed certificate | Install the certificate as trusted ([04](04-claude-desktop.md)). Fully restart the app |
| Fails even after installing the certificate | Certificate SAN does not match the ALB hostname | Reissue with the SAN set to the actual ALB DNS. `*.elb.amazonaws.com` only matches one level |
| `/v1/models` connection test times out | Current network is not covered by the ALB CIDR restriction | Add the range to `allowedCidrs` and redeploy |

## Gateway / Models

| Symptom | Cause | Fix |
|------|------|------|
| Connection test finds 99 models, then 400 | The `bedrock/*` wildcard exposes models that cannot be invoked | List only specific models in model_list (remove the wildcard) |
| `provided model identifier is invalid` | Prefix not available in the region (e.g., apac 4.6) | `modelPrefix=global` |
| `JWT Auth is an enterprise only feature` | `enable_jwt_auth` is Enterprise-only | Use the bootstrap (Virtual Key) approach on OSS |

## Bootstrap / Claude Desktop

| Symptom | Cause | Fix |
|------|------|------|
| No bootstrap field in the settings UI | The UI only supports gateway settings | Deploy via managed configuration (`.reg`/plist) |
| App calls `.../portal/bootstrap/v1/models` | Bootstrap URL was entered in the gateway URL field | Bootstrap goes in the `bootstrapUrl` key, separate from the gateway base URL |
| Chat tab does not appear | Boolean set as integer/native type | Use the string `"true"` (dword/plist-boolean values are ignored) |
| Fails immediately with no login window | Cached refresh token from the previous AS | Force a fresh login by renaming the connector/config |
| "Managed by your organization" lock | Managed configuration present = UI locked by design | Pushing permissive policy keys unlocks the features. The lock itself cannot be removed |

## Okta OAuth

| Symptom | Cause | Fix |
|------|------|------|
| `redirect_uri_mismatch` | App calls back to `127.0.0.1`, but only `localhost` is registered in Okta | Register both (port 8123; also 8124 for MCP) |
| `Policy evaluation failed` 400 | The custom AS has no Access Policy allowing the app | Add a rule under Security→API→default→Access Policies (Scopes: Any) |
| Cognito login screen appears (not Okta) | Okta IdP not deployed / not switched over | Redeploy with `oktaIssuer` etc. specified |

## AgentCore MCP

| Symptom | Cause | Fix |
|------|------|------|
| `insufficient_scope` 403 | **Not a scope issue** — claim validation failed | Decode the token's `aud`/`cid` and compare against the authorizer ([05](05-agentcore-mcp.md)) |
| ↳ audience mismatch | The org AS cannot issue `aud` as the clientId | Use a custom AS + `allowedAudience: api://default` |
| ↳ client mismatch | `allowedClients` checks `client_id`, but Okta issues `cid` | Validate `cid` via `customClaims` |
| `grant was issued for another authorization server` | Cached refresh token from before the AS change | Rename the connector and redeploy |
| Accept/reject prompt on every tool call | No approval policy configured (default is ask) | `mcp_catalog.py autoapprove <name> all\|read` → restart the app |
| External MCP rejects our token (401) | Hosted MCPs like Notion only accept their own OAuth | Register with `add-external` to use the service's OAuth (internal tokens won't work) |
| External MCP login passes silently | The browser already has a session with that service | Normal — the login window appears when signed out |
| `text content blocks must be non-empty` after a tool call | Bedrock rejects the empty text+thinking blocks in the tool_use turn | Include the sanitize hook (below) |

## Empty Content Block 400 (After Tool Calls)

**Symptom**: The MCP tool is invoked and responds normally, but the next turn — the one
receiving the result — fails with a `text content blocks must be non-empty` 400.

**Cause**: Claude Desktop builds the tool_use turn history like this —
```
assistant: [ {empty text}, {empty thinking, no signature}, {tool_use} ]
```
Bedrock Converse rejects empty text and empty thinking blocks. LiteLLM's `modify_params`
catches a single empty text block but misses this **combination**.

**Fix**: The custom pre-call hook `litellm/sanitize_hook.py` strips empty
text/thinking/redacted_thinking blocks from every message. It is injected into the official
image via a base64 env var; the key detail is that it must be **written to the same directory
as the config (`/tmp`)** so LiteLLM can find the callback module (any other path fails
startup with `Could not find module file`).

## Diagnostic Tools

| Tool | Purpose |
|------|------|
| LiteLLM container logs (`/ecs/llm-gw-gs/litellm`) | Bedrock requests/responses, auth errors |
| Claude Desktop logs (macOS `~/Library/Logs/Claude-3p/main.log`) | MCP/OAuth flow under the `custom3p-mcp` tag |
| AgentCore `exceptionLevel: DEBUG` | Details on authorizer claim validation failures |
| Token claim decoding | Check `aud`/`cid`/`scp` — identify the cause of insufficient_scope |

> Temporary debug logging (`LITELLM_LOG=DEBUG`) writes user request bodies to the logs —
> be sure to remove it after diagnosis.
