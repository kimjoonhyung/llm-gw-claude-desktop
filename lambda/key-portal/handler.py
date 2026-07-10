"""
Self-Service Key Portal Lambda

일반 사용자가 AWS CLI 없이 브라우저에서 Okta 로그인만으로
LiteLLM Virtual Key를 발급받는 포털. 기존 블루프린트의 apiKeyHelper
(로컬 스크립트) 로직을 클라우드(Lambda)로 옮긴 버전이다.

흐름:
1. GET /            -> Cognito Hosted UI(Okta OIDC 연동)로 리다이렉트 (CSRF state 쿠키 발급)
2. GET /?code=...   -> state 검증 -> Cognito 토큰 교환 -> /oauth2/userInfo로 사용자 확인
3. 이메일 기반으로 DynamoDB 캐시 조회 -> 없으면 LiteLLM /key/generate로 Virtual Key 생성
4. 인증된 세션 화면에 Virtual Key + Claude Code/Desktop 설정 가이드 표시

Virtual Key는 이메일로 전송하지 않고 인증된 HTTPS 세션 화면에만 표시한다.
(이메일은 평문 저장/전달 경로가 생기므로, 인증 화면 표시가 더 안전하다)
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets as pysecrets
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
_cognito_client = boto3.client("cognito-idp")

_master_key_cache: str | None = None
_app_client_cache: tuple[str, str] | None = None  # (client_id, client_secret)
_gateway_cert_cache: str | None = None

STATE_COOKIE = "portal_oauth_state"


def _get_app_client() -> tuple[str, str]:
    """Cognito App Client의 ID/Secret을 런타임에 조회한다 (CDK 순환 참조 회피).

    CloudFormation에서 Lambda 환경변수 <- App Client <- callback URL(Function URL) <- Lambda
    순환이 생기므로, 이름으로 client를 찾아 자격증명을 가져온다. 모듈 레벨 캐싱.
    """
    global _app_client_cache
    if _app_client_cache is not None:
        return _app_client_cache

    pool_id = os.environ["USER_POOL_ID"]
    target_name = os.environ["COGNITO_CLIENT_NAME"]

    client_id = None
    pagination_token = None
    while client_id is None:
        kwargs = {"UserPoolId": pool_id, "MaxResults": 60}
        if pagination_token:
            kwargs["NextToken"] = pagination_token
        page = _cognito_client.list_user_pool_clients(**kwargs)
        for c in page.get("UserPoolClients", []):
            if c.get("ClientName") == target_name:
                client_id = c["ClientId"]
                break
        pagination_token = page.get("NextToken")
        if client_id is None and not pagination_token:
            raise RuntimeError(f"Cognito app client를 찾을 수 없습니다: {target_name}")

    detail = _cognito_client.describe_user_pool_client(UserPoolId=pool_id, ClientId=client_id)
    secret = detail["UserPoolClient"]["ClientSecret"]
    _app_client_cache = (client_id, secret)
    return _app_client_cache


def _portal_url(event: dict[str, Any]) -> str:
    """요청의 Host 헤더로 포털 자신의 URL을 구성한다 (환경변수 순환 참조 회피).

    ALB 뒤에 있으므로 경로는 /portal 고정, 스킴은 X-Forwarded-Proto를 따른다.
    """
    headers = event.get("headers") or {}
    host = headers.get("host") or event.get("requestContext", {}).get("domainName", "")
    scheme = headers.get("x-forwarded-proto", "https")
    return f"{scheme}://{host}/portal"


def _get_path(event: dict[str, Any]) -> str:
    """Function URL(v2: rawPath)과 ALB(path) 이벤트 모두 지원."""
    return event.get("rawPath") or event.get("path") or "/"


def _get_cookies(event: dict[str, Any]) -> list[str]:
    """Function URL(cookies 배열)과 ALB(cookie 헤더) 이벤트 모두 지원."""
    if event.get("cookies"):
        return event["cookies"]
    cookie_header = (event.get("headers") or {}).get("cookie", "")
    return [c.strip() for c in cookie_header.split(";") if c.strip()]


# ---------------------------------------------------------------------------
# 엔트리포인트
# ---------------------------------------------------------------------------

def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda Function URL (payload v2.0) 핸들러"""
    try:
        path = _get_path(event)
        params = event.get("queryStringParameters") or {}

        if path == "/portal/health":
            return _text_response(200, "ok")

        # 게이트웨이 인증서 다운로드 (인증서는 TLS 핸드셰이크로 공개되는 정보라 인증 불필요)
        if path == "/portal/cert":
            return _cert_response()

        # Claude Desktop bootstrap: Okta 토큰 검증 후 사용자별 설정 JSON 반환
        if path == "/portal/bootstrap":
            return _handle_bootstrap(event)

        if path not in ("/portal", "/portal/"):
            return _redirect("/portal")

        # 웹 포털(브라우저 키 발급)은 백업 플랜 — 비활성 시 안내만 표시
        if not os.environ.get("WEB_PORTAL_ENABLED"):
            return _html_response(200, _bootstrap_info_page())

        code = params.get("code")
        if not code:
            return _start_login(event)

        return _handle_callback(event, code, params.get("state", ""))

    except Exception:
        logger.exception("포털 처리 중 오류 발생")
        return _html_response(500, _error_page("일시적인 오류가 발생했습니다. 잠시 후 다시 시도해주세요."))


# ---------------------------------------------------------------------------
# OAuth 로그인 (Cognito Hosted UI + Okta OIDC)
# ---------------------------------------------------------------------------

def _start_login(event: dict[str, Any]) -> dict[str, Any]:
    """Cognito Hosted UI로 리다이렉트한다. Okta IdP가 설정된 경우 바로 Okta 로그인으로 이동."""
    client_id, _ = _get_app_client()
    state = pysecrets.token_urlsafe(24)
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _portal_url(event),
        "scope": "openid email profile",
        "state": state,
    }
    idp_name = os.environ.get("IDP_NAME", "")
    if idp_name:
        query["identity_provider"] = idp_name

    authorize_url = f"{os.environ['COGNITO_DOMAIN']}/oauth2/authorize?{urllib.parse.urlencode(query)}"
    return {
        "statusCode": 302,
        "headers": {
            "Location": authorize_url,
            "Set-Cookie": f"{STATE_COOKIE}={_sign_state(state)}; Secure; HttpOnly; SameSite=Lax; Max-Age=600; Path=/portal",
        },
        "body": "",
    }


def _handle_callback(event: dict[str, Any], code: str, state: str) -> dict[str, Any]:
    """인증 코드 콜백: state 검증 -> 토큰 교환 -> 사용자 확인 -> Virtual Key 발급/표시"""
    # 1. CSRF state 검증
    if not _verify_state(event, state):
        logger.warning("OAuth state 검증 실패")
        return _redirect("/portal")

    # 2. 인증 코드 -> 토큰 교환 (client secret 포함, 서버 간 통신)
    try:
        tokens = _exchange_code(code, _portal_url(event))
    except urllib.error.HTTPError as e:
        logger.warning("토큰 교환 실패: %d", e.code)
        return _redirect("/portal")

    # 3. Cognito userInfo로 액세스 토큰 검증 및 사용자 확인
    userinfo = _fetch_userinfo(tokens["access_token"])
    email = (userinfo.get("email") or "").strip().lower()
    if not email:
        return _html_response(400, _error_page("IdP에서 이메일 정보를 받지 못했습니다. 관리자에게 문의하세요."))

    display_name = userinfo.get("name") or email
    logger.info("포털 로그인: email=%s", email)

    # 4. Virtual Key 조회/발급 (기존 apiKeyHelper의 클라우드 버전)
    virtual_key = _get_or_create_virtual_key(email)

    # 5. 인증된 세션 화면에 표시
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "Set-Cookie": f"{STATE_COOKIE}=deleted; Secure; HttpOnly; SameSite=Lax; Max-Age=0; Path=/portal",
        },
        "body": _key_page(display_name, email, virtual_key),
    }


def _exchange_code(code: str, redirect_uri: str) -> dict[str, Any]:
    """Cognito /oauth2/token에서 인증 코드를 토큰으로 교환한다."""
    client_id, client_secret = _get_app_client()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode()

    req = urllib.request.Request(
        f"{os.environ['COGNITO_DOMAIN']}/oauth2/token",
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _fetch_userinfo(access_token: str) -> dict[str, Any]:
    """Cognito /oauth2/userInfo 호출. Cognito가 토큰 유효성을 서버 측에서 검증한다."""
    req = urllib.request.Request(
        f"{os.environ['COGNITO_DOMAIN']}/oauth2/userInfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# CSRF state (HMAC 서명 쿠키)
# ---------------------------------------------------------------------------

def _state_secret() -> bytes:
    # client secret을 HMAC 키로 재사용 (별도 시크릿 불필요)
    _, client_secret = _get_app_client()
    return client_secret.encode()


def _sign_state(state: str) -> str:
    sig = hmac.new(_state_secret(), state.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{state}.{sig}"


def _verify_state(event: dict[str, Any], state: str) -> bool:
    if not state:
        return False
    for cookie in _get_cookies(event):
        if cookie.startswith(f"{STATE_COOKIE}="):
            value = cookie.split("=", 1)[1]
            expected = _sign_state(state)
            return hmac.compare_digest(value, expected)
    return False


# ---------------------------------------------------------------------------
# Virtual Key 조회/발급
# ---------------------------------------------------------------------------

def _get_or_create_virtual_key(email: str) -> str:
    """LiteLLM 사용자 보장 -> DynamoDB 캐시 조회 -> 없으면 키 생성 -> 캐시 저장"""
    master_key = _get_master_key()

    # SSO 로그인 시 LiteLLM Internal User를 항상 보장한다 (이미 있으면 no-op).
    # 사용자 단위 사용량 추적/예산이 키가 아닌 사용자 레벨에서도 동작하게 한다.
    _ensure_litellm_user(master_key, email)

    cached = _get_cached_key(email)
    if cached:
        logger.info("DynamoDB 캐시에서 Virtual Key 반환: user=%s", email)
        return cached

    try:
        virtual_key = _create_virtual_key(master_key, email)
    except urllib.error.HTTPError as e:
        if e.code == 400:
            # key_alias 충돌 -> 기존 키 복구
            logger.info("alias 충돌 감지, 기존 키 복구 시도: user=%s", email)
            virtual_key = _recover_existing_key(master_key, email)
        else:
            raise

    _cache_key(email, virtual_key)
    logger.info("Virtual Key 발급 완료: user=%s", email)
    return virtual_key


def _ensure_litellm_user(master_key: str, email: str) -> None:
    """LiteLLM에 Internal User를 생성한다 (멱등).

    /user/new는 이미 존재하는 user_id면 400을 반환하므로 무시한다.
    사용자 생성 실패가 키 발급을 막지 않도록 예외는 모두 로깅 후 삼킨다.
    """
    default_model = os.environ.get("DEFAULT_MODEL", "")
    body = {
        "user_id": email,
        "user_email": email,
        "user_role": "internal_user",
        "auto_create_key": False,
        "max_budget": float(os.environ.get("DEFAULT_MAX_BUDGET_USD", "1000")),
        "budget_duration": os.environ.get("BUDGET_DURATION", "30d"),
        "metadata": {"created_by": "key-portal", "idp": "okta", "default_model": default_model},
    }
    try:
        _litellm_request("POST", "/user/new", master_key, body=body)
        logger.info("LiteLLM 사용자 생성: user=%s", email)
    except urllib.error.HTTPError as e:
        # 400/409 모두 "이미 존재" 응답 (LiteLLM 버전에 따라 다름)
        if e.code in (400, 409):
            logger.info("LiteLLM 사용자 이미 존재: user=%s", email)
        else:
            logger.warning("LiteLLM 사용자 생성 실패(키 발급은 계속): user=%s code=%d", email, e.code)
    except Exception:
        logger.warning("LiteLLM 사용자 생성 실패(키 발급은 계속): user=%s", email, exc_info=True)


def _get_cached_key(email: str) -> str | None:
    table = _dynamodb.Table(os.environ["CONFIG_TABLE_NAME"])
    try:
        result = table.get_item(Key={"pk": f"USER#{email}", "sk": "VIRTUAL_KEY"})
        item = result.get("Item")
        if item and item.get("virtual_key"):
            return item["virtual_key"]
    except Exception:
        logger.warning("DynamoDB 캐시 조회 실패: user=%s", email, exc_info=True)
    return None


def _cache_key(email: str, virtual_key: str) -> None:
    table = _dynamodb.Table(os.environ["CONFIG_TABLE_NAME"])
    try:
        table.put_item(Item={
            "pk": f"USER#{email}",
            "sk": "VIRTUAL_KEY",
            "virtual_key": virtual_key,
            "key_alias": f"okta-{email}",
        })
    except Exception:
        logger.warning("DynamoDB 캐시 저장 실패: user=%s", email, exc_info=True)


def _get_master_key() -> str:
    global _master_key_cache
    if _master_key_cache is None:
        response = _secrets_client.get_secret_value(SecretId=os.environ["LITELLM_MASTER_KEY_ARN"])
        _master_key_cache = response["SecretString"]
    return _master_key_cache


def _create_virtual_key(master_key: str, email: str) -> str:
    """LiteLLM /key/generate로 사용자별 예산이 설정된 Virtual Key를 생성한다."""
    body = {
        "key_alias": f"okta-{email}",
        "user_id": email,
        "max_budget": float(os.environ.get("DEFAULT_MAX_BUDGET_USD", "1000")),
        "budget_duration": os.environ.get("BUDGET_DURATION", "30d"),
        "metadata": {"issued_by": "key-portal", "idp": "okta"},
    }
    response = _litellm_request("POST", "/key/generate", master_key, body=body)
    return response["key"]


def _recover_existing_key(master_key: str, email: str) -> str:
    """/user/info에서 okta- prefix 키를 찾아 기존 Virtual Key를 복구한다."""
    quoted = urllib.parse.quote(email)
    response = _litellm_request("GET", f"/user/info?user_id={quoted}", master_key)
    for key_info in response.get("keys", []):
        if key_info.get("key_alias", "").startswith("okta-"):
            return key_info["token"]
    raise RuntimeError(f"기존 Virtual Key를 찾을 수 없습니다: user={email}")


def _litellm_request(method: str, path: str, master_key: str, body: dict | None = None) -> dict:
    url = f"{os.environ['LITELLM_ENDPOINT']}{path}"
    headers = {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None

    # ALB가 자체서명 인증서를 쓰는 경우를 위해 내부 호출은 TLS 검증 생략
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
# Claude Desktop Bootstrap (앱 네이티브 OIDC — 수동 키 입력 제거)
# ---------------------------------------------------------------------------
#
# 앱이 bootstrapOidc(Okta PKCE)로 로그인 후, 액세스 토큰을 Bearer로 붙여
# GET /portal/bootstrap 을 호출한다. 토큰을 검증하고 그 사용자의
# Virtual Key가 포함된 설정 JSON을 반환한다 — 응답이 곧 앱의 유효 설정이 된다.
#
# 토큰 검증 (2단계):
# 1. JWT 페이로드 사전 검사 — iss가 우리 Okta 테넌트, cid가 우리 Native App,
#    exp 미경과인지 확인 (서명 검증 전 필터링)
# 2. Okta /oauth2/v1/userinfo 호출 — Okta가 서버 측에서 서명/유효성을
#    최종 검증하고 email 클레임을 반환 (Cognito 콜백과 동일한 패턴)

def _handle_bootstrap(event: dict[str, Any]) -> dict[str, Any]:
    okta_issuer = os.environ.get("OKTA_ISSUER", "")
    expected_client_id = os.environ.get("DESKTOP_OIDC_CLIENT_ID", "")
    if not okta_issuer or not expected_client_id:
        return _json_response(404, {"error": "bootstrap not enabled"})

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return _json_response(401, {"error": "missing bearer token"})
    token = auth[len("Bearer "):].strip()

    # 1. 페이로드 사전 검사
    claims = _decode_jwt_payload(token)
    if claims is None:
        return _json_response(401, {"error": "malformed token"})
    if claims.get("iss") != okta_issuer:
        logger.warning("bootstrap: issuer 불일치: %s", claims.get("iss"))
        return _json_response(401, {"error": "invalid issuer"})
    token_client = claims.get("cid") or claims.get("client_id") or claims.get("aud")
    if expected_client_id not in (token_client if isinstance(token_client, list) else [token_client]):
        logger.warning("bootstrap: client 불일치: %s", token_client)
        return _json_response(401, {"error": "invalid client"})
    if claims.get("exp") is not None and claims["exp"] < _now_epoch():
        return _json_response(401, {"error": "token expired"})

    # 2. Okta 서버 측 최종 검증 + email 획득
    try:
        userinfo = _fetch_okta_userinfo(okta_issuer, token)
    except urllib.error.HTTPError as e:
        logger.warning("bootstrap: userinfo 검증 실패: %d", e.code)
        return _json_response(401, {"error": "token verification failed"})

    email = (userinfo.get("email") or "").strip().lower()
    if not email:
        return _json_response(403, {"error": "email claim missing"})

    logger.info("bootstrap 요청: email=%s", email)
    virtual_key = _get_or_create_virtual_key(email)

    # 응답 JSON = Claude Desktop의 유효 설정 (managed config 스키마)
    gateway_url = os.environ.get("GATEWAY_URL", "").rstrip("/")
    config = {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": gateway_url,
        "inferenceCredentialKind": "apiKey",
        "inferenceGatewayApiKey": virtual_key,
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceModels": [
            os.environ.get("MODEL_OPUS", ""),
            os.environ.get("MODEL_SONNET", ""),
            os.environ.get("MODEL_HAIKU", ""),
        ],
        # --- 기능 정책 (bootstrap 중앙 관리, boolean도 문자열로) ---
        # 최대 개방 프로파일: 탭 전부 + 확장/로컬 MCP/자동 모드/파일 분석 허용.
        # 제한 키(disabledBuiltinTools, allowedWorkspaceFolders,
        # coworkEgressAllowedHosts 등)는 아예 넣지 않는다 — 미설정 = 무제한.
        "chatTabEnabled": "true",
        "coworkTabEnabled": "true",
        "isClaudeCodeForDesktopEnabled": "true",
        "chatAdvancedFileAnalysisEnabled": "true",
        "autoModeEnabled": "true",
        "isDesktopExtensionEnabled": "true",
        "isDesktopExtensionSignatureRequired": "false",
        "isLocalDevMcpEnabled": "true",
    }

    # --- 조직 관리 MCP 커넥터 (AgentCore Gateway 등) ---
    # bootstrap 응답에 포함하면 사용자가 URL을 직접 입력하지 않아도
    # 커넥터 목록에 자동으로 뜬다. OAuth는 bootstrap과 동일한 Okta Native App 재사용
    # (게이트웨이 authorizer 허용 클라이언트에 등록되어 있어야 함).
    # 콜백 포트는 bootstrap(8123)과 겹치지 않게 분리.
    managed_mcp = _managed_mcp_servers()
    if managed_mcp:
        config["managedMcpServers"] = managed_mcp

    return _json_response(200, config)


def _managed_mcp_servers() -> list:
    """MCP_SERVERS_JSON 환경변수(JSON 배열)를 파싱해 반환. 미설정이면 빈 리스트."""
    raw = os.environ.get("MCP_SERVERS_JSON", "").strip()
    if not raw:
        return []
    try:
        servers = json.loads(raw)
        return servers if isinstance(servers, list) else []
    except Exception:
        logger.warning("MCP_SERVERS_JSON 파싱 실패", exc_info=True)
        return []


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """JWT 페이로드를 서명 검증 없이 디코딩한다 (사전 필터링용 — 최종 검증은 userinfo)."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def _fetch_okta_userinfo(issuer: str, access_token: str) -> dict[str, Any]:
    """Okta userinfo 호출. Okta가 토큰 서명/만료/폐기 여부를 서버 측에서 검증한다."""
    req = urllib.request.Request(
        f"{issuer}/oauth2/v1/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _now_epoch() -> int:
    import time
    return int(time.time())


def _json_response(status_code: int, body: dict) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# 게이트웨이 인증서 다운로드
# ---------------------------------------------------------------------------

def _fetch_gateway_cert() -> str:
    """게이트웨이(ALB)가 서빙 중인 TLS 인증서를 PEM으로 가져온다. 모듈 레벨 캐싱."""
    global _gateway_cert_cache
    if _gateway_cert_cache is None:
        gateway_url = os.environ["GATEWAY_URL"]
        parsed = urllib.parse.urlparse(gateway_url)
        host = parsed.hostname
        port = parsed.port or 443
        _gateway_cert_cache = ssl.get_server_certificate((host, port))
    return _gateway_cert_cache


def _cert_response() -> dict[str, Any]:
    """게이트웨이 인증서를 .crt 파일로 다운로드시킨다."""
    try:
        pem = _fetch_gateway_cert()
    except Exception:
        logger.exception("게이트웨이 인증서 조회 실패")
        return _text_response(503, "certificate unavailable")
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/x-x509-ca-cert",
            "Content-Disposition": 'attachment; filename="llm-gateway.crt"',
            "Cache-Control": "no-store",
        },
        "body": pem,
    }


# ---------------------------------------------------------------------------
# HTML 렌더링
# ---------------------------------------------------------------------------

def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


def _key_page(display_name: str, email: str, virtual_key: str) -> str:
    gateway_url = os.environ.get("GATEWAY_URL", "").rstrip("/")
    opus = os.environ.get("MODEL_OPUS", "")
    sonnet = os.environ.get("MODEL_SONNET", "")
    haiku = os.environ.get("MODEL_HAIKU", "")

    claude_code_settings = json.dumps({
        "env": {
            "ANTHROPIC_BASE_URL": gateway_url,
            "ANTHROPIC_AUTH_TOKEN": virtual_key,
            "ANTHROPIC_MODEL": os.environ.get("DEFAULT_MODEL", opus),
            "ANTHROPIC_DEFAULT_OPUS_MODEL": opus,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": sonnet,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": haiku,
            "NODE_EXTRA_CA_CERTS": "~/llm-gateway.crt",
        },
    }, indent=2)

    cert_url = f"{gateway_url}/portal/cert"
    # 다운로드 + macOS 시스템 신뢰 등록을 한 줄로
    mac_install_cmd = (
        f"curl -sk {cert_url} -o ~/llm-gateway.crt && "
        "sudo security add-trusted-cert -d -r trustRoot "
        "-k /Library/Keychains/System.keychain ~/llm-gateway.crt"
    )
    # Windows (관리자 PowerShell)
    win_install_cmd = (
        f"curl.exe -sk {cert_url} -o $env:USERPROFILE\\llm-gateway.crt; "
        "Import-Certificate -FilePath $env:USERPROFILE\\llm-gateway.crt "
        "-CertStoreLocation Cert:\\LocalMachine\\Root"
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>LLM Gateway - API Key</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 760px; margin: 40px auto; padding: 0 20px; color: #1a1a2e; }}
  h1 {{ font-size: 22px; }}
  .card {{ border: 1px solid #d9d9e3; border-radius: 10px; padding: 20px 24px; margin: 18px 0; }}
  .key {{ font-family: ui-monospace, Menlo, monospace; font-size: 14px; background: #f4f4f8;
          border: 1px solid #d9d9e3; border-radius: 6px; padding: 12px; word-break: break-all; }}
  pre {{ background: #f4f4f8; border: 1px solid #d9d9e3; border-radius: 6px;
         padding: 14px; overflow-x: auto; font-size: 13px; }}
  button {{ background: #4a4ae0; color: #fff; border: 0; border-radius: 6px;
            padding: 8px 14px; cursor: pointer; margin-top: 10px; }}
  .muted {{ color: #667; font-size: 13px; }}
  .warn {{ background: #fff8e6; border: 1px solid #f0d998; border-radius: 6px;
           padding: 10px 14px; font-size: 13px; }}
</style>
</head>
<body>
<h1>LLM Gateway API Key</h1>
<p>{_escape(display_name)} ({_escape(email)}) 님, Okta 인증이 완료되었습니다.</p>

<div class="card">
  <h3>1. Virtual API Key</h3>
  <div class="key" id="vkey">{_escape(virtual_key)}</div>
  <button onclick="copyText('vkey')">키 복사</button>
  <p class="warn">이 키는 본인 계정의 사용량/예산에 연결됩니다. 절대 다른 사람과 공유하지 마세요.</p>
</div>

<div class="card">
  <h3>2. Gateway 주소</h3>
  <div class="key" id="gwurl">{_escape(gateway_url)}</div>
  <button onclick="copyText('gwurl')">주소 복사</button>
</div>

<div class="card">
  <h3>3. 게이트웨이 인증서 설치 (최초 1회, 필수)</h3>
  <p class="muted">사내 게이트웨이는 자체 인증서를 사용하므로, 설치하지 않으면
  Claude Desktop에서 <b>ERR_CERT_AUTHORITY_INVALID</b> 오류가 발생합니다.</p>

  <p><b>macOS</b> — 터미널에 붙여넣고 실행 (관리자 암호 입력):</p>
  <pre id="certmac">{_escape(mac_install_cmd)}</pre>
  <button onclick="copyText('certmac')">macOS 설치 명령 복사</button>

  <p style="margin-top:16px"><b>Windows</b> — 관리자 PowerShell에 붙여넣고 실행:</p>
  <pre id="certwin">{_escape(win_install_cmd)}</pre>
  <button onclick="copyText('certwin')">Windows 설치 명령 복사</button>

  <p class="muted" style="margin-top:12px">명령 실행이 어려우면
  <a href="/portal/cert" download="llm-gateway.crt">인증서 파일 다운로드</a> 후
  macOS: 더블클릭 → 키체인 접근에서 "항상 신뢰"로 변경 /
  Windows: 우클릭 → 인증서 설치 → 로컬 컴퓨터 → "신뢰할 수 있는 루트 인증 기관"을 선택하세요.</p>

  <p class="warn">설치 후 Claude Desktop을 완전히 종료(Cmd+Q / 트레이 아이콘 종료)했다가 다시 실행해야 적용됩니다.</p>
</div>

<div class="card">
  <h3>4. Claude Code / Claude Desktop 설정</h3>
  <p class="muted">~/.claude/settings.json 에 아래 내용을 붙여넣으세요. AWS CLI나 SSO 로그인은 필요하지 않습니다.</p>
  <pre id="settings">{_escape(claude_code_settings)}</pre>
  <button onclick="copyText('settings')">설정 복사</button>
</div>

<p class="muted">키를 재발급 받으려면 관리자에게 문의하세요. 같은 계정으로 다시 로그인하면 항상 동일한 키가 표시됩니다.</p>

<script>
function copyText(id) {{
  navigator.clipboard.writeText(document.getElementById(id).innerText);
}}
</script>
</body>
</html>"""


def _bootstrap_info_page() -> str:
    """웹 포털 비활성 시 안내 페이지 — 주력 경로(Claude Desktop 자동 설정)를 안내한다."""
    return """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><meta name="robots" content="noindex">
<title>LLM Gateway</title></head>
<body style="font-family:sans-serif;max-width:640px;margin:60px auto;">
<h2>LLM Gateway</h2>
<p>이 게이트웨이는 <b>Claude Desktop 자동 설정(bootstrap)</b> 방식으로 운영됩니다.</p>
<p>IT 부서가 배포한 구성이 적용된 PC에서 Claude Desktop을 실행하고
회사 계정(Okta)으로 로그인하면 자동으로 연결됩니다 — 별도의 키 발급이 필요 없습니다.</p>
<p>문제가 있으면 관리자에게 문의하세요.</p>
</body></html>"""


def _error_page(message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><title>LLM Gateway Portal</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:60px auto;">
<h2>오류</h2><p>{_escape(message)}</p>
<p><a href="/portal">다시 시도</a></p>
</body></html>"""


# ---------------------------------------------------------------------------
# 응답 헬퍼
# ---------------------------------------------------------------------------

def _redirect(location: str) -> dict[str, Any]:
    return {"statusCode": 302, "headers": {"Location": location}, "body": ""}


def _html_response(status_code: int, body: str) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
        "body": body,
    }


def _text_response(status_code: int, body: str) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "text/plain; charset=utf-8"},
        "body": body,
    }
