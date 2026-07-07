"""key-portal Lambda 핸들러 단위 테스트 (표준 라이브러리 unittest만 사용)

실행: python3 -m unittest discover -s lambda/key-portal/tests
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TEST_ENV = {
    "COGNITO_DOMAIN": "https://test.auth.ap-northeast-2.amazoncognito.com",
    "USER_POOL_ID": "ap-northeast-2_test",
    "COGNITO_CLIENT_NAME": "llm-gateway-portal-client",
    "IDP_NAME": "Okta",
    "CONFIG_TABLE_NAME": "llm-gateway-config",
    "LITELLM_ENDPOINT": "https://gateway.example.com",
    "LITELLM_MASTER_KEY_ARN": "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:test",
    "GATEWAY_URL": "https://gateway.example.com",
    "AWS_DEFAULT_REGION": "ap-northeast-2",
    "OKTA_ISSUER": "https://test-org.okta.com",
    "DESKTOP_OIDC_CLIENT_ID": "0oaNATIVETEST",
    "WEB_PORTAL_ENABLED": "true",
    "MODEL_OPUS": "global.anthropic.claude-opus-4-8",
    "MODEL_SONNET": "global.anthropic.claude-sonnet-4-6",
    "MODEL_HAIKU": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
}

with mock.patch.dict(os.environ, TEST_ENV):
    with mock.patch("boto3.resource"), mock.patch("boto3.client"):
        import handler

# app client 런타임 조회를 고정값으로 대체
handler._app_client_cache = ("test-client-id", "test-client-secret")

PORTAL_HOST = "test.lambda-url.ap-northeast-2.on.aws"


def _apply_env(test):
    return mock.patch.dict(os.environ, TEST_ENV)(test)


@_apply_env
class TestLoginRedirect(unittest.TestCase):
    def test_root_without_code_redirects_to_okta(self):
        # ALB 이벤트 형식 (path + cookie 헤더)
        event = {
            "path": "/portal",
            "queryStringParameters": None,
            "headers": {"host": PORTAL_HOST, "x-forwarded-proto": "https"},
        }
        resp = handler.handler(event, None)

        self.assertEqual(resp["statusCode"], 302)
        location = resp["headers"]["Location"]
        self.assertIn("/oauth2/authorize", location)
        self.assertIn("identity_provider=Okta", location)
        self.assertIn("client_id=test-client-id", location)
        # redirect_uri가 /portal 경로를 포함해야 함
        self.assertIn("%2Fportal", location)
        # CSRF state 쿠키가 설정되어야 함
        self.assertTrue(resp["headers"]["Set-Cookie"].startswith(handler.STATE_COOKIE))

    def test_unknown_path_redirects_home(self):
        resp = handler.handler({"path": "/foo"}, None)
        self.assertEqual(resp["statusCode"], 302)
        self.assertEqual(resp["headers"]["Location"], "/portal")

    def test_health_check(self):
        resp = handler.handler({"path": "/portal/health"}, None)
        self.assertEqual(resp["statusCode"], 200)

    def test_cert_download(self):
        with mock.patch.object(handler, "_fetch_gateway_cert", return_value="-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"):
            resp = handler.handler({"path": "/portal/cert"}, None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("attachment", resp["headers"]["Content-Disposition"])
        self.assertIn("BEGIN CERTIFICATE", resp["body"])


@_apply_env
class TestStateVerification(unittest.TestCase):
    def test_valid_state_roundtrip(self):
        state = "abc123"
        cookie_value = handler._sign_state(state)
        # ALB는 cookie를 단일 헤더로 전달
        event = {"headers": {"cookie": f"other=1; {handler.STATE_COOKIE}={cookie_value}"}}
        self.assertTrue(handler._verify_state(event, state))

    def test_function_url_cookie_array_also_supported(self):
        state = "abc123"
        cookie_value = handler._sign_state(state)
        event = {"cookies": [f"{handler.STATE_COOKIE}={cookie_value}"]}
        self.assertTrue(handler._verify_state(event, state))

    def test_tampered_state_rejected(self):
        cookie_value = handler._sign_state("original")
        event = {"headers": {"cookie": f"{handler.STATE_COOKIE}={cookie_value}"}}
        self.assertFalse(handler._verify_state(event, "forged"))

    def test_missing_cookie_rejected(self):
        self.assertFalse(handler._verify_state({"headers": {}}, "abc"))

    def test_empty_state_rejected(self):
        self.assertFalse(handler._verify_state({"headers": {"cookie": "x=y"}}, ""))


@_apply_env
class TestCallback(unittest.TestCase):
    def _callback_event(self, state):
        return {
            "path": "/portal",
            "queryStringParameters": {"code": "auth-code", "state": state},
            "headers": {
                "host": PORTAL_HOST,
                "x-forwarded-proto": "https",
                "cookie": f"{handler.STATE_COOKIE}={handler._sign_state(state)}",
            },
        }

    @mock.patch.object(handler, "_get_or_create_virtual_key", return_value="sk-test-key")
    @mock.patch.object(handler, "_fetch_userinfo", return_value={"email": "Alice@Example.com", "name": "Alice"})
    @mock.patch.object(handler, "_exchange_code", return_value={"access_token": "at"})
    def test_successful_callback_shows_key(self, mock_exchange, mock_userinfo, mock_key):
        resp = handler.handler(self._callback_event("state1"), None)

        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("sk-test-key", resp["body"])
        # 이메일은 소문자로 정규화되어 키 발급에 사용
        mock_key.assert_called_once_with("alice@example.com")
        # 캐시 방지 헤더
        self.assertEqual(resp["headers"]["Cache-Control"], "no-store")

    @mock.patch.object(handler, "_exchange_code")
    def test_invalid_state_redirects_without_token_exchange(self, mock_exchange):
        event = self._callback_event("state1")
        event["queryStringParameters"]["state"] = "wrong-state"
        resp = handler.handler(event, None)

        self.assertEqual(resp["statusCode"], 302)
        mock_exchange.assert_not_called()

    @mock.patch.object(handler, "_fetch_userinfo", return_value={"name": "NoEmail"})
    @mock.patch.object(handler, "_exchange_code", return_value={"access_token": "at"})
    def test_missing_email_returns_error(self, mock_exchange, mock_userinfo):
        resp = handler.handler(self._callback_event("state1"), None)
        self.assertEqual(resp["statusCode"], 400)


@_apply_env
class TestVirtualKeyFlow(unittest.TestCase):
    @mock.patch.object(handler, "_ensure_litellm_user")
    @mock.patch.object(handler, "_get_master_key", return_value="master")
    @mock.patch.object(handler, "_get_cached_key", return_value="sk-cached")
    def test_cache_hit_skips_key_creation_but_ensures_user(self, mock_cached, mock_master, mock_ensure):
        self.assertEqual(handler._get_or_create_virtual_key("a@b.com"), "sk-cached")
        # 캐시 히트여도 SSO 로그인 시 LiteLLM 사용자는 항상 보장
        mock_ensure.assert_called_once_with("master", "a@b.com")

    @mock.patch.object(handler, "_ensure_litellm_user")
    @mock.patch.object(handler, "_cache_key")
    @mock.patch.object(handler, "_create_virtual_key", return_value="sk-new")
    @mock.patch.object(handler, "_get_master_key", return_value="master")
    @mock.patch.object(handler, "_get_cached_key", return_value=None)
    def test_cache_miss_creates_user_key_and_caches(self, mock_cached, mock_master, mock_create, mock_cache, mock_ensure):
        self.assertEqual(handler._get_or_create_virtual_key("a@b.com"), "sk-new")
        mock_ensure.assert_called_once_with("master", "a@b.com")
        mock_create.assert_called_once_with("master", "a@b.com")
        mock_cache.assert_called_once_with("a@b.com", "sk-new")


@_apply_env
class TestEnsureLitellmUser(unittest.TestCase):
    @mock.patch.object(handler, "_litellm_request")
    def test_creates_internal_user(self, mock_req):
        handler._ensure_litellm_user("master", "a@b.com")
        method, path, key = mock_req.call_args[0]
        body = mock_req.call_args[1]["body"]
        self.assertEqual((method, path), ("POST", "/user/new"))
        self.assertEqual(body["user_id"], "a@b.com")
        self.assertEqual(body["user_role"], "internal_user")
        self.assertFalse(body["auto_create_key"])

    @mock.patch.object(handler, "_litellm_request", side_effect=__import__("urllib.error", fromlist=["HTTPError"]).HTTPError("u", 400, "exists", {}, None))
    def test_existing_user_is_noop(self, mock_req):
        # 400(이미 존재)은 예외 없이 통과해야 함
        handler._ensure_litellm_user("master", "a@b.com")

    @mock.patch.object(handler, "_litellm_request", side_effect=RuntimeError("down"))
    def test_failure_does_not_block_key_issuance(self, mock_req):
        handler._ensure_litellm_user("master", "a@b.com")


def _make_jwt(payload):
    """서명 없는 테스트용 JWT (헤더.페이로드.가짜서명)"""
    import base64 as b64
    def enc(d):
        return b64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg':'RS256'})}.{enc(payload)}.fakesig"


@_apply_env
class TestBootstrap(unittest.TestCase):
    def _bootstrap_event(self, token):
        return {
            "path": "/portal/bootstrap",
            "headers": {"authorization": f"Bearer {token}", "host": PORTAL_HOST},
        }

    def _valid_claims(self, **overrides):
        claims = {
            "iss": "https://test-org.okta.com",
            "cid": "0oaNATIVETEST",
            "exp": 9999999999,
        }
        claims.update(overrides)
        return claims

    @mock.patch.object(handler, "_get_or_create_virtual_key", return_value="sk-boot-key")
    @mock.patch.object(handler, "_fetch_okta_userinfo", return_value={"email": "Boot@Example.com"})
    def test_valid_token_returns_config_with_key(self, mock_userinfo, mock_key):
        token = _make_jwt(self._valid_claims())
        resp = handler.handler(self._bootstrap_event(token), None)

        self.assertEqual(resp["statusCode"], 200)
        config = json.loads(resp["body"])
        self.assertEqual(config["inferenceGatewayApiKey"], "sk-boot-key")
        self.assertEqual(config["inferenceGatewayBaseUrl"], "https://gateway.example.com")
        self.assertEqual(config["inferenceCredentialKind"], "apiKey")
        self.assertEqual(len(config["inferenceModels"]), 3)
        mock_key.assert_called_once_with("boot@example.com")

    def test_missing_token_401(self):
        resp = handler.handler({"path": "/portal/bootstrap", "headers": {}}, None)
        self.assertEqual(resp["statusCode"], 401)

    @mock.patch.object(handler, "_fetch_okta_userinfo")
    def test_wrong_issuer_rejected_before_userinfo(self, mock_userinfo):
        token = _make_jwt(self._valid_claims(iss="https://evil.okta.com"))
        resp = handler.handler(self._bootstrap_event(token), None)
        self.assertEqual(resp["statusCode"], 401)
        mock_userinfo.assert_not_called()

    @mock.patch.object(handler, "_fetch_okta_userinfo")
    def test_wrong_client_rejected(self, mock_userinfo):
        token = _make_jwt(self._valid_claims(cid="0oaOTHERAPP"))
        resp = handler.handler(self._bootstrap_event(token), None)
        self.assertEqual(resp["statusCode"], 401)
        mock_userinfo.assert_not_called()

    def test_expired_token_rejected(self):
        token = _make_jwt(self._valid_claims(exp=1000000000))
        resp = handler.handler(self._bootstrap_event(token), None)
        self.assertEqual(resp["statusCode"], 401)

    def test_malformed_token_rejected(self):
        resp = handler.handler(self._bootstrap_event("not-a-jwt"), None)
        self.assertEqual(resp["statusCode"], 401)

    @mock.patch.object(handler, "_fetch_okta_userinfo", side_effect=__import__("urllib.error", fromlist=["HTTPError"]).HTTPError("u", 401, "bad", {}, None))
    def test_userinfo_rejection_propagates_401(self, mock_userinfo):
        token = _make_jwt(self._valid_claims())
        resp = handler.handler(self._bootstrap_event(token), None)
        self.assertEqual(resp["statusCode"], 401)

    @mock.patch.object(handler, "_fetch_okta_userinfo", return_value={"sub": "no-email"})
    def test_missing_email_403(self, mock_userinfo):
        token = _make_jwt(self._valid_claims())
        resp = handler.handler(self._bootstrap_event(token), None)
        self.assertEqual(resp["statusCode"], 403)

    def test_disabled_when_env_missing(self):
        with mock.patch.dict(os.environ, {"DESKTOP_OIDC_CLIENT_ID": ""}):
            resp = handler.handler(self._bootstrap_event("any"), None)
        self.assertEqual(resp["statusCode"], 404)


@_apply_env
class TestWebPortalDisabled(unittest.TestCase):
    def test_portal_shows_info_page_when_web_portal_disabled(self):
        with mock.patch.dict(os.environ, {"WEB_PORTAL_ENABLED": ""}):
            resp = handler.handler({"path": "/portal", "headers": {"host": PORTAL_HOST}}, None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("bootstrap", resp["body"])

    @mock.patch.object(handler, "_fetch_okta_userinfo", return_value={"email": "b@e.com"})
    @mock.patch.object(handler, "_get_or_create_virtual_key", return_value="sk-x")
    def test_bootstrap_still_works_when_web_portal_disabled(self, mock_key, mock_userinfo):
        token = _make_jwt({"iss": "https://test-org.okta.com", "cid": "0oaNATIVETEST", "exp": 9999999999})
        with mock.patch.dict(os.environ, {"WEB_PORTAL_ENABLED": ""}):
            resp = handler.handler(
                {"path": "/portal/bootstrap", "headers": {"authorization": f"Bearer {token}"}}, None)
        self.assertEqual(resp["statusCode"], 200)


@_apply_env
class TestHtmlEscaping(unittest.TestCase):
    def test_key_page_escapes_user_input(self):
        page = handler._key_page("<script>alert(1)</script>", "x@y.com", "sk-key")
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_key_page_includes_cert_section(self):
        page = handler._key_page("User", "x@y.com", "sk-key")
        self.assertIn("/portal/cert", page)
        self.assertIn("add-trusted-cert", page)
        self.assertIn("Import-Certificate", page)


if __name__ == "__main__":
    unittest.main()
