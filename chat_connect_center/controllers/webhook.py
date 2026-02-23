import xml.etree.ElementTree as ET

from odoo import http
from odoo.http import request


class ChatConnectWebhookController(http.Controller):
    @staticmethod
    def _get_payload():
        data = request.httprequest.get_json(silent=True)
        return data or {}

    @staticmethod
    def _json(data, status=200):
        return request.make_json_response(data, status=status)

    @staticmethod
    def _text(data, status=200):
        headers = [("Content-Type", "text/plain; charset=utf-8")]
        return request.make_response(data, headers=headers, status=status)

    @staticmethod
    def _parse_xml_body(raw_body):
        if not raw_body:
            return {}
        root = ET.fromstring(raw_body.decode("utf-8"))
        result = {}
        for child in root:
            result[child.tag] = child.text or ""
        return result

    def _resolve_account(self, platform, webhook_uid):
        return (
            request.env["chat.connect.account"]
            .sudo()
            .search(
                [
                    ("active", "=", True),
                    ("platform", "=", platform),
                    ("webhook_uid", "=", webhook_uid),
                ],
                limit=1,
            )
        )

    @staticmethod
    def _ensure_conversation(account, payload):
        config = request.env["chat.connect.config"].sudo().search([("active", "=", True)], limit=1)
        conversation_ref = (
            payload.get("conversation_id")
            or payload.get("chat_id")
            or payload.get("session_id")
            or payload.get("sender_id")
        )
        if not conversation_ref:
            return None, "conversation_id_required"

        conversation_model = request.env["chat.connect.conversation"].sudo()
        conversation = conversation_model.search(
            [
                ("account_id", "=", account.id),
                ("external_conversation_id", "=", str(conversation_ref)),
            ],
            limit=1,
        )
        if not conversation:
            if config and not config.auto_create_conversation:
                return None, "conversation_not_found"
            conversation = conversation_model.create(
                {
                    "account_id": account.id,
                    "external_conversation_id": str(conversation_ref),
                    "external_visitor_id": str(payload.get("sender_id") or ""),
                    "external_visitor_name": payload.get("sender_name") or payload.get("visitor_name") or "",
                }
            )
        return conversation, None

    @http.route(
        "/chat_connect_center/webhook/<string:platform>/<string:webhook_uid>",
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
    )
    def receive_webhook(self, platform, webhook_uid, **kwargs):
        account = self._resolve_account(platform, webhook_uid)
        if not account:
            return self._json({"ok": False, "error": "account_not_found"}, status=404)

        # WeChat Official Account verification callback (GET)
        if platform in ("wechat", "wechat_service") and request.httprequest.method == "GET":
            args = request.httprequest.args
            signature = args.get("signature")
            timestamp = args.get("timestamp")
            nonce = args.get("nonce")
            echostr = args.get("echostr") or ""
            if account._wechat_verify_signature(signature, timestamp, nonce):
                return self._text(echostr)
            return self._text("invalid signature", status=401)

        raw_body = request.httprequest.get_data() or b""
        # LINE official signature validation
        if platform == "line":
            line_signature = request.httprequest.headers.get("X-Line-Signature")
            if not account._line_verify_signature(raw_body, line_signature):
                return self._json({"ok": False, "error": "invalid_line_signature"}, status=401)
            payload = self._get_payload()
            normalized_events = account._line_parse_events(payload)
            created = []
            for item in normalized_events:
                conversation, error = self._ensure_conversation(account, item)
                if error:
                    continue
                record = dict(item)
                raw_event = record.pop("raw_event", None)
                record["payload"] = raw_event or payload
                message = conversation.ingest_inbound(
                    {
                        "message_id": record.get("message_id"),
                        "sender_id": record.get("sender_id"),
                        "sender_name": record.get("sender_name"),
                        "text": record.get("text"),
                        "conversation_id": record.get("conversation_id"),
                        "reply_token": record.get("reply_token"),
                        "platform": "line",
                        "raw_event": raw_event or {},
                    }
                )
                created.append({"conversation_id": conversation.id, "message_id": message.id})
            return self._json({"ok": True, "count": len(created), "records": created})

        # WeChat official account callback (POST XML)
        if platform in ("wechat", "wechat_service"):
            args = request.httprequest.args
            signature = args.get("signature")
            timestamp = args.get("timestamp")
            nonce = args.get("nonce")
            if not account._wechat_verify_signature(signature, timestamp, nonce):
                return self._text("invalid signature", status=401)

            data = self._parse_xml_body(raw_body)
            if account.wechat_safe_mode_enabled and data.get("Encrypt"):
                msg_signature = args.get("msg_signature")
                encrypt_text = data.get("Encrypt")
                if not account._wechat_verify_msg_signature(msg_signature, timestamp, nonce, encrypt_text):
                    return self._text("invalid msg signature", status=401)
                decrypted_xml = account._wechat_decrypt_message(encrypt_text)
                data = self._parse_xml_body(decrypted_xml.encode("utf-8"))

            msg_type = data.get("MsgType", "")
            text = data.get("Content", "") if msg_type == "text" else ""
            message_type = "text"
            media_id = ""
            if msg_type == "event":
                text = f"[WECHAT EVENT] {data.get('Event', '')}"
                message_type = "event"
            elif msg_type in ("image", "voice", "video", "shortvideo"):
                media_id = data.get("MediaId", "")
                text = f"[WECHAT {msg_type.upper()}]"
                message_type = "audio" if msg_type == "voice" else ("video" if msg_type in ("video", "shortvideo") else "image")
            elif msg_type == "location":
                text = f"[WECHAT LOCATION] {data.get('Location_X','')},{data.get('Location_Y','')}"
                message_type = "event"
            elif msg_type == "link":
                text = f"[WECHAT LINK] {data.get('Title','')}"
                message_type = "file"

            payload = {
                "conversation_id": data.get("FromUserName", ""),
                "sender_id": data.get("FromUserName", ""),
                "sender_name": "",
                "message_id": data.get("MsgId") or data.get("CreateTime") or "",
                "message_type": message_type,
                "media_id": media_id,
                "text": text,
                "platform": "wechat",
                "raw_xml": data,
            }
            conversation, error = self._ensure_conversation(account, payload)
            if error:
                return self._text("success")
            conversation.ingest_inbound(payload)
            # WeChat expects plain text response.
            return self._text("success")

        # Generic fallback
        payload = self._get_payload()
        config = request.env["chat.connect.config"].sudo().search([("active", "=", True)], limit=1)
        given_token = request.httprequest.headers.get("X-Chat-Token")
        enforce_token = True if not config else bool(config.webhook_enforce_token)
        if enforce_token and account.webhook_secret and given_token != account.webhook_secret:
            return self._json({"ok": False, "error": "invalid_token"}, status=401)
        conversation, error = self._ensure_conversation(account, payload)
        if error:
            return self._json({"ok": False, "error": error}, status=400)
        message = conversation.ingest_inbound(payload)
        return self._json({"ok": True, "conversation_id": conversation.id, "message_id": message.id})

    @http.route(
        "/chat_connect_center/webhook/<string:platform>/<string:webhook_uid>/send",
        type="http",
        auth="user",
        methods=["POST"],
        csrf=False,
    )
    def send_message(self, platform, webhook_uid, **kwargs):
        payload = self._get_payload()
        account = (
            request.env["chat.connect.account"]
            .sudo()
            .search(
                [
                    ("active", "=", True),
                    ("platform", "=", platform),
                    ("webhook_uid", "=", webhook_uid),
                ],
                limit=1,
            )
        )
        if not account:
            return self._json({"ok": False, "error": "account_not_found"}, status=404)

        conversation_ref = payload.get("conversation_id")
        if not conversation_ref:
            return self._json({"ok": False, "error": "conversation_id_required"}, status=400)

        conversation = (
            request.env["chat.connect.conversation"]
            .sudo()
            .search(
                [
                    ("account_id", "=", account.id),
                    ("external_conversation_id", "=", str(conversation_ref)),
                ],
                limit=1,
            )
        )
        if not conversation:
            return self._json({"ok": False, "error": "conversation_not_found"}, status=404)

        text = payload.get("text") or ""
        message = (
            request.env["chat.connect.message"]
            .sudo()
            .create(
                {
                    "conversation_id": conversation.id,
                    "direction": "outbound",
                    "text": text,
                    "payload_json": payload,
                }
            )
        )
        message.action_send_outbound()
        return self._json(
            {
                "ok": message.state == "sent",
                "message_id": message.id,
                "state": message.state,
                "error": message.error_message,
            }
        )
