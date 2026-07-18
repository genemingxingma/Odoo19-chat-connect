import base64
import hashlib
import hmac
from unittest.mock import Mock, patch

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged

from ..controllers.webhook import ChatConnectWebhookController
from ..models.provider_errors import ChatConnectPermanentError


@tagged("post_install", "-at_install")
class TestChatConnectCore(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator = new_test_user(
            cls.env,
            login="chat_connect_operator_test",
            groups="base.group_user",
        )
        cls.other_user = new_test_user(
            cls.env,
            login="chat_connect_other_test",
            groups="base.group_user",
        )
        cls.account = cls.env["chat.connect.account"].create(
            {
                "name": "Test LINE",
                "platform": "line",
                "line_channel_secret": "test-secret",
                "line_channel_access_token": "test-token",
                "operator_user_ids": [Command.set(cls.operator.ids)],
            }
        )

    def _conversation(self, external_id, **values):
        return self.env["chat.connect.conversation"].create(
            {
                "account_id": self.account.id,
                "external_conversation_id": external_id,
                "external_visitor_id": values.pop("external_visitor_id", external_id),
                **values,
            }
        )

    def test_line_signature_and_group_event_mapping(self):
        raw_body = b'{"events":[]}'
        signature = base64.b64encode(
            hmac.new(b"test-secret", raw_body, hashlib.sha256).digest()
        ).decode()
        self.assertTrue(self.account._line_verify_signature(raw_body, signature))
        self.assertFalse(self.account._line_verify_signature(raw_body, "invalid"))

        events = self.account._line_parse_events(
            {
                "events": [
                    {
                        "type": "message",
                        "webhookEventId": "evt-group-1",
                        "source": {
                            "type": "group",
                            "groupId": "group-100",
                            "userId": "user-200",
                        },
                        "message": {"id": "msg-1", "type": "text", "text": "hello"},
                    }
                ]
            }
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["conversation_id"], "group-100")
        self.assertEqual(events[0]["conversation_type"], "group")
        self.assertEqual(events[0]["sender_id"], "user-200")
        self.assertEqual(events[0]["event_uid"], "evt-group-1")

    def test_line_group_profile_uses_official_member_endpoint(self):
        response = Mock(status_code=200)
        response.json.return_value = {"displayName": "Alice"}
        with patch(
            "odoo.addons.chat_connect_center.models.chat_connect_account.requests.get",
            return_value=response,
        ) as request_get:
            profile = self.account._line_get_profile(
                "user/200",
                conversation_type="group",
                conversation_id="group/100",
            )
        self.assertEqual(profile["displayName"], "Alice")
        self.assertEqual(
            request_get.call_args.args[0],
            "https://api.line.me/v2/bot/group/group%2F100/member/user%2F200",
        )

    def test_inbound_queue_is_idempotent(self):
        payload = {
            "event_uid": "event-idempotent-1",
            "message_id": "message-idempotent-1",
            "conversation_id": "visitor-idempotent",
            "sender_id": "visitor-idempotent",
            "message_type": "text",
            "text": "hello",
        }
        first, first_created = self.env["chat.connect.inbound.event"].enqueue(
            self.account,
            payload["event_uid"],
            payload,
        )
        second, second_created = self.env["chat.connect.inbound.event"].enqueue(
            self.account,
            payload["event_uid"],
            payload,
        )
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first, second)
        self.assertEqual(
            self.env["chat.connect.inbound.event"].search_count(
                [("account_id", "=", self.account.id), ("event_uid", "=", payload["event_uid"])]
            ),
            1,
        )

    def test_idempotency_keys_exist_only_for_outbound_messages(self):
        conversation = self._conversation("visitor-key-scope")
        inbound = self.env["chat.connect.message"].create(
            {
                "conversation_id": conversation.id,
                "direction": "inbound",
                "state": "received",
                "text": "incoming",
            }
        )
        outbound_a = self.env["chat.connect.message"].create(
            {
                "conversation_id": conversation.id,
                "direction": "outbound",
                "text": "outgoing A",
            }
        )
        outbound_b = self.env["chat.connect.message"].create(
            {
                "conversation_id": conversation.id,
                "direction": "outbound",
                "text": "outgoing B",
            }
        )
        self.assertFalse(inbound.idempotency_key)
        self.assertTrue(outbound_a.idempotency_key)
        self.assertTrue(outbound_b.idempotency_key)
        self.assertNotEqual(outbound_a.idempotency_key, outbound_b.idempotency_key)

    def test_one_conversation_one_channel_prevents_wrong_recipient(self):
        conversation_a = self._conversation("visitor-a")
        conversation_b = self._conversation("visitor-b")
        channel_a = conversation_a._ensure_discuss_channel()
        channel_b = conversation_b._ensure_discuss_channel()
        self.assertNotEqual(channel_a, channel_b)
        self.assertEqual(channel_a.chat_connect_conversation_id, conversation_a)
        self.assertEqual(channel_b.chat_connect_conversation_id, conversation_b)

        channel_a.with_user(self.operator).message_post(
            body="reply only to A",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
        )
        outbound = self.env["chat.connect.message"].search(
            [("direction", "=", "outbound"), ("text", "=", "reply only to A")]
        )
        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound.conversation_id, conversation_a)
        self.assertNotEqual(outbound.conversation_id, conversation_b)
        self.assertEqual(outbound.state, "queued")

    def test_triage_channel_cannot_be_customer_channel_or_send(self):
        triage = self.env["discuss.channel"].create(
            {"name": "Shared triage", "channel_type": "channel"}
        )
        self.account.default_channel_id = triage
        conversation = self._conversation("visitor-triage")
        with self.assertRaises(ValidationError):
            conversation.mail_channel_id = triage

        triage.add_members(partner_ids=self.operator.partner_id.ids, post_joined_message=False)
        triage.with_user(self.operator).message_post(
            body="internal triage note",
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
        )
        self.assertFalse(
            self.env["chat.connect.message"].search(
                [("direction", "=", "outbound"), ("text", "=", "internal triage note")]
            )
        )

    def test_non_member_author_cannot_enqueue_outbound(self):
        conversation = self._conversation("visitor-member-check")
        channel = conversation._ensure_discuss_channel()
        self.env["mail.message"].create(
            {
                "model": "discuss.channel",
                "res_id": channel.id,
                "message_type": "comment",
                "subtype_id": self.env.ref("mail.mt_comment").id,
                "author_id": self.other_user.partner_id.id,
                "body": "unauthorized reply",
            }
        )
        self.assertFalse(
            self.env["chat.connect.message"].search(
                [("direction", "=", "outbound"), ("text", "=", "unauthorized reply")]
            )
        )

    def test_operator_cannot_read_provider_secret(self):
        self.assertTrue(self.operator.has_group("chat_connect_center.group_chat_connect_user"))
        with self.assertRaises(AccessError):
            self.account.with_user(self.operator).read(["line_channel_access_token"])

    def test_line_push_uses_retry_key_and_archives_provider_ids(self):
        conversation = self._conversation("line-recipient")
        response = Mock(status_code=200, content=b"{}", text="")
        response.json.return_value = {"sentMessages": [{"id": "line-message-1"}]}
        response.headers = {"x-line-request-id": "line-request-1"}
        with patch(
            "odoo.addons.chat_connect_center.models.chat_connect_account.requests.post",
            return_value=response,
        ) as request_post:
            result = self.account._line_send_message(
                conversation,
                "hello",
                idempotency_key="retry-key-1",
            )
        self.assertEqual(request_post.call_args.args[0], "https://api.line.me/v2/bot/message/push")
        self.assertEqual(request_post.call_args.kwargs["headers"]["X-Line-Retry-Key"], "retry-key-1")
        self.assertEqual(request_post.call_args.kwargs["json"]["to"], "line-recipient")
        self.assertEqual(result["external_message_id"], "line-message-1")
        self.assertEqual(result["provider_request_id"], "line-request-1")

    def test_exhausted_legacy_failure_is_marked_permanent(self):
        conversation = self._conversation("legacy-failure")
        message = self.env["chat.connect.message"].create(
            {
                "conversation_id": conversation.id,
                "direction": "outbound",
                "state": "failed",
                "text": "legacy failed message",
                "retry_count": 5,
                "max_retries": 5,
                "next_retry_at": False,
            }
        )
        self.env["chat.connect.message"]._cron_retry_failed_outbound()
        self.assertEqual(message.state, "permanent_failed")

    def test_wechat_requires_active_customer_service_window(self):
        wechat = self.env["chat.connect.account"].create(
            {
                "name": "Test WeChat",
                "platform": "wechat",
                "operator_user_ids": [Command.set(self.operator.ids)],
            }
        )
        conversation = self.env["chat.connect.conversation"].create(
            {
                "account_id": wechat.id,
                "external_conversation_id": "openid-1",
                "external_visitor_id": "openid-1",
            }
        )
        with self.assertRaises(ChatConnectPermanentError):
            conversation._check_wechat_outbound_window()
        conversation.wechat_outbound_window_expires_at = fields.Datetime.now()
        with self.assertRaises(ChatConnectPermanentError):
            conversation._check_wechat_outbound_window()

    def test_wechat_safe_mode_does_not_fallback_to_plain_signature(self):
        token = "wechat-test-token"
        timestamp = "1712345678"
        nonce = "nonce-1"
        encrypt_text = "encrypted-payload"
        wechat = self.env["chat.connect.account"].create(
            {
                "name": "Safe WeChat",
                "platform": "wechat",
                "wechat_token": token,
                "wechat_safe_mode_enabled": True,
                "operator_user_ids": [Command.set(self.operator.ids)],
            }
        )
        signature = hashlib.sha1("".join(sorted([token, timestamp, nonce])).encode()).hexdigest()
        msg_signature = hashlib.sha1(
            "".join(sorted([token, timestamp, nonce, encrypt_text])).encode()
        ).hexdigest()

        self.assertFalse(
            wechat._wechat_verify_callback_signature(
                signature=signature,
                timestamp=timestamp,
                nonce=nonce,
            )
        )
        self.assertTrue(
            wechat._wechat_verify_callback_signature(
                msg_signature=msg_signature,
                timestamp=timestamp,
                nonce=nonce,
                encrypt_text=encrypt_text,
            )
        )
        wechat.wechat_safe_mode_enabled = False
        self.assertTrue(
            wechat._wechat_verify_callback_signature(
                signature=signature,
                timestamp=timestamp,
                nonce=nonce,
            )
        )

    def test_ai_automation_is_exclusive_with_chatbot(self):
        conversation = self._conversation("visitor-ai")
        model_type = type(conversation)
        with patch.object(model_type, "_trigger_livechat_ai_agent", return_value=True), patch.object(
            model_type,
            "_trigger_livechat_chatbot",
            return_value=True,
        ) as chatbot:
            conversation._trigger_livechat_automation(False, False, "question")
        chatbot.assert_not_called()

    def test_diagnostic_sanitizer_redacts_nested_secrets(self):
        controller = ChatConnectWebhookController()
        sanitized = controller._sanitize_for_log(
            {
                "authorization": "Bearer secret",
                "nested": {"access_token": "token", "safe": "visible"},
            }
        )
        self.assertEqual(sanitized["authorization"], "[redacted]")
        self.assertEqual(sanitized["nested"]["access_token"], "[redacted]")
        self.assertEqual(sanitized["nested"]["safe"], "visible")
