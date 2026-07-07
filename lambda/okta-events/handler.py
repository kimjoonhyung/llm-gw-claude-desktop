"""
Okta Event Hook Handler — 자동 오프보딩

Okta에서 사용자가 비활성화/정지되거나 앱 어사인이 해제되면
LiteLLM Virtual Key를 즉시 회수한다. (SCIM deprovisioning의 대체 구현)

처리 흐름:
1. GET  + X-Okta-Verification-Challenge 헤더 -> 원타임 검증 응답 (Okta 훅 등록 시 1회)
2. POST -> Authorization 헤더를 웹훅 시크릿과 비교 (불일치 시 401)
3. 이벤트 파싱:
   - user.lifecycle.deactivate / suspend / delete.initiated -> 무조건 회수
   - application.user_membership.remove -> OKTA_APP_LABEL과 일치하는 앱일 때만 회수
4. 회수 = LiteLLM 키 삭제(/key/delete) + DynamoDB 캐시 삭제
   (LiteLLM 사용자 레코드는 사용량 감사를 위해 남긴다)
"""

import hmac
import json
import logging
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_dynamodb = boto3.resource("dynamodb")
_secrets_client = boto3.client("secretsmanager")

_master_key_cache: str | None = None
_webhook_secret_cache: str | None = None

# 사용자 단위 비활성화 이벤트 (앱과 무관하게 즉시 회수)
USER_DEPROVISION_EVENTS = {
    "user.lifecycle.deactivate",
    "user.lifecycle.suspend",
    "user.lifecycle.delete.initiated",
}
# 앱 어사인 해제 이벤트 (OKTA_APP_LABEL 일치 시에만 회수)
APP_UNASSIGN_EVENT = "application.user_membership.remove"
# 그룹 제거 이벤트 (OKTA_GROUP_LABEL 일치 시에만 회수 — 그룹 기반 어사인 운영용)
GROUP_UNASSIGN_EVENT = "group.user_membership.remove"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway (REST proxy) 핸들러"""
    try:
        method = event.get("httpMethod", "")
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

        # 1. Okta 원타임 검증 (훅 등록 시 GET으로 챌린지 전송)
        if method == "GET":
            challenge = headers.get("x-okta-verification-challenge")
            if challenge:
                return _json_response(200, {"verification": challenge})
            return _json_response(400, {"error": "missing verification challenge"})

        if method != "POST":
            return _json_response(405, {"error": "method not allowed"})

        # 2. 웹훅 인증 (Okta가 보내는 Authorization 헤더 == 시크릿)
        if not hmac.compare_digest(headers.get("authorization", ""), _get_webhook_secret()):
            logger.warning("웹훅 인증 실패")
            return _json_response(401, {"error": "unauthorized"})

        # 3. 이벤트 처리
        body = json.loads(event.get("body") or "{}")
        events = body.get("data", {}).get("events", [])
        revoked = []
        for e in events:
            email = _extract_target_email(e)
            if email:
                _revoke_user_access(email)
                revoked.append(email)

        logger.info("이벤트 %d건 중 %d건 회수 처리: %s", len(events), len(revoked), revoked)
        return _json_response(200, {"received": len(events), "revoked": revoked})

    except Exception:
        logger.exception("이벤트 훅 처리 중 오류")
        # Okta 재시도를 막기 위해 200 반환 (오류는 로그로 추적)
        return _json_response(200, {"error": "internal error, logged"})


# ---------------------------------------------------------------------------
# 이벤트 파싱
# ---------------------------------------------------------------------------

def _extract_target_email(e: dict[str, Any]) -> str | None:
    """회수 대상 이벤트면 대상 사용자의 이메일을 반환, 아니면 None."""
    event_type = e.get("eventType", "")
    targets = e.get("target") or []

    user_email = None
    app_label = None
    group_label = None
    for t in targets:
        if t.get("type") == "User":
            user_email = (t.get("alternateId") or "").strip().lower()
        elif t.get("type") == "AppInstance":
            app_label = t.get("displayName", "")
        elif t.get("type") == "UserGroup":
            group_label = t.get("displayName", "")

    if not user_email:
        logger.info("User 타겟 없는 이벤트 무시: type=%s", event_type)
        return None

    if event_type in USER_DEPROVISION_EVENTS:
        logger.info("사용자 비활성화 이벤트: type=%s user=%s", event_type, user_email)
        return user_email

    if event_type == APP_UNASSIGN_EVENT:
        expected_label = os.environ.get("OKTA_APP_LABEL", "")
        if not expected_label:
            # 앱 필터 미설정 시 무관한 앱 어사인 해제로 오회수하지 않도록 무시
            logger.info("앱 어사인 해제 무시(OKTA_APP_LABEL 미설정): user=%s app=%s", user_email, app_label)
            return None
        if app_label == expected_label:
            logger.info("게이트웨이 앱 어사인 해제: user=%s app=%s", user_email, app_label)
            return user_email
        logger.info("다른 앱 어사인 해제 무시: user=%s app=%s", user_email, app_label)
        return None

    if event_type == GROUP_UNASSIGN_EVENT:
        # 쉼표 구분 복수 그룹 지원. 미설정 시 무관한 그룹 제거로 오회수하지 않도록 무시.
        expected_groups = [g.strip() for g in os.environ.get("OKTA_GROUP_LABEL", "").split(",") if g.strip()]
        if not expected_groups:
            logger.info("그룹 제거 무시(OKTA_GROUP_LABEL 미설정): user=%s group=%s", user_email, group_label)
            return None
        if group_label in expected_groups:
            logger.info("게이트웨이 그룹 제거: user=%s group=%s", user_email, group_label)
            return user_email
        logger.info("다른 그룹 제거 무시: user=%s group=%s", user_email, group_label)
        return None

    logger.info("미처리 이벤트 타입 무시: type=%s user=%s", event_type, user_email)
    return None


# ---------------------------------------------------------------------------
# 접근 회수
# ---------------------------------------------------------------------------

def _revoke_user_access(email: str) -> None:
    """LiteLLM 키 전부 삭제 + DynamoDB 캐시 삭제. 사용자 레코드는 감사용으로 유지."""
    master_key = _get_master_key()

    # 1. 사용자의 모든 키 조회 후 삭제 (alias 규칙에 의존하지 않음)
    tokens = _collect_user_key_tokens(master_key, email)
    if tokens:
        _litellm_request("POST", "/key/delete", master_key, body={"keys": tokens})
        logger.info("LiteLLM 키 %d개 삭제: user=%s", len(tokens), email)
    else:
        logger.info("삭제할 LiteLLM 키 없음: user=%s", email)

    # 2. DynamoDB 캐시 삭제 (다음 로그인 시 키 재발급 방지 — 재로그인 자체가 Okta에서 차단됨)
    table = _dynamodb.Table(os.environ["CONFIG_TABLE_NAME"])
    try:
        table.delete_item(Key={"pk": f"USER#{email}", "sk": "VIRTUAL_KEY"})
        logger.info("DynamoDB 캐시 삭제: user=%s", email)
    except Exception:
        logger.warning("DynamoDB 캐시 삭제 실패: user=%s", email, exc_info=True)


def _collect_user_key_tokens(master_key: str, email: str) -> list[str]:
    """사용자의 모든 키 토큰을 수집한다.

    /user/info가 기본 경로. 사용자 레코드 없이 키만 존재하는 과거 케이스를 위해
    404 시 /key/list?user_id= 로 폴백한다.
    """
    quoted = urllib.parse.quote(email)
    try:
        info = _litellm_request("GET", f"/user/info?user_id={quoted}", master_key)
        return [k["token"] for k in info.get("keys", []) if k.get("token")]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        logger.info("LiteLLM 사용자 레코드 없음, /key/list 폴백: user=%s", email)

    try:
        listing = _litellm_request(
            "GET", f"/key/list?user_id={quoted}&return_full_object=true", master_key)
        return [k["token"] for k in listing.get("keys", [])
                if isinstance(k, dict) and k.get("token")]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise


# ---------------------------------------------------------------------------
# 시크릿
# ---------------------------------------------------------------------------

def _get_webhook_secret() -> str:
    global _webhook_secret_cache
    if _webhook_secret_cache is None:
        resp = _secrets_client.get_secret_value(SecretId=os.environ["WEBHOOK_SECRET_ARN"])
        _webhook_secret_cache = resp["SecretString"]
    return _webhook_secret_cache


def _get_master_key() -> str:
    global _master_key_cache
    if _master_key_cache is None:
        resp = _secrets_client.get_secret_value(SecretId=os.environ["LITELLM_MASTER_KEY_ARN"])
        _master_key_cache = resp["SecretString"]
    return _master_key_cache


# ---------------------------------------------------------------------------
# LiteLLM API
# ---------------------------------------------------------------------------

def _litellm_request(method: str, path: str, master_key: str, body: dict | None = None) -> dict:
    url = f"{os.environ['LITELLM_ENDPOINT']}{path}"
    headers = {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None

    # 자체서명 인증서 ALB 대응
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()
        except Exception:
            pass
        logger.error("LiteLLM API 에러: %s %s -> %d %s", method, path, e.code, detail)
        raise


# ---------------------------------------------------------------------------
# 응답 헬퍼
# ---------------------------------------------------------------------------

def _json_response(status_code: int, body: dict) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
