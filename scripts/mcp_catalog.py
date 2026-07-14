#!/usr/bin/env python3
"""
MCP 커넥터 카탈로그 관리 — 재배포 없이 DDB config 테이블만 수정한다.

bootstrap Lambda가 매 요청 시 이 카탈로그를 읽어 사용자 그룹으로 필터링해
Claude Desktop의 managedMcpServers로 내려준다. 서버 추가/회수는 이 CLI로 즉시 반영
(다음 앱 재시작 시 사용자에게 적용).

사용법:
  scripts/mcp_catalog.py list
  scripts/mcp_catalog.py add <name> <mcp-url> <okta-native-client-id> <okta-issuer> [group1,group2]
      → AgentCore 게이트웨이 등 Okta(custom AS)로 인증하는 MCP
  scripts/mcp_catalog.py add-external <name> <mcp-url> <issuer> [group1,group2]
      → 외부 SaaS MCP(예: Notion 호스티드 MCP). 그 서비스 자체 OAuth로 사용자가 각자 로그인.
        DCR 지원 서버는 client ID 불필요.
  scripts/mcp_catalog.py enable <name>
  scripts/mcp_catalog.py disable <name>
  scripts/mcp_catalog.py remove <name>

환경변수: REGION(기본 ap-northeast-2), TABLE(기본 llm-gw-gs-config)

예)
  scripts/mcp_catalog.py add seoul-weather \\
    https://xxx.gateway.bedrock-agentcore.ap-northeast-2.amazonaws.com/mcp \\
    0oa14xjdvu0Kcrclt698 https://your-org.okta.com weather-team

  allowed_groups를 비우면(마지막 인자 생략) 전원 허용.
  회수: disable(카탈로그에서 제외) 또는 remove(완전 삭제) → 다음 앱 재시작 시 반영.
"""
import os
import sys

import boto3

REGION = os.environ.get("REGION", "ap-northeast-2")
TABLE = os.environ.get("TABLE", "llm-gw-gs-config")


def _table():
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def cmd_list():
    # sk-index GSI로 CATALOG 아이템 조회
    from boto3.dynamodb.conditions import Key
    try:
        items = _table().query(
            IndexName="sk-index",
            KeyConditionExpression=Key("sk").eq("CATALOG"),
        ).get("Items", [])
    except Exception:
        items = _table().scan().get("Items", [])
        items = [i for i in items if i.get("sk") == "CATALOG"]
    if not items:
        print("(카탈로그 비어있음)")
        return
    for it in items:
        groups = ", ".join(it.get("allowed_groups") or []) or "전원"
        flag = "on " if it.get("enabled", True) else "off"
        print(f"[{flag}] {it['name']:24} groups={groups:20} {it['url']}")


def cmd_add(name, url, client, issuer, groups=""):
    auth_server = issuer.rstrip("/") + "/oauth2/default"
    group_list = [g.strip() for g in groups.split(",") if g.strip()]
    _table().put_item(Item={
        "pk": f"MCP#{name}",
        "sk": "CATALOG",
        "name": name,
        "url": url,
        "transport": "http",
        "enabled": True,
        "allowed_groups": group_list,
        "oauth": {
            "clientId": client,
            "issuer": auth_server,
            "authorizationServer": [auth_server],
            "scope": "openid profile email offline_access",
            "appendOfflineAccess": True,
            "callbackHost": "127.0.0.1",
            "callbackPort": 8124,
        },
    })
    print(f"추가됨: {name} (groups={groups or '전원'})")


def cmd_add_external(name, url, issuer, groups=""):
    """외부 SaaS MCP(예: Notion 호스티드 MCP) 등록.
    해당 서비스 자체 OAuth(AS)로 사용자가 각자 로그인한다. DCR 지원 서버는 clientId 불필요."""
    group_list = [g.strip() for g in groups.split(",") if g.strip()]
    iss = issuer.rstrip("/")
    _table().put_item(Item={
        "pk": f"MCP#{name}",
        "sk": "CATALOG",
        "name": name,
        "url": url,
        "transport": "http",
        "enabled": True,
        "allowed_groups": group_list,
        "oauth": {
            # 외부 서비스 AS. Claude Desktop이 DCR로 client를 동적 등록하고
            # 사용자가 그 서비스(Notion 등)에 직접 로그인한다. Okta와 무관.
            "issuer": iss,
            "authorizationServer": [iss],
            "scope": "",
            "appendOfflineAccess": True,
            "callbackHost": "127.0.0.1",
            "callbackPort": 8124,
        },
    })
    print(f"외부 MCP 추가됨: {name} (issuer={iss}, groups={groups or '전원'})")


def cmd_toggle(name, enabled):
    _table().update_item(
        Key={"pk": f"MCP#{name}", "sk": "CATALOG"},
        UpdateExpression="SET enabled = :e",
        ExpressionAttributeValues={":e": enabled},
    )
    print(f"{'enable' if enabled else 'disable'} 완료: {name} (다음 앱 재시작 시 반영)")


def cmd_remove(name):
    _table().delete_item(Key={"pk": f"MCP#{name}", "sk": "CATALOG"})
    print(f"삭제됨: {name} (다음 앱 재시작 시 커넥터 사라짐)")


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]
    if cmd == "list":
        cmd_list()
    elif cmd == "add":
        if len(args) < 4:
            print("add <name> <mcp-url> <client-id> <issuer> [groups]"); return 1
        cmd_add(*args[:5])
    elif cmd == "add-external":
        if len(args) < 3:
            print("add-external <name> <mcp-url> <issuer> [groups]"); return 1
        cmd_add_external(*args[:4])
    elif cmd in ("enable", "disable"):
        if not args:
            print(f"{cmd} <name>"); return 1
        cmd_toggle(args[0], cmd == "enable")
    elif cmd == "remove":
        if not args:
            print("remove <name>"); return 1
        cmd_remove(args[0])
    else:
        print(__doc__); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
