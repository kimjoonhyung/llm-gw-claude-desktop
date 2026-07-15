> 🇰🇷 [한국어](README.md) | 🇺🇸 English

# LLM Gateway — Okta Self-Service Edition

> **Attribution**: This project is a fork of AWS Samples'
> [claude-code-bedrock-enterprise-blueprint](https://github.com/aws-samples/sample-aws-kr-enterprise/tree/main/ai-ml/claude-code-bedrock-enterprise-blueprint)
> (MIT-0 license). It reuses the original's CDK stack structure (Network/Database/Gateway/Monitoring)
> and LiteLLM-based gateway concept, and redesigns the items listed in the table below.
> Original copyright: Amazon.com, Inc. or its affiliates.

A redesign of the original blueprint for **end users who never touch the AWS CLI** (Claude Desktop / Claude Code).

> **📖 Setup & Operations Guide**: A step-by-step guide, organized for external sharing, lives in [`guide/`](guide/en/README.md).
> (Architecture · Deployment · Okta setup · Claude Desktop rollout · AgentCore MCP integration · Operations · Troubleshooting)
> This README is a developer-oriented summary of the repository; for an actual rollout, follow `guide/`.

## Authentication Paths: Primary and Backup

| | Primary — Claude Desktop Bootstrap (app-native OIDC) | Backup — Web Portal (`-c enableWebPortal=true`) |
|---|---|---|
| User experience | **Launch the app → sign in with Okta → done** (users never see or type a key) | Sign in to the portal in a browser → copy the key → paste into the app |
| Auth chain | App → Okta directly (PKCE) → `/portal/bootstrap` → Virtual Key applied automatically | Browser → **Cognito** Hosted UI → Okta → key page |
| Cognito | **Not required** | Required (created only when enabled) |
| Rollout artifact | `templates/claude-desktop-bootstrap.reg` (one identical file for all PCs) | None (just share the URL) |
| Audience | Claude Desktop 1.10270.0+ | Older app versions, Claude Code CLI, emergencies |

The primary path has been verified end-to-end on real hardware (macOS, app 1.18286.0).
Details: [guide/en/04-claude-desktop.md](guide/en/04-claude-desktop.md)

## Changes from the Original

| Item | Original blueprint | This version |
|------|----------------|---------|
| User authentication | `aws sso login` + local `apiKeyHelper` script | **Okta sign-in in the browser only** (self-service key portal) |
| Virtual Key acquisition | Local shell script calls the Token Service with SigV4 | **A Lambda portal issues keys in the cloud** — no scripts run on the user's machine |
| IdP integration | Direct IAM Identity Center login | **Okta OIDC → Amazon Cognito** (AWS managed auth service) |
| Key delivery | stdout (apiKeyHelper) | **Displayed on an authenticated HTTPS session page** (no email needed — see below) |
| LiteLLM image | `main-latest` (unstable) | **`main-v1.83.14-stable`** (latest stable tag, overridable via context) |
| Default region | `us-east-1` hardcoded | **`ap-northeast-2` by default** + set at deploy time with `-c region=...` |
| Model prefix | `us.` hardcoded | **Derived automatically from the region** (`ap-*` → `apac.`) + `-c modelPrefix=...` override |
| Claude configuration | Bedrock mode + AWS_PROFILE required | **Only `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`** — no AWS tooling at all |

### Why display on screen instead of sending email?

The requirement was "deliver the virtual key securely via email, but use an AWS authentication feature other than SES." Our review found:

- Sending arbitrary-body email from AWS ultimately requires SES (or an SNS email subscription), and **email is a delivery channel where the plaintext API key lingers in mailboxes and relay servers**, so it is not recommended from a security standpoint.
- Instead, we integrated **Amazon Cognito** (AWS's managed authentication feature) with Okta OIDC, so users can view the key **only on an HTTPS session page authenticated through Okta**. Since Okta has already verified email ownership, "securely deliver to the owner only" is achieved without sending any mail.
- If organizational policy mandates email notification, we recommend adding an SNS email subscription (a key-issuance notification only, without the key value).

## Architecture

```
User browser
  │
  ├─ ① Open the key portal (ALB /portal → Lambda)
  │     └─ ② Cognito Hosted UI → Okta OIDC login (users enter only their Okta password)
  │     └─ ③ Portal Lambda: auth code → token exchange → email verification
  │           ├─ Auto-create LiteLLM Internal User (/user/new, idempotent — ignored if it exists)
  │           ├─ DynamoDB cache lookup (USER#{email}/VIRTUAL_KEY)
  │           └─ Cache miss → LiteLLM /key/generate (budget set automatically) → cache
  │     └─ ④ Display Virtual Key + settings.json template on screen (copy button)
  │
Claude Desktop / Claude Code
  │
  └─ ⑤ ANTHROPIC_BASE_URL={ALB} + ANTHROPIC_AUTH_TOKEN={Virtual Key}
        └─ ALB → ECS Fargate (LiteLLM stable) → VPC Endpoint → Amazon Bedrock
              └─ Aurora PostgreSQL: per-user usage/budget tracking
```

CDK NestedStack layout:

```
LlmGatewayStack (Root)
├── Network    — VPC (2 AZ), Security Groups, VPC Endpoints (Bedrock, S3, DynamoDB)
├── Database   — Aurora Serverless v2 (PostgreSQL, 0.5~4 ACU)
├── Gateway    — ALB + ECS Fargate + LiteLLM (stable tag, config including model_list)
├── Portal     — Cognito User Pool (+Okta OIDC IdP) + key portal Lambda (ALB /portal routing)
└── Monitoring — DynamoDB (Audit/Config), CloudWatch Dashboard/Alarms, SNS
```

## Prerequisites

- AWS account + Bedrock Claude model access approval (Opus 4.6 / Sonnet 4.6 / Haiku 4.5 in the deployment region)
- Okta OIDC Web App (see "Okta Setup" below)
- ACM certificate (for ALB HTTPS — without one, the stack deploys over HTTP and is suitable for demos only)
- Node.js 18+, AWS CDK v2

## Deployment

```bash
npm install
npx cdk bootstrap   # once, on first use

# Default deployment (ap-northeast-2, apac. model prefix, stable LiteLLM)
npx cdk deploy LlmGatewayStack \
  -c certificateArn=arn:aws:acm:ap-northeast-2:123456789012:certificate/xxxx \
  -c allowedCidrs=10.0.0.0/8,203.0.113.0/24 \
  -c oktaIssuer=https://your-org.okta.com \
  -c oktaClientId=0oaXXXXXXXX \
  -c oktaClientSecret=XXXXXXXX
```

Context values you can set at deploy time:

| Context key | Default | Description |
|-------------|--------|------|
| `region` | `ap-northeast-2` | Deployment region (e.g. `-c region=us-east-1`) |
| `modelPrefix` | `global` | Bedrock inference profile prefix (`us`/`eu`/`apac`/`global`). Claude 4.6/4.5-generation models are only available via the `global` profile in most regions |
| `litellmImageTag` | `main-v1.83.14-stable` | LiteLLM GHCR image tag |
| `certificateArn` | (none) | ALB HTTPS certificate. If omitted, HTTP-only (demo use only) |
| `allowedCidrs` | (none = open to all) | Comma-separated list of CIDRs allowed inbound to the ALB (e.g. `-c allowedCidrs=10.0.0.0/8,203.0.113.0/24`) |
| `oktaIssuer` | (none) | Okta domain URL. If omitted, a Cognito-native user pool is used (testing only) |
| `desktopOidcClientId` | (none) | **Primary**: Okta Native App Client ID for the Claude Desktop bootstrap. When set together with `oktaIssuer`, enables `/portal/bootstrap` |
| `enableWebPortal` | `false` | **Backup**: enables the Cognito-based web portal (browser key issuance) |
| `oktaClientId` / `oktaClientSecret` | (none) | Okta Web App credentials for the web portal (required only when `enableWebPortal=true`) |

> If you don't want the Okta client secret in your shell history, put it in `context` in `cdk.json` or pass it via the `OKTA_CLIENT_SECRET` environment variable.

### Post-deployment Okta Setup (one-time)

See **[guide/en/03-okta-setup.md](guide/en/03-okta-setup.md)** for the full procedure. Summary:

1. Create an OIDC **Web Application** in Okta Admin (Grant: Authorization Code)
2. Register the `OktaRedirectUri` value from the deployment outputs as a Sign-in redirect URI
3. Redeploy with the Client ID/Secret: `-c oktaClientId=... -c oktaClientSecret=... -c oktaIssuer=...`
4. Under Assignments, assign the users/groups allowed to use the gateway

## User Onboarding (end users — no AWS CLI)

1. Open the **key portal URL** shared by the admin (deployment output `KeyPortalUrl`) in a browser.
2. You are redirected to the **Okta sign-in** page → sign in with your corporate account.
3. Copy the **Virtual Key** and the **settings.json configuration** shown on screen.
4. Paste into `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://{ALB_DNS}",
    "ANTHROPIC_AUTH_TOKEN": "sk-...",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "global.anthropic.claude-opus-4-6-v1",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "global.anthropic.claude-sonnet-4-6",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "global.anthropic.claude-haiku-4-5-20251001-v1:0"
  }
}
```

5. Launch Claude Code/Desktop. That's it — no `aws sso login`, no apiKeyHelper, no AWS profile setup.

Logging in to the portal again with the same account always returns the same key (DynamoDB cache). If a key leaks, an admin revokes it in the LiteLLM UI (`/ui/`) and deletes the DynamoDB cache entry (`USER#{email}`); a new key is issued on the next login.

## Budget/Usage Management

A per-user budget is set automatically when a Virtual Key is issued (`lib/config/constants.ts`):

- `MONTHLY_LIMIT_USD: 1000` — maximum budget per user
- `BUDGET_DURATION: '30d'` — budget reset period

The LiteLLM Admin UI (`{GatewayUrl}/ui/`, log in with the master key) provides per-user usage tracking and key management.

## Directory Layout

```
├── bin/app.ts                        # CDK entry point (handles region context)
├── lib/
│   ├── config/constants.ts           # Region/model prefix/LiteLLM tag/budgets
│   └── stacks/
│       ├── root-stack.ts             # NestedStack orchestration + Outputs
│       ├── network-stack.ts          # VPC, SG, VPC Endpoints
│       ├── database-stack.ts         # Aurora Serverless v2
│       ├── gateway-stack.ts          # ALB + ECS + LiteLLM (stable, inline config)
│       ├── portal-stack.ts           # Cognito(+Okta OIDC) + key portal Lambda
│       └── monitoring-stack.ts       # DynamoDB, CloudWatch, SNS
├── lambda/key-portal/
│   ├── handler.py                    # OAuth callback + Virtual Key issuance/display
│   └── tests/test_handler.py         # Unit tests (python3 -m unittest)
├── templates/claude-settings.json    # settings.json template for user rollout
├── cdk.json
└── package.json
```

## Testing

```bash
npx tsc --noEmit                                   # CDK type check
npx cdk synth --quiet                              # Template synthesis check
cd lambda/key-portal && python3 -m unittest discover -s tests   # Lambda unit tests
```

## Security Notes

- The Virtual Key never appears in email or logs; it is shown only on an authenticated session page (`Cache-Control: no-store`, `noindex`).
- The OAuth callback defends against CSRF with an HMAC-signed state cookie.
- The portal Lambda is exposed via **ALB path routing (`/portal`)**, not a public Function URL. A Function URL (NONE auth) requires a `Principal: *` resource policy, which security scanners (e.g. Amazon's internal Palisade) flag/block as a world-accessible Lambda. With the ALB approach, only the `elasticloadbalancing.amazonaws.com` service has invoke permission, and the `allowedCidrs` restriction applies to the portal as well.
- Key issuance is only possible after passing the Cognito token exchange (which requires the server-side client secret).
- The LiteLLM master key exists only in Secrets Manager and can be read only by the portal Lambda and the ECS task.
- In production, always set `certificateArn` so the ALB is exposed over HTTPS.
- **ALB inbound restriction**: list your corporate network/VPN CIDRs (comma-separated) in `allowedCidrs` so that ALB ports 80/443 are reachable only from those ranges. If omitted, 0.0.0.0/0 is open (demo only; synth prints a warning). Malformed CIDRs fail at the synth stage.
  - The portal Lambda calls LiteLLM from inside the VPC (private subnets) through a static NAT EIP, and that EIP is added to the allowlist automatically — no extra configuration needed.
  - The deployment output `NatEip` shows the actual EIP (useful for registering with external service firewalls, etc.).
  - Note: Okta/Cognito login traffic does not go through the ALB, so it works regardless of the CIDR restriction. In effect, **users can obtain a key from the portal anywhere, but can use the gateway only from allowed networks**.
