> 🇰🇷 [한국어](../README.md) | 🇺🇸 English

# Claude Desktop × LLM Gateway Setup Guide

An enterprise setup guide for connecting Claude Desktop to an internal LLM gateway
(Amazon Bedrock) with nothing but Okta SSO — including AgentCore agents as tools.
**No AWS CLI, no developer mode, no manual key entry.**

> This document captures what we learned during an actual build-out and verification.
> Refer to the parent repository for the code; use this guide for concepts, procedures, and pitfalls.

---

## What This Setup Solves

| Problem | Solution |
|------|------|
| End users don't know how to use the AWS CLI/SSO | **Launch the app → sign in with Okta → done** (bootstrap auto-configuration) |
| API keys must be copied and entered by hand | The server issues and injects per-user keys automatically (users never even see a key) |
| Rolling out configuration to ~300 PCs | One identical `.reg`/`.mobileconfig` for all PCs; policy is managed centrally on the server |
| Revoking access for leavers/transfers | Automatic revocation within seconds via Okta Event Hook (no SCIM needed) |
| Using internal agents (AgentCore) | MCP connectors auto-deployed as org-managed tools |

---

## Table of Contents

| # | Document | Contents |
|---|------|------|
| 01 | [Architecture](01-architecture.md) | Overall diagram, auth paths (primary/backup), components |
| 02 | [Deployment](02-deployment.md) | Prerequisites, CDK deployment, context parameters |
| 03 | [Okta Setup](03-okta-setup.md) | OIDC app creation, Event Hook offboarding, group management |
| 04 | [Claude Desktop Rollout](04-claude-desktop.md) | Bootstrap approach, `.reg`/`.mobileconfig`, tab/feature policies |
| 05 | [MCP Connectors](05-agentcore-mcp.md) | Catalog approach (no redeploys), group filters, AgentCore/external SaaS, tool approval policies |
| 06 | [Operations](06-operations.md) | On/offboarding, budgets & usage, monitoring, the SCIM decision |
| 07 | [Troubleshooting](07-troubleshooting.md) | Real-world pitfalls and fixes (must-read) |

---

## Three-Minute Summary

**Primary path (Claude Desktop Bootstrap)** — for ~300 end users
1. IT deploys a single minimal `.reg` (or `.mobileconfig`) to each PC — just the bootstrap server address + Okta info
2. The user launches Claude Desktop → signs in with Okta via the browser
3. The app automatically receives and applies a personal Virtual Key + gateway settings from the bootstrap server
4. Ready to use. Chat/Cowork/Code tabs and internal AgentCore agents are all active

**Backup path (web portal)** — for environments without bootstrap support (older app versions, Claude Code CLI)
- Open the portal in a browser → sign in with Okta → copy and paste the key and settings from the page

In both paths, Okta is the source of identity, and behind the gateway everything is the same: LiteLLM → Amazon Bedrock.

---

## Verification Status

- Primary bootstrap path: verified end-to-end on real hardware (macOS, Claude Desktop 1.18286.0)
- Okta Event Hook offboarding: group removal → key revoked within 1 second, confirmed
- MCP connectors: AgentCore weather agent (Okta) + Notion hosted MCP (external OAuth) — connection, group filters, and auto-approval confirmed
- Deployment region: ap-northeast-2 (changeable via context)

## License & Attribution

This project is a fork of AWS Samples'
[claude-code-bedrock-enterprise-blueprint](https://github.com/aws-samples/sample-aws-kr-enterprise/tree/main/ai-ml/claude-code-bedrock-enterprise-blueprint)
(MIT-0). It reuses the original's CDK stack structure and LiteLLM gateway concept,
while redesigning user authentication as Okta self-service.
