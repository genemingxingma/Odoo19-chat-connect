import logging
import hashlib
import hmac
import json
import base64
import uuid
from datetime import timedelta
import struct

import requests

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class ChatConnectAccount(models.Model):
    _name = "chat.connect.account"
    _description = "Chat Connect Account"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    platform = fields.Selection(
        [
            ("wechat", "WeChat"),
            ("line", "LINE"),
            ("whatsapp", "WhatsApp"),
            ("wecom", "WeCom (Enterprise WeChat)"),
            ("wechat_service", "WeChat Service Account"),
        ],
        required=True,
    )
    webhook_uid = fields.Char(
        default=lambda self: str(uuid.uuid4()),
        copy=False,
        readonly=True,
        required=True,
        index=True,
    )
    webhook_secret = fields.Char(help="Shared secret validated from request header X-Chat-Token.")

    external_app_id = fields.Char(string="External App ID")
    external_app_secret = fields.Char(string="External App Secret")
    external_access_token = fields.Char(string="External Access Token")
    line_channel_secret = fields.Char(string="LINE Channel Secret")
    line_channel_access_token = fields.Char(string="LINE Channel Access Token")
    wechat_token = fields.Char(string="WeChat Verify Token")
    wechat_encoding_aes_key = fields.Char(string="WeChat EncodingAESKey")
    wechat_safe_mode_enabled = fields.Boolean(string="WeChat Safe Mode", default=False)
    wechat_access_token_expires_at = fields.Datetime(copy=False)

    operator_user_ids = fields.Many2many("res.users", string="Operator Users")
    default_channel_id = fields.Many2one("discuss.channel", string="Default Discuss Channel")

    translation_enabled = fields.Boolean(default=False)
    source_lang = fields.Char(default="auto")
    target_lang = fields.Char(default="en")
    translation_endpoint = fields.Char(help="AI translation API endpoint.")
    translation_api_key = fields.Char()
    translation_model = fields.Char(default="gpt-4o-mini")

    outbound_enabled = fields.Boolean(default=True)
    notes = fields.Text()

    _sql_constraints = [
        ("chat_connect_webhook_uid_uniq", "unique(webhook_uid)", "Webhook UID must be unique."),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        config = self.env["chat.connect.config"].sudo().search([("active", "=", True)], limit=1)
        for vals in vals_list:
            if not config:
                continue
            vals.setdefault("source_lang", config.default_source_lang or "auto")
            vals.setdefault("target_lang", config.default_target_lang or "en")
            vals.setdefault("translation_endpoint", config.default_translation_endpoint or False)
            vals.setdefault("translation_api_key", config.default_translation_api_key or False)
            vals.setdefault("translation_model", config.default_translation_model or "gpt-4o-mini")
        return super().create(vals_list)

    def _webhook_url(self):
        self.ensure_one()
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        return f"{base}/chat_connect_center/webhook/{self.platform}/{self.webhook_uid}"

    def _translate_text(self, text):
        self.ensure_one()
        if not text or not self.translation_enabled:
            return ""
        if not self.translation_endpoint:
            _logger.info("Translation enabled but endpoint is empty for account %s", self.id)
            return ""

        headers = {"Content-Type": "application/json"}
        if self.translation_api_key:
            headers["Authorization"] = f"Bearer {self.translation_api_key}"

        payload = {
            "model": self.translation_model,
            "text": text,
            "source_lang": self.source_lang or "auto",
            "target_lang": self.target_lang or "en",
            "task": "translate",
        }

        try:
            response = requests.post(self.translation_endpoint, json=payload, headers=headers, timeout=20)
            response.raise_for_status()
            data = response.json() if response.content else {}
            translated = data.get("translated_text") or data.get("text") or data.get("result") or ""
            return translated.strip()
        except Exception as err:
            _logger.warning("Translation request failed for account %s: %s", self.id, err)
            return ""

    def _send_external_message(self, conversation, text, attachments=None, reply_token=None):
        self.ensure_one()
        if not self.outbound_enabled:
            raise ValueError("Outbound is disabled for this account")

        if self.platform == "line":
            return self._line_send_message(conversation, text, reply_token=reply_token)
        if self.platform in ("wechat", "wechat_service"):
            return self._wechat_send_message(conversation, text)

        _logger.info(
            "[chat_connect_center] outbound message queued platform=%s conv=%s attachments=%s",
            self.platform,
            conversation.external_conversation_id,
            bool(attachments),
        )
        return str(uuid.uuid4())

    def _line_get_access_token(self):
        self.ensure_one()
        return (self.line_channel_access_token or self.external_access_token or "").strip()

    def _line_verify_signature(self, raw_body, signature):
        self.ensure_one()
        channel_secret = (self.line_channel_secret or "").strip()
        if not channel_secret:
            return False
        if not signature:
            return False

        digest = hmac.new(channel_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
        generated = hmac.compare_digest(
            signature.strip(),
            base64.b64encode(digest).decode("utf-8"),
        )
        return generated

    def _line_parse_events(self, payload):
        self.ensure_one()
        events = (payload or {}).get("events") or []
        normalized = []
        for event in events:
            source = event.get("source") or {}
            user_id = source.get("userId") or ""
            conversation_id = user_id or source.get("groupId") or source.get("roomId") or ""
            if not conversation_id:
                continue
            message = event.get("message") or {}
            msg_type = message.get("type") or "event"
            message_text = ""
            if event.get("type") == "message" and msg_type == "text":
                message_text = message.get("text") or ""
            elif event.get("type"):
                message_text = f"[LINE {msg_type.upper()}] {event.get('type')}"

            normalized.append(
                {
                    "conversation_id": conversation_id,
                    "sender_id": user_id,
                    "sender_name": "",
                    "message_id": message.get("id") or event.get("webhookEventId") or "",
                    "message_type": "text" if msg_type == "text" else ("event" if event.get("type") != "message" else msg_type),
                    "media_id": message.get("id") if msg_type != "text" else "",
                    "text": message_text,
                    "reply_token": event.get("replyToken") or "",
                    "raw_event": event,
                }
            )
        return normalized

    def _line_send_message(self, conversation, text, reply_token=None):
        self.ensure_one()
        token = self._line_get_access_token()
        if not token:
            raise ValueError(_("LINE channel access token is not configured"))

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        recipient_user_id = conversation.external_visitor_id or conversation.external_conversation_id
        request_body = {"messages": [{"type": "text", "text": text}]}
        endpoint = ""
        if reply_token:
            endpoint = "https://api.line.me/v2/bot/message/reply"
            request_body["replyToken"] = reply_token
        else:
            endpoint = "https://api.line.me/v2/bot/message/push"
            request_body["to"] = recipient_user_id

        response = requests.post(endpoint, headers=headers, data=json.dumps(request_body), timeout=20)
        if response.status_code >= 400:
            raise ValueError(_("LINE send failed: %s") % response.text)
        return str(uuid.uuid4())

    def _wechat_verify_signature(self, signature, timestamp, nonce):
        self.ensure_one()
        token = (self.wechat_token or self.webhook_secret or "").strip()
        if not token or not signature:
            return False
        arr = [token, timestamp or "", nonce or ""]
        arr.sort()
        sign = hashlib.sha1("".join(arr).encode("utf-8")).hexdigest()
        return sign == (signature or "").strip()

    def _wechat_verify_msg_signature(self, msg_signature, timestamp, nonce, encrypt_text):
        self.ensure_one()
        token = (self.wechat_token or self.webhook_secret or "").strip()
        if not token or not msg_signature or not encrypt_text:
            return False
        arr = [token, timestamp or "", nonce or "", encrypt_text]
        arr.sort()
        sign = hashlib.sha1("".join(arr).encode("utf-8")).hexdigest()
        return sign == (msg_signature or "").strip()

    def _wechat_decrypt_message(self, encrypt_text):
        self.ensure_one()
        aes_key = (self.wechat_encoding_aes_key or "").strip()
        if len(aes_key) != 43:
            raise ValueError(_("WeChat EncodingAESKey is invalid"))
        try:
            from Crypto.Cipher import AES
        except Exception as err:
            raise ValueError(_("pycryptodome is required for WeChat safe mode")) from err

        key = base64.b64decode(aes_key + "=")
        iv = key[:16]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        encrypted = base64.b64decode(encrypt_text)
        plain = cipher.decrypt(encrypted)
        pad_len = plain[-1]
        if isinstance(pad_len, str):  # pragma: no cover
            pad_len = ord(pad_len)
        if pad_len < 1 or pad_len > 32:
            pad_len = 0
        plain = plain[:-pad_len]

        msg_len = struct.unpack("!I", plain[16:20])[0]
        xml_bytes = plain[20 : 20 + msg_len]
        from_appid = plain[20 + msg_len :].decode("utf-8")
        expected_appid = (self.external_app_id or "").strip()
        if expected_appid and from_appid != expected_appid:
            raise ValueError(_("WeChat AppID mismatch in encrypted payload"))
        return xml_bytes.decode("utf-8")

    def _wechat_get_access_token(self):
        self.ensure_one()
        now = fields.Datetime.now()
        if self.external_access_token and self.wechat_access_token_expires_at and self.wechat_access_token_expires_at > now:
            return self.external_access_token

        app_id = (self.external_app_id or "").strip()
        app_secret = (self.external_app_secret or "").strip()
        if not app_id or not app_secret:
            raise ValueError(_("WeChat app id/secret is not configured"))

        url = "https://api.weixin.qq.com/cgi-bin/token"
        params = {
            "grant_type": "client_credential",
            "appid": app_id,
            "secret": app_secret,
        }
        response = requests.get(url, params=params, timeout=20)
        data = response.json() if response.content else {}
        access_token = data.get("access_token")
        expires_in = int(data.get("expires_in") or 7200)
        if not access_token:
            raise ValueError(_("WeChat access_token request failed: %s") % (response.text or data))

        self.write(
            {
                "external_access_token": access_token,
                "wechat_access_token_expires_at": now + timedelta(seconds=max(expires_in - 300, 60)),
            }
        )
        return access_token

    def _wechat_send_message(self, conversation, text):
        self.ensure_one()
        access_token = self._wechat_get_access_token()
        openid = conversation.external_visitor_id or conversation.external_conversation_id
        if not openid:
            raise ValueError(_("WeChat recipient openid is empty"))

        endpoint = "https://api.weixin.qq.com/cgi-bin/message/custom/send"
        payload = {
            "touser": openid,
            "msgtype": "text",
            "text": {"content": text},
        }
        response = requests.post(endpoint, params={"access_token": access_token}, json=payload, timeout=20)
        data = response.json() if response.content else {}
        if data.get("errcode", 0) != 0:
            raise ValueError(_("WeChat send failed: %s") % (response.text or data))
        return str(uuid.uuid4())
