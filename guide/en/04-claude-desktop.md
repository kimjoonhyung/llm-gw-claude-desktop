> 🇰🇷 [한국어](../04-claude-desktop.md) | 🇺🇸 English

# 04. Claude Desktop Deployment

The primary approach is **Bootstrap** — after Okta sign-in, the app automatically receives its configuration from the server.
Users never enter a gateway address or a key.

> Version requirements: Bootstrap requires Claude Desktop **1.10270.0+**; app-native OIDC requires 1.6889.0+.
> Both are part of the "Claude Desktop on 3P" feature (official docs target GA on 2026-07-09).

## Deployment Files

You deploy **one identical file** to every PC. It contains no gateway address, keys, or model list
(the server delivers those in the bootstrap response). So when policy changes, you only fix the server —
no PC redeployment needed.

### Windows (`.reg`)

Registry path: `HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Claude`
(use `HKEY_CURRENT_USER\SOFTWARE\Policies\Claude` for self-service installs)

```reg
Windows Registry Editor Version 5.00

[HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Claude]
"bootstrapEnabled"="true"
"bootstrapUrl"="https://{ALB_DNS}/portal/bootstrap"
"bootstrapOidc"="{\"issuer\":\"https://{your-org}.okta.com\",\"clientId\":\"{NATIVE_CLIENT_ID}\",\"redirectPort\":8123,\"scopes\":\"openid profile email offline_access\"}"
```

Template: `templates/claude-desktop-bootstrap.reg`

### macOS (`.mobileconfig` / MDM)

Located at `/Library/Managed Preferences/com.anthropic.claudefordesktop.plist`.
Deploy via MDM (Jamf/Kandji, etc.) or install as a profile. Template:
`templates/claude-desktop-bootstrap.mobileconfig`

## Value Encoding Rules (avoid common mistakes)

| Rule | Description |
|------|------|
| **All values are strings** | Booleans are `"true"`, numbers are `"3600"`. On Windows, use REG_SZ, not dword |
| **Objects are a single string** | `bootstrapOidc` is one REG_SZ containing the entire escaped JSON. Do not split it into subkeys (the most common mistake the official docs warn about) |
| **When settings apply** | Read once at app startup → after deploying, fully quit (Cmd+Q) and relaunch |

> **Safest approach**: build the configuration in the app under **Developer → Configure Third-Party Inference…**
> and Export it — the schema will be exactly right (guards against schema changes during the beta).

## Trusting a Self-Signed Certificate (when no proper certificate is available)

The app validates the gateway's TLS certificate, so include certificate installation in your deployment script.
The portal serves the certificate at `/portal/cert`.

**Windows (Administrator PowerShell)**:
```powershell
curl.exe -sk https://{ALB_DNS}/portal/cert -o $env:TEMP\gw.crt
Import-Certificate -FilePath $env:TEMP\gw.crt -CertStoreLocation Cert:\LocalMachine\Root
```

**macOS**:
```bash
curl -sk https://{ALB_DNS}/portal/cert -o ~/gw.crt
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ~/gw.crt
```

> Moving to a proper domain + ACM certificate eliminates this entire step.

## Tab and Feature Policies (centrally managed on the server)

Tabs (Chat/Cowork/Code) and feature flags are **best delivered via the bootstrap response**.
Even without putting them in the `.reg` file, the server distributes them, and policy changes only require a server-side edit.

| Policy key | Example value | Description |
|---------|---------|------|
| `chatTabEnabled` | `"true"` | Chat tab (disabled by default → must be set explicitly) |
| `coworkTabEnabled` | `"true"` | Cowork tab |
| `isClaudeCodeForDesktopEnabled` | `"true"` | Code tab |
| `isDesktopExtensionEnabled` | `"true"` | Allow installing extensions (.dxt/.mcpb) |
| `isLocalDevMcpEnabled` | `"true"` | Allow adding local MCP servers |

> **Booleans must be the string `"true"`**. A plist integer `1` or a native boolean is
> ignored (verified empirically).

## "Managed by your organization" UI Lock

When a managed configuration/bootstrap is present, the app enters "organization-managed" mode and locks the settings UI.
**This is by design** (to prevent regular employees from arbitrarily changing gateway settings), and there is no key
that only unlocks the UI. Instead, deliver the policy keys above permissively and all features open up. Individual
users who need full UI freedom must remove the managed configuration and configure manually (which breaks the
automatic bootstrap connection).
