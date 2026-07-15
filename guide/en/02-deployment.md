> 🇰🇷 [한국어](../02-deployment.md) | 🇺🇸 English

# 02. Deployment

## Prerequisites

- AWS account + **Bedrock Claude model access approved** in the deployment region (Opus 4.8 / Sonnet 4.6 / Haiku 4.5)
- Node.js 18+, AWS CDK v2
- Okta tenant (permission to create OIDC apps) — [03. Okta Setup](03-okta-setup.md)
- Certificate for ALB HTTPS: proper domain + ACM certificate (recommended), or a self-signed import

## Base Deployment (primary: bootstrap)

```bash
npm install
npx cdk bootstrap   # first time only

npx cdk deploy LlmGatewayStackV2 \
  -c certificateArn=arn:aws:acm:{REGION}:{ACCOUNT}:certificate/{CERT_ID} \
  -c allowedCidrs={CORP_CIDR_1},{CORP_CIDR_2} \
  -c oktaIssuer=https://{your-org}.okta.com \
  -c desktopOidcClientId={Okta Native App Client ID}
```

What this deploys:
- VPC / ALB / ECS (LiteLLM) / Aurora / DynamoDB
- Key Portal Lambda + `/portal/bootstrap` endpoint (enabled when desktopOidcClientId is set)
- Okta Event Hook receiver API + Lambda (automatic offboarding)
- **Cognito is not created** (web portal disabled by default)

## Context Parameters

| Context key | Default | Description |
|-------------|---------|-------------|
| `region` | `ap-northeast-2` | Deployment region |
| `certificateArn` | (none) | ALB HTTPS certificate. HTTP-only if omitted (demo use only) |
| `allowedCidrs` | (none = open to all) | ALB inbound allowed CIDRs, comma-separated |
| `oktaIssuer` | (none) | Okta tenant issuer URL |
| **`desktopOidcClientId`** | (none) | **Primary**: Okta Native App Client ID for bootstrap. Enables `/portal/bootstrap` when set |
| `enableWebPortal` | `false` | **Backup**: enable the Cognito web portal |
| `oktaClientId` / `oktaClientSecret` | (none) | Okta Web App credentials for the web portal (only with `enableWebPortal=true`) |
| `modelPrefix` | `global` | Bedrock inference profile prefix (`us`/`eu`/`apac`/`global`) |
| `litellmImageTag` | `main-v1.83.14-stable` | LiteLLM container image tag |

> MCP connectors are managed via the **DDB catalog** (`scripts/mcp_catalog.py`), not
> deployment context. Add/revoke connectors, assign groups, and set approval policies
> without redeploying — [05. MCP Connectors](05-agentcore-mcp.md).

## Deploying the Backup Path (web portal) Too

```bash
npx cdk deploy LlmGatewayStackV2 \
  ... base context ... \
  -c enableWebPortal=true \
  -c oktaClientId={Okta Web App Client ID} \
  -c oktaClientSecret={Okta Web App Secret}
```

> The Web App is **separate** from the Native App used for bootstrap. If you only use
> bootstrap, you don't need to deploy the web portal.

## Deployment Outputs

| Output | Purpose |
|--------|---------|
| `GatewayUrl` | LiteLLM gateway base URL |
| `BootstrapUrl` | The `bootstrapUrl` value for the Claude Desktop `.reg` file |
| `OktaEventHookUrl` | URL to register as the Okta Event Hook |
| `OktaWebhookSecretArn` | Event Hook auth secret (Secrets Manager) |
| `KeyPortalUrl` | Web portal URL (when the backup path is enabled) |

## Importing a Self-Signed Certificate (when you have no proper certificate)

```bash
# Create a self-signed certificate with the ALB DNS name as CN/SAN, then import to ACM
openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
  -keyout gateway.key -out gateway.crt \
  -subj "/CN={ALB_DNS}" \
  -addext "subjectAltName=DNS:{ALB_DNS},DNS:*.{REGION}.elb.amazonaws.com"

aws acm import-certificate --certificate fileb://gateway.crt \
  --private-key fileb://gateway.key --region {REGION}
```

> **Caution**: the SAN must match the actual ALB hostname. The wildcard
> `*.elb.amazonaws.com` matches only one level, so it does not cover names of the form
> `xxx.{region}.elb.amazonaws.com`. User PCs must install the certificate as trusted —
> see [04. Claude Desktop Distribution](04-claude-desktop.md).

## Region and Model Notes

The Claude 4.6/4.5 generation is available **only via the `global.` inference profile**
in most regions (e.g., there is no `apac.` 4.6 profile in ap-northeast-2), hence the
default prefix is `global`. `/v1/models` exposes only the three specified models (no
wildcards — for governance and compatibility with the Claude Desktop connection test).
