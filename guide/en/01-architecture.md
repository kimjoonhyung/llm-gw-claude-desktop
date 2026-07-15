> 🇰🇷 [한국어](../01-architecture.md) | 🇺🇸 English

# 01. Architecture

## Overall Layout

```
┌─────────────────┐
│ Claude Desktop  │  ← End-user PC (Windows/macOS)
│  (end users)    │
└────────┬────────┘
         │ ① Okta login (PKCE, system browser)
         │ ② GET /portal/bootstrap  (Bearer: Okta token)
         ▼
┌─────────────────────────────────────────────┐
│ ALB (HTTPS, restricted to corporate CIDRs)    │
│  ├─ /portal/*   → Key Portal Lambda           │
│  └─ /v1/*, /*   → ECS Fargate (LiteLLM)        │
└───────┬──────────────────────┬────────────────┘
        │                      │
        ▼                      ▼
┌───────────────┐    ┌──────────────────────┐
│ Key Portal    │    │ LiteLLM Proxy (ECS)   │
│ Lambda        │    │  ├─ Virtual Key auth   │
│ ├─ Okta verify │    │  ├─ Per-user budgets/  │
│ ├─ Key issuance│───▶│  │   usage tracking    │
│ └─ Config JSON │    │  └─ Bedrock routing    │
└───────────────┘    └──────────┬────────────┘
        │                        │ VPC Endpoint (PrivateLink)
        │                        ▼
        │              ┌──────────────────┐
        │              │ Amazon Bedrock   │  (Claude Opus/Sonnet/Haiku)
        │              └──────────────────┘
        │
        ▼ User/key cache
┌───────────────┐        ┌────────────────────────────┐
│ DynamoDB      │        │ Okta Event Hook → API GW →   │
│ (key cache)   │        │ Lambda (automatic offboarding)│
│ Aurora        │        └────────────────────────────┘
│ (LiteLLM DB)  │
└───────────────┘

Separate path: Claude Desktop ──MCP (Okta OAuth)──▶ AgentCore Gateway ──▶ internal agents
```

## Authentication Paths: Primary and Backup

| | **Primary** — Claude Desktop Bootstrap | **Backup** — Web Portal |
|---|---|---|
| User experience | Launch app → Okta login → done | Log in via browser → copy key → paste |
| Auth chain | App → Okta directly (PKCE) → `/portal/bootstrap` → applied automatically | Browser → Cognito Hosted UI → Okta → key screen |
| Cognito | **Not required** | Required (created only with `-c enableWebPortal=true`) |
| Key exposure | Users never see the key | Displayed on screen (copy) |
| Distribution artifact | One `.reg`/`.mobileconfig` file | None (just share the URL) |
| Audience | Claude Desktop 1.10270.0+ | Older app versions, Claude Code CLI, emergencies |

> **Why two paths**: bootstrap is a "3P" feature of recent Claude Desktop builds, so it
> doesn't work with older versions or the CLI. The web portal is a fallback that only
> needs a browser. Both use Okta for identity and issue the same LiteLLM Virtual Key,
> so users can freely run them in parallel or switch between them.

## Why Cognito Is Absent from the Primary Path

- **Web portal (backup)**: it's a traditional web app where the server must exchange the
  OAuth code, so Cognito is useful as a broker. Token verification is delegated to an
  AWS-managed service instead of embedding Okta credentials in the Lambda.
- **bootstrap (primary)**: the app performs the PKCE flow itself, so no broker is needed.
  The Lambda verifies the token directly against Okta userinfo.

When explaining to partners: "In the primary path the app talks to Okta directly;
Cognito is used only in the browser fallback path."

## Components

| Component | Role | Exposure |
|-----------|------|----------|
| ALB | Entry point. `/portal/*` → Lambda, everything else → LiteLLM | Internet-facing + corporate CIDR whitelist |
| LiteLLM (ECS Fargate) | Virtual Key auth, budget/usage tracking, Bedrock routing | Private, behind the ALB |
| Key Portal Lambda | bootstrap, web portal, certificate download, and offboarding logic | ALB `/portal/*` path |
| Okta Events Lambda | Receives Event Hooks → automatically revokes keys | API Gateway (separate, internet-facing) |
| DynamoDB | Per-user Virtual Key cache | Private |
| Aurora Serverless v2 | LiteLLM usage/budget DB | Isolated subnet |
| Amazon Bedrock | Claude models (Opus/Sonnet/Haiku) | VPC Endpoint |

## Certificate Notes

The ALB uses HTTPS. If you use a self-signed certificate, it must first be installed as
trusted on user PCs (the app validates TLS). **For production, a proper domain plus an
ACM certificate is strongly recommended** — it eliminates all certificate work on the
user side.
