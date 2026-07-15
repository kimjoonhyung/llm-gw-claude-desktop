"""okta-events Lambda 핸들러 단위 테스트

실행: cd lambda/okta-events && python3 -m unittest discover -s tests

Unit tests for the okta-events Lambda handler.

Run: cd lambda/okta-events && python3 -m unittest discover -s tests
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TEST_ENV = {
    "WEBHOOK_SECRET_ARN": "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:webhook",
    "LITELLM_MASTER_KEY_ARN": "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:master",
    "LITELLM_ENDPOINT": "https://gateway.example.com",
    "CONFIG_TABLE_NAME": "llm-gw-gs-config",
    "OKTA_APP_LABEL": "LLM Gateway Key Portal",
    "OKTA_GROUP_LABEL": "llm-gateway-users",
    "AWS_DEFAULT_REGION": "ap-northeast-2",
}

with mock.patch.dict(os.environ, TEST_ENV):
    with mock.patch("boto3.resource"), mock.patch("boto3.client"):
        import handler

handler._webhook_secret_cache = "test-webhook-secret"
handler._master_key_cache = "test-master-key"


def _apply_env(test):
    return mock.patch.dict(os.environ, TEST_ENV)(test)


def _post_event(events, auth="test-webhook-secret"):
    return {
        "httpMethod": "POST",
        "headers": {"Authorization": auth},
        "body": json.dumps({"data": {"events": events}}),
    }


def _okta_event(event_type, email="alice@example.com", app_label=None, group_label=None):
    targets = [{"type": "User", "alternateId": email}]
    if app_label:
        targets.append({"type": "AppInstance", "displayName": app_label})
    if group_label:
        targets.append({"type": "UserGroup", "displayName": group_label})
    return {"eventType": event_type, "target": targets}


@_apply_env
class TestVerification(unittest.TestCase):
    def test_okta_challenge_echo(self):
        event = {
            "httpMethod": "GET",
            "headers": {"X-Okta-Verification-Challenge": "abc-123"},
        }
        resp = handler.handler(event, None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(json.loads(resp["body"])["verification"], "abc-123")

    def test_get_without_challenge_is_400(self):
        resp = handler.handler({"httpMethod": "GET", "headers": {}}, None)
        self.assertEqual(resp["statusCode"], 400)


@_apply_env
class TestAuth(unittest.TestCase):
    def test_wrong_secret_rejected(self):
        resp = handler.handler(_post_event([], auth="wrong"), None)
        self.assertEqual(resp["statusCode"], 401)

    def test_missing_auth_rejected(self):
        event = _post_event([])
        event["headers"] = {}
        resp = handler.handler(event, None)
        self.assertEqual(resp["statusCode"], 401)


@_apply_env
class TestDeprovisionEvents(unittest.TestCase):
    @mock.patch.object(handler, "_revoke_user_access")
    def test_user_deactivate_revokes(self, mock_revoke):
        resp = handler.handler(_post_event([_okta_event("user.lifecycle.deactivate")]), None)
        self.assertEqual(resp["statusCode"], 200)
        mock_revoke.assert_called_once_with("alice@example.com")

    @mock.patch.object(handler, "_revoke_user_access")
    def test_user_suspend_revokes(self, mock_revoke):
        handler.handler(_post_event([_okta_event("user.lifecycle.suspend")]), None)
        mock_revoke.assert_called_once_with("alice@example.com")

    @mock.patch.object(handler, "_revoke_user_access")
    def test_app_unassign_for_gateway_app_revokes(self, mock_revoke):
        e = _okta_event("application.user_membership.remove", app_label="LLM Gateway Key Portal")
        handler.handler(_post_event([e]), None)
        mock_revoke.assert_called_once_with("alice@example.com")

    @mock.patch.object(handler, "_revoke_user_access")
    def test_app_unassign_for_other_app_ignored(self, mock_revoke):
        e = _okta_event("application.user_membership.remove", app_label="Some Other SaaS")
        handler.handler(_post_event([e]), None)
        mock_revoke.assert_not_called()

    @mock.patch.object(handler, "_revoke_user_access")
    def test_unrelated_event_ignored(self, mock_revoke):
        handler.handler(_post_event([_okta_event("user.session.start")]), None)
        mock_revoke.assert_not_called()

    @mock.patch.object(handler, "_revoke_user_access")
    def test_group_remove_for_gateway_group_revokes(self, mock_revoke):
        e = _okta_event("group.user_membership.remove", group_label="llm-gateway-users")
        handler.handler(_post_event([e]), None)
        mock_revoke.assert_called_once_with("alice@example.com")

    @mock.patch.object(handler, "_revoke_user_access")
    def test_group_remove_for_other_group_ignored(self, mock_revoke):
        e = _okta_event("group.user_membership.remove", group_label="some-other-team")
        handler.handler(_post_event([e]), None)
        mock_revoke.assert_not_called()

    @mock.patch.object(handler, "_revoke_user_access")
    def test_email_normalized_to_lowercase(self, mock_revoke):
        handler.handler(_post_event([_okta_event("user.lifecycle.deactivate", email="Alice@Example.COM")]), None)
        mock_revoke.assert_called_once_with("alice@example.com")


@_apply_env
class TestRevoke(unittest.TestCase):
    @mock.patch.object(handler, "_litellm_request")
    def test_deletes_all_user_keys_and_cache(self, mock_req):
        mock_req.side_effect = [
            {"keys": [{"token": "sk-1"}, {"token": "sk-2"}]},  # /user/info
            {},  # /key/delete
        ]
        mock_table = mock.MagicMock()
        with mock.patch.object(handler._dynamodb, "Table", return_value=mock_table):
            handler._revoke_user_access("alice@example.com")

        delete_call = mock_req.call_args_list[1]
        self.assertEqual(delete_call[0][1], "/key/delete")
        self.assertEqual(delete_call[1]["body"], {"keys": ["sk-1", "sk-2"]})
        mock_table.delete_item.assert_called_once_with(
            Key={"pk": "USER#alice@example.com", "sk": "VIRTUAL_KEY"})

    @mock.patch.object(handler, "_litellm_request")
    def test_no_keys_still_clears_cache(self, mock_req):
        mock_req.return_value = {"keys": []}
        mock_table = mock.MagicMock()
        with mock.patch.object(handler._dynamodb, "Table", return_value=mock_table):
            handler._revoke_user_access("bob@example.com")
        # /user/info(빈 keys) -> /key/list 폴백 없음... user/info가 성공했으므로 1회
        # /user/info (empty keys) -> no /key/list fallback... 1 call since user/info succeeded
        self.assertEqual(mock_req.call_count, 1)  # /key/delete 호출 안 함 / /key/delete not called
        mock_table.delete_item.assert_called_once()

    @mock.patch.object(handler, "_litellm_request")
    def test_user_not_found_falls_back_to_key_list(self, mock_req):
        import urllib.error
        mock_req.side_effect = [
            urllib.error.HTTPError("u", 404, "not found", {}, None),  # /user/info
            {"keys": [{"token": "sk-orphan"}]},                        # /key/list
            {},                                                        # /key/delete
        ]
        mock_table = mock.MagicMock()
        with mock.patch.object(handler._dynamodb, "Table", return_value=mock_table):
            handler._revoke_user_access("orphan@example.com")
        self.assertEqual(mock_req.call_args_list[1][0][1],
                         "/key/list?user_id=orphan%40example.com&return_full_object=true")
        self.assertEqual(mock_req.call_args_list[2][1]["body"], {"keys": ["sk-orphan"]})


if __name__ == "__main__":
    unittest.main()
