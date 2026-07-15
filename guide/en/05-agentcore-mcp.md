> 🇰🇷 [한국어](../05-agentcore-mcp.md) | 🇺🇸 English

# 05. MCP Connectors (AgentCore Agents · External SaaS)

Deploy **organization-managed MCP connectors** to Claude Desktop. They are delivered via
`managedMcpServers` in the bootstrap response, so connectors appear in the list automatically
without users entering URLs, and they can be added/revoked by editing the DynamoDB catalog (CLI)
**without redeployment**.

## Catalog Approach (no redeployment)

The MCP list is stored in the config table (`pk=MCP#<name>, sk=CATALOG`); the bootstrap Lambda
reads it on every request, **filters by the user's Okta groups**, and includes it in the response.

```bash
scripts/mcp_catalog.py list
scripts/mcp_catalog.py enable <name> / disable <name> / remove <name>
scripts/mcp_catalog.py autoapprove <name> [all|read|ask]
```

- **Add**: register as one of the two types below
- **Revoke**: `disable` (excluded from catalog) or `remove` (deleted) → the connector disappears **on the next app restart**. No PC changes needed
- **Group filter**: only the intersection of the catalog entry's `allowed_groups` and the user's groups is exposed. Empty means everyone is allowed

> For group filtering to work, the Okta custom AS must have a `groups` scope + claim,
> and the bootstrap login scopes must include `groups` — see [03. Okta Setup](03-okta-setup.md).

---

## Type A — AgentCore Gateway (Okta authentication)

Exposes in-house AgentCore Runtime agents. Authentication is unified through Okta.

```
Claude Desktop ──MCP(Okta OAuth)──▶ AgentCore Gateway ──▶ AgentCore Runtime (agents)
```

Registration (OAuth reuses the same Okta Native App as bootstrap, signing in via the custom AS):
```bash
scripts/mcp_catalog.py add <name> \
  https://{gateway-id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp \
  {OKTA_NATIVE_CLIENT_ID} https://{org}.okta.com {group}
```
Callback port is 8124 — you must add `http://127.0.0.1:8124/callback` to the Native App's redirect URIs.

### AgentCore Gateway Authorizer Configuration (2 pitfalls — must read)

> If either of the two items below is wrong, the gateway returns **`insufficient_scope` (403)**.
> **Despite the name, it is not a scope problem** — AgentCore lumps every claim-validation
> failure after signature verification into this single error.

**Pitfall 1 — audience**: Access tokens from the Okta **org AS** (`https://{org}.okta.com`)
cannot be issued with `aud` set to the client ID. You must use the **custom AS**
(`/oauth2/default`) and set the gateway's `allowedAudience` to `api://default`. The custom AS
must have an **Access Policy** allowing the app (otherwise sign-in fails with
`Policy evaluation failed` 400).

**Pitfall 2 — claim name (`cid` vs `client_id`)**: The gateway's `allowedClients` checks the
token's `client_id` claim, but **Okta puts it in `cid`**. Since the conditions are ANDed, this
fails even when the audience is correct. Validate `cid` via `customClaims` instead of
`allowedClients`:

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
Update the authorizer via boto3 `bedrock-agentcore-control` `update_gateway`. Enabling
`exceptionLevel: DEBUG` makes subsequent claim-validation failures show their specific cause.

---

## Type B — External SaaS MCP (the service's own OAuth)

SaaS products that already have a hosted MCP server, such as Notion or GitHub. Each user
**signs in to that service individually** (not Okta). If the server supports DCR (Dynamic
Client Registration), no client ID needs to be pre-registered.

```bash
scripts/mcp_catalog.py add-external notion https://mcp.notion.com/mcp https://mcp.notion.com
```

How it works: Claude Desktop signs in directly against that service's OAuth authorization
server → if a browser session already exists, it passes through without a consent screen.
Okta and AgentCore are not involved.

> **You could wrap it with AgentCore** (3LO outbound OAuth), but that requires a **Public
> OAuth app** for the SaaS. Notion sometimes blocks creating Public OAuth apps on personal
> accounts, so Type B — connecting directly to the hosted MCP — is simpler. Also note that
> Notion's hosted MCP rejects internal integration tokens (`ntn_...`) and accepts only its
> own OAuth.

---

## Tool Approval Policy (auto-approval)

By default, Claude Desktop asks "accept/decline" for every MCP tool call. You can enable
auto-approval by delivering a per-server approval policy from the catalog (managed configuration).

```bash
scripts/mcp_catalog.py autoapprove notion all    # auto-approve all tools  {"*":"allow"}
scripts/mcp_catalog.py autoapprove notion read   # auto-approve read-only tools; confirm writes/deletes
scripts/mcp_catalog.py autoapprove notion ask    # remove policy → confirm every time (default)
```

The value is a mapping of tool name (or `*`/`read_*` wildcards) → `allow`/`ask`/`blocked`.

> **Security trade-off**: `all` is convenient but unconditionally runs write and delete tools
> too. For SaaS that can modify documents (Notion, etc.), **`read`** is recommended in
> practice — reads are automatic, changes require user confirmation. Note that Microsoft 365's
> send-type tools have a safeguard that forces `ask` even if you set `allow`.

The policy is delivered via the bootstrap response, so it **takes effect on app restart**.

---

## 400 After Tool Calls (empty content blocks)

MCP tool calls may succeed, yet the **next turn** fails with a
`text content blocks must be non-empty` 400. Claude Desktop includes empty text + empty
thinking blocks in the tool_use turn history, which Bedrock Converse rejects. This setup
includes a LiteLLM custom hook (`litellm/sanitize_hook.py`) that strips them automatically —
see [07. Troubleshooting](07-troubleshooting.md).

## Verification Order

1. Does the connector appear in the list (bootstrap `managedMcpServers` + group filter)
2. Connect → sign-in succeeds (Type A: Okta Access Policy / Type B: the service's OAuth)
3. (Type A) Gateway accepts the token (audience + cid pitfalls)
4. Tool call → response (auto-approval policy / sanitize hook)

If you get stuck, consult the symptom table in [07. Troubleshooting](07-troubleshooting.md).
