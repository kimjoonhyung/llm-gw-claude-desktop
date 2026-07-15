> 🇰🇷 한국어 | 🇺🇸 [English](en/04-claude-desktop.md)

# 04. Claude Desktop 배포

주력은 **Bootstrap 방식** — 앱이 Okta 로그인 후 서버에서 설정을 자동 수신합니다.
사용자는 게이트웨이 주소도 키도 입력하지 않습니다.

> 버전 요건: Bootstrap은 Claude Desktop **1.10270.0+**, 앱 네이티브 OIDC는 1.6889.0+.
> "Claude Desktop on 3P" 기능(공식 문서 GA 목표 2026-07-09)에 포함됩니다.

## 배포 파일

전 PC에 **동일한 파일 1개**를 배포합니다. 게이트웨이 주소·키·모델 목록은 들어있지
않습니다(서버가 bootstrap 응답으로 내려줌). 그래서 정책이 바뀌어도 서버만 고치면 되고
PC 재배포가 필요 없습니다.

### Windows (`.reg`)

레지스트리 경로: `HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Claude`
(사용자 자율 설치면 `HKEY_CURRENT_USER\SOFTWARE\Policies\Claude`)

```reg
Windows Registry Editor Version 5.00

[HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Claude]
"bootstrapEnabled"="true"
"bootstrapUrl"="https://{ALB_DNS}/portal/bootstrap"
"bootstrapOidc"="{\"issuer\":\"https://{your-org}.okta.com\",\"clientId\":\"{NATIVE_CLIENT_ID}\",\"redirectPort\":8123,\"scopes\":\"openid profile email offline_access\"}"
```

템플릿: `templates/claude-desktop-bootstrap.reg`

### macOS (`.mobileconfig` / MDM)

`/Library/Managed Preferences/com.anthropic.claudefordesktop.plist` 위치.
MDM(Jamf/Kandji 등)으로 배포하거나 프로파일 설치. 템플릿:
`templates/claude-desktop-bootstrap.mobileconfig`

## 값 인코딩 규칙 (실수 방지)

| 규칙 | 설명 |
|------|------|
| **모든 값은 문자열** | boolean도 `"true"`, 숫자도 `"3600"`. Windows에서 dword 아님(REG_SZ) |
| **객체는 단일 문자열** | `bootstrapOidc`는 JSON을 통째로 이스케이프한 REG_SZ 하나. 하위 키로 쪼개면 안 됨 (공식 문서가 경고하는 최다 실수) |
| **적용 시점** | 앱 시작 시 1회 읽음 → 배포 후 완전 종료(Cmd+Q) 후 재실행 |

> **가장 안전한 방법**: 앱 내 **Developer → Configure Third-Party Inference…**에서
> 구성을 만들어 Export하면 스키마가 정확히 맞습니다 (베타 기간 중 스키마 변경 대비).

## 자체서명 인증서 신뢰 설치 (정식 인증서 없을 때)

앱이 게이트웨이 TLS를 검증하므로, 배포 스크립트에 인증서 설치를 함께 넣습니다.
인증서는 포털이 `/portal/cert`로 제공합니다.

**Windows (관리자 PowerShell)**:
```powershell
curl.exe -sk https://{ALB_DNS}/portal/cert -o $env:TEMP\gw.crt
Import-Certificate -FilePath $env:TEMP\gw.crt -CertStoreLocation Cert:\LocalMachine\Root
```

**macOS**:
```bash
curl -sk https://{ALB_DNS}/portal/cert -o ~/gw.crt
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ~/gw.crt
```

> 정식 도메인 + ACM 인증서로 가면 이 단계 전체가 불필요해집니다.

## 탭·기능 정책 (서버 중앙 관리)

탭(Chat/Cowork/Code)과 기능 플래그는 **bootstrap 응답으로 내려주는 것을 권장**합니다.
`.reg`에 넣지 않아도 서버가 배포하며, 정책 변경 시 서버만 수정하면 됩니다.

| 정책 키 | 값 예시 | 설명 |
|---------|---------|------|
| `chatTabEnabled` | `"true"` | Chat 탭 (기본 비활성 → 명시 필요) |
| `coworkTabEnabled` | `"true"` | Cowork 탭 |
| `isClaudeCodeForDesktopEnabled` | `"true"` | Code 탭 |
| `isDesktopExtensionEnabled` | `"true"` | 확장(.dxt/.mcpb) 설치 허용 |
| `isLocalDevMcpEnabled` | `"true"` | 로컬 MCP 서버 추가 허용 |

> **boolean도 문자열 `"true"`**로 넣어야 합니다. plist 정수 `1`이나 native boolean은
> 무시됩니다 (실측 확인).

## "조직에서 관리합니다" UI 잠금

관리 구성/bootstrap이 있으면 앱이 "조직 관리" 모드가 되어 설정 UI가 잠깁니다.
**이건 설계 의도**이며(일반 직원이 게이트웨이 설정을 임의 변경 못하게) 잠금만 푸는 키는
없습니다. 대신 위 정책 키들을 허용적으로 내려주면 기능은 다 열립니다. 완전한 UI 자유가
필요한 개별 사용자는 관리 구성을 제거하고 수동 구성해야 합니다(그러면 bootstrap 자동
연결은 끊김).
