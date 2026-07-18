import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import struct
import uuid
from datetime import timedelta
from urllib.parse import quote

import requests

from odoo import _, api, fields, models, Command

from .provider_errors import (
    ChatConnectDeliveryUncertain,
    ChatConnectPermanentError,
    ChatConnectTransientError,
)


_logger = logging.getLogger(__name__)


class ChatConnectAccount(models.Model):
    _name = "chat.connect.account"
    _description = "Chat Connect Account"
    _check_company_auto = True

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        domain=lambda self: [("id", "in", self.env.companies.ids)],
        index=True,
    )
    platform = fields.Selection(
        [
            ("wechat", "WeChat"),
            ("line", "LINE"),
            ("whatsapp", "WhatsApp (Generic Webhook)"),
            ("wecom", "WeCom (Generic Webhook)"),
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
        groups="chat_connect_center.group_chat_connect_manager",
    )
    webhook_url = fields.Char(
        compute="_compute_webhook_urls",
        string="Webhook URL",
        groups="chat_connect_center.group_chat_connect_manager",
    )
    webhook_send_url = fields.Char(
        compute="_compute_webhook_urls",
        string="Webhook Send URL",
        groups="chat_connect_center.group_chat_connect_manager",
    )
    webhook_secret = fields.Char(
        help="Shared secret required by generic webhook endpoints.",
        groups="chat_connect_center.group_chat_connect_manager",
    )

    external_app_id = fields.Char(string="External App ID", groups="chat_connect_center.group_chat_connect_manager")
    external_app_secret = fields.Char(string="External App Secret", groups="chat_connect_center.group_chat_connect_manager")
    external_access_token = fields.Char(string="External Access Token", copy=False, groups="chat_connect_center.group_chat_connect_manager")
    line_channel_secret = fields.Char(string="LINE Channel Secret", groups="chat_connect_center.group_chat_connect_manager")
    line_channel_access_token = fields.Char(string="LINE Channel Access Token", groups="chat_connect_center.group_chat_connect_manager")
    wechat_token = fields.Char(string="WeChat Verify Token", groups="chat_connect_center.group_chat_connect_manager")
    wechat_encoding_aes_key = fields.Char(string="WeChat EncodingAESKey", groups="chat_connect_center.group_chat_connect_manager")
    wechat_safe_mode_enabled = fields.Boolean(string="WeChat Safe Mode", default=False)
    wechat_use_stable_token = fields.Boolean(string="Use WeChat Stable Token", default=True)
    wechat_access_token_expires_at = fields.Datetime(copy=False, groups="chat_connect_center.group_chat_connect_manager")

    operator_user_ids = fields.Many2many(
        "res.users",
        string="Operator Users",
        domain=[("share", "=", False)],
    )
    default_channel_id = fields.Many2one(
        "discuss.channel",
        string="Triage Notification Channel",
        help="Optional notification channel. It is never used as the outbound customer conversation.",
    )
    livechat_channel_id = fields.Many2one("im_livechat.channel", string="Odoo Livechat Channel")
    chatbot_script_id = fields.Many2one(
        "chatbot.script",
        string="Legacy Chatbot Script",
        help="Deprecated. Automation is resolved from the selected Livechat channel rules.",
    )

    translation_enabled = fields.Boolean(default=False)
    outbound_translation_enabled = fields.Boolean(
        string="Translate Outbound Messages",
        default=False,
        help="Translate operator replies to the detected customer language before provider delivery.",
    )
    source_lang = fields.Char(default="auto")
    target_lang = fields.Char(default="en")
    translation_endpoint = fields.Char(
        help="AI translation API endpoint.",
        groups="chat_connect_center.group_chat_connect_manager",
    )
    translation_api_key = fields.Char(groups="chat_connect_center.group_chat_connect_manager")
    translation_model = fields.Char(default="gpt-4o-mini")

    outbound_enabled = fields.Boolean(default=True)
    last_webhook_at = fields.Datetime(readonly=True)
    last_inbound_at = fields.Datetime(readonly=True)
    last_outbound_at = fields.Datetime(readonly=True)
    last_error_at = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)
    connection_status = fields.Selection(
        [("not_tested", "Not Tested"), ("connected", "Connected"), ("error", "Error")],
        default="not_tested",
        readonly=True,
        index=True,
    )
    notes = fields.Text()

    _webhook_uid_unique = models.Constraint(
        "UNIQUE(webhook_uid)",
        "Webhook UID must be unique.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            company = self.env["res.company"].browse(vals.get("company_id")) or self.env.company
            config = self.env["chat.connect.config"].get_active(company)
            if not config:
                continue
            vals.setdefault("source_lang", config.default_source_lang or "auto")
            vals.setdefault("target_lang", config.default_target_lang or "en")
            vals.setdefault("translation_endpoint", config.default_translation_endpoint or False)
            vals.setdefault("translation_api_key", config.default_translation_api_key or False)
            vals.setdefault("translation_model", config.default_translation_model or "gpt-4o-mini")
        records = super().create(vals_list)
        records._sync_default_channel_members()
        return records

    def write(self, vals):
        res = super().write(vals)
        if "default_channel_id" in vals or "operator_user_ids" in vals:
            self._sync_default_channel_members()
        return res

    def _sync_default_channel_members(self):
        member_model = self.env["discuss.channel.member"].sudo()
        operator_group = self.env.ref(
            "chat_connect_center.group_chat_connect_user",
            raise_if_not_found=False,
        )
        now = fields.Datetime.now()
        for record in self:
            channel = record.default_channel_id.sudo()
            operators = record.operator_user_ids.filtered(lambda user: not user.share)
            if operator_group and operators:
                operators.sudo().write({"group_ids": [Command.link(operator_group.id)]})
            partner_ids = operators.partner_id.ids
            if not channel or not partner_ids:
                continue
            channel.add_members(partner_ids=partner_ids, post_joined_message=False)
            members = member_model.search(
                [("channel_id", "=", channel.id), ("partner_id", "in", partner_ids)]
            )
            members.write({"unpin_dt": False, "last_interest_dt": now})

    def _config(self):
        self.ensure_one()
        return self.env["chat.connect.config"].get_active(self.company_id)

    def _webhook_url(self):
        self.ensure_one()
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")
        return f"{base}/chat_connect_center/webhook/{self.platform}/{self.webhook_uid}"

    @api.depends("platform", "webhook_uid")
    def _compute_webhook_urls(self):
        base = (self.env["ir.config_parameter"].sudo().get_param("web.base.url", "") or "").rstrip("/")
        for record in self:
            if not base or not record.platform or not record.webhook_uid:
                record.webhook_url = False
                record.webhook_send_url = False
            else:
                record.webhook_url = f"{base}/chat_connect_center/webhook/{record.platform}/{record.webhook_uid}"
                record.webhook_send_url = f"{record.webhook_url}/send"

    def _translation_payload(self, text, source_lang, target_lang):
        endpoint = (self.translation_endpoint or "").lower()
        if endpoint.rstrip("/").endswith("/v1/chat/completions"):
            return {
                "model": self.translation_model,
                "temperature": 0,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Translate the user text faithfully. Return only the translation. "
                            f"Source language: {source_lang}; target language: {target_lang}."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
            }
        if endpoint.rstrip("/").endswith("/v1/responses"):
            return {
                "model": self.translation_model,
                "instructions": (
                    "Translate faithfully and return only the translation. "
                    f"Source language: {source_lang}; target language: {target_lang}."
                ),
                "input": text,
            }
        return {
            "model": self.translation_model,
            "text": text,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "task": "translate",
        }

    @staticmethod
    def _translation_result(data):
        if not isinstance(data, dict):
            return ""
        result = data.get("translated_text") or data.get("text") or data.get("result") or data.get("output_text")
        if result:
            return str(result).strip()
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            return str(message.get("content") or "").strip()
        output = data.get("output") or []
        parts = []
        for item in output:
            for content in item.get("content") or []:
                if content.get("text"):
                    parts.append(content["text"])
        return "\n".join(parts).strip()

    def _translate_text(self, text, source_lang=None, target_lang=None):
        self.ensure_one()
        if not text or not self.translation_enabled or not self.translation_endpoint:
            return ""
        source_lang = source_lang or self.source_lang or "auto"
        target_lang = target_lang or self.target_lang or "en"
        if source_lang != "auto" and source_lang.lower() == target_lang.lower():
            return text
        headers = {"Content-Type": "application/json"}
        if self.translation_api_key:
            headers["Authorization"] = f"Bearer {self.translation_api_key}"
        try:
            response = requests.post(
                self.translation_endpoint,
                json=self._translation_payload(text, source_lang, target_lang),
                headers=headers,
                timeout=20,
            )
            response.raise_for_status()
            return self._translation_result(response.json() if response.content else {})
        except Exception as err:
            _logger.warning("Translation request failed for account %s: %s", self.id, err)
            return ""

    def _send_external_message(
        self,
        conversation,
        text,
        attachments=None,
        reply_token=None,
        idempotency_key=None,
        outbound_message=None,
    ):
        self.ensure_one()
        if not self.outbound_enabled:
            raise ChatConnectPermanentError(_("Outbound is disabled for this account"))
        if self.platform == "line":
            return self._line_send_message(
                conversation,
                text,
                attachments=attachments,
                reply_token=reply_token,
                idempotency_key=idempotency_key,
                outbound_message=outbound_message,
            )
        if self.platform in ("wechat", "wechat_service"):
            return self._wechat_send_message(
                conversation,
                text,
                attachments=attachments,
                outbound_message=outbound_message,
            )
        raise ChatConnectPermanentError(
            _("Outbound sending is not implemented for platform: %s") % self.platform
        )

    def _create_livechat_discuss_channel(self, conversation):
        self.ensure_one()
        if not self.livechat_channel_id:
            return False, False
        livechat_channel = self.livechat_channel_id.sudo()
        rule_model = self.env["im_livechat.channel.rule"].sudo()
        rule = rule_model.match_rule(
            channel_id=livechat_channel.id,
            url=f"{self.platform}://chat",
            country_id=False,
        ) or livechat_channel.rule_ids[:1]
        chatbot_script = rule.chatbot_script_id if rule else self.env["chatbot.script"]
        ai_agent_id = rule.ai_agent_id.id if rule and getattr(rule, "ai_agent_id", False) else False
        customer_lang = conversation.customer_lang or self.source_lang or "en_US"
        operator_info = livechat_channel._get_operator_info(
            previous_operator_id=None,
            chatbot_script_id=chatbot_script.id or False,
            ai_agent_id=ai_agent_id,
            country_id=False,
            lang=customer_lang,
        )
        if not operator_info.get("operator_partner"):
            raise ChatConnectPermanentError(
                _("No available operator or chatbot for selected Odoo Livechat Channel.")
            )
        channel_vals = livechat_channel._get_livechat_discuss_channel_vals(**operator_info)
        lang = self.env["res.lang"].search([("code", "=", customer_lang)], limit=1)
        if not lang and "_" not in customer_lang:
            lang = self.env["res.lang"].search([("code", "ilike", f"{customer_lang}_%")], limit=1)
        channel_vals.update(
            {
                "country_id": False,
                "livechat_lang_id": lang.id or False,
                "chat_connect_conversation_id": conversation.id,
            }
        )
        guest = self.env["mail.guest"].sudo().create(
            {"name": conversation.external_visitor_name or conversation.external_visitor_id or _("Visitor")}
        )
        channel_vals["channel_member_ids"] = list(channel_vals.get("channel_member_ids", [])) + [
            Command.create({"livechat_member_type": "visitor", "guest_id": guest.id})
        ]
        channel = self.env["discuss.channel"].sudo().create(channel_vals)
        operator_partner_ids = self.operator_user_ids.partner_id.ids
        if operator_partner_ids:
            channel.add_members(partner_ids=operator_partner_ids, post_joined_message=False)
        return channel, guest

    # LINE adapter
    def _line_get_access_token(self):
        self.ensure_one()
        return (self.line_channel_access_token or self.external_access_token or "").strip()

    def _line_verify_signature(self, raw_body, signature):
        self.ensure_one()
        channel_secret = (self.line_channel_secret or "").strip()
        if not channel_secret or not signature:
            return False
        digest = hmac.new(channel_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
        return hmac.compare_digest(signature.strip(), base64.b64encode(digest).decode("utf-8"))

    def _line_parse_events(self, payload):
        self.ensure_one()
        normalized = []
        for event in (payload or {}).get("events") or []:
            source = event.get("source") or {}
            source_type = source.get("type") or "user"
            user_id = source.get("userId") or ""
            if source_type == "group":
                conversation_id = source.get("groupId") or ""
            elif source_type == "room":
                conversation_id = source.get("roomId") or ""
            else:
                conversation_id = user_id
            if not conversation_id:
                continue
            message = event.get("message") or {}
            event_type = event.get("type") or "event"
            provider_type = message.get("type") or "event"
            message_type = provider_type if provider_type in ("text", "image", "file", "audio", "video") else "event"
            text = (message.get("text") or "") if provider_type == "text" else ""
            if provider_type == "location":
                title = message.get("title") or message.get("address") or "Location"
                text = f"{title}: {message.get('latitude', '')},{message.get('longitude', '')}"
            elif provider_type == "sticker":
                text = f"[LINE sticker {message.get('packageId', '')}/{message.get('stickerId', '')}]"
            elif event_type != "message":
                text = f"[LINE {event_type}]"
            elif not text and message_type != "text":
                text = f"[LINE {provider_type}]"
            event_uid = event.get("webhookEventId") or message.get("id") or hashlib.sha256(
                json.dumps(event, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            normalized.append(
                {
                    "event_uid": event_uid,
                    "event_timestamp_ms": event.get("timestamp"),
                    "conversation_id": conversation_id,
                    "conversation_type": source_type,
                    "sender_id": user_id,
                    "sender_name": "",
                    "message_id": message.get("id") or event_uid,
                    "message_type": message_type,
                    "provider_message_type": provider_type,
                    "media_id": message.get("id") if message_type in ("image", "file", "audio", "video") else "",
                    "file_name": message.get("fileName") or "",
                    "text": text,
                    "reply_token": event.get("replyToken") or "",
                    "quote_token": message.get("quoteToken") or "",
                    "is_redelivery": bool((event.get("deliveryContext") or {}).get("isRedelivery")),
                    "platform": "line",
                    "raw_event": event,
                }
            )
        return normalized

    def _line_headers(self):
        token = self._line_get_access_token()
        if not token:
            raise ChatConnectPermanentError(_("LINE channel access token is not configured"))
        return {"Authorization": f"Bearer {token}"}

    def _line_get_profile(self, user_id, conversation_type="user", conversation_id=""):
        self.ensure_one()
        if not user_id:
            return {}
        escaped_user = quote(user_id, safe="")
        if conversation_type == "group" and conversation_id:
            endpoint = (
                f"https://api.line.me/v2/bot/group/{quote(conversation_id, safe='')}"
                f"/member/{escaped_user}"
            )
        elif conversation_type == "room" and conversation_id:
            endpoint = (
                f"https://api.line.me/v2/bot/room/{quote(conversation_id, safe='')}"
                f"/member/{escaped_user}"
            )
        else:
            endpoint = f"https://api.line.me/v2/bot/profile/{escaped_user}"
        try:
            response = requests.get(
                endpoint,
                headers=self._line_headers(),
                timeout=10,
            )
            if response.status_code == 200:
                return response.json()
        except requests.RequestException:
            _logger.info("Unable to fetch LINE profile for account %s", self.id)
        return {}

    def _line_download_content(self, message_id):
        self.ensure_one()
        config = self._config()
        max_bytes = (config.max_media_mb if config else 20) * 1024 * 1024
        try:
            response = requests.get(
                f"https://api-data.line.me/v2/bot/message/{quote(message_id, safe='')}/content",
                headers=self._line_headers(),
                timeout=30,
                stream=True,
            )
        except requests.RequestException as err:
            raise ChatConnectTransientError(_("LINE media download failed: %s") % err) from err
        if response.status_code == 202:
            raise ChatConnectTransientError(_("LINE media is still being prepared"))
        if response.status_code >= 400:
            raise ChatConnectPermanentError(_("LINE media download failed: %s") % response.text)
        content_length = int(response.headers.get("Content-Length") or 0)
        if content_length and content_length > max_bytes:
            raise ChatConnectPermanentError(_("LINE media exceeds the configured size limit"))
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > max_bytes:
                raise ChatConnectPermanentError(_("LINE media exceeds the configured size limit"))
            chunks.append(chunk)
        mimetype = (response.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0]
        extension = mimetypes.guess_extension(mimetype) or ""
        return {"content": b"".join(chunks), "mimetype": mimetype, "extension": extension}

    def _attachment_public_url(self, attachment, outbound_message):
        config = self._config()
        media = self.env["chat.connect.media"].create_for_attachment(
            attachment,
            outbound_message=outbound_message,
            ttl_hours=config.media_link_ttl_hours if config else 24,
        )
        base = (self.env["ir.config_parameter"].sudo().get_param("web.base.url") or "").rstrip("/")
        return f"{base}/chat_connect_center/media/{media.id}/{media.access_token}"

    def _line_message_objects(self, text, attachments, outbound_message):
        messages = []
        link_lines = []
        for attachment in (attachments or [])[:4]:
            url = self._attachment_public_url(attachment, outbound_message)
            if attachment.mimetype in ("image/jpeg", "image/png") and url.startswith("https://"):
                messages.append(
                    {"type": "image", "originalContentUrl": url, "previewImageUrl": url}
                )
            else:
                link_lines.append(f"{attachment.name or _('Attachment')}: {url}")
        combined_text = "\n".join(part for part in [text, *link_lines] if part)
        if combined_text:
            messages.insert(0, {"type": "text", "text": combined_text[:5000]})
        return messages[:5]

    def _line_send_message(
        self,
        conversation,
        text,
        attachments=None,
        reply_token=None,
        idempotency_key=None,
        outbound_message=None,
    ):
        self.ensure_one()
        headers = {**self._line_headers(), "Content-Type": "application/json"}
        reply_token = reply_token or conversation._get_valid_reply_token()
        messages = self._line_message_objects(text, attachments, outbound_message)
        if not messages:
            raise ChatConnectPermanentError(_("LINE outbound message is empty"))
        body = {"messages": messages}
        using_reply = bool(reply_token)
        if using_reply:
            endpoint = "https://api.line.me/v2/bot/message/reply"
            body["replyToken"] = reply_token
        else:
            endpoint = "https://api.line.me/v2/bot/message/push"
            body["to"] = conversation.external_conversation_id
            headers["X-Line-Retry-Key"] = idempotency_key or str(uuid.uuid4())
        try:
            response = requests.post(endpoint, headers=headers, json=body, timeout=20)
        except requests.Timeout as err:
            if using_reply:
                raise ChatConnectDeliveryUncertain(_("LINE reply timed out; delivery is uncertain")) from err
            raise ChatConnectTransientError(_("LINE push timed out and can be retried safely")) from err
        except requests.RequestException as err:
            if using_reply:
                raise ChatConnectDeliveryUncertain(_("LINE reply failed; delivery is uncertain")) from err
            raise ChatConnectTransientError(_("LINE push connection failed: %s") % err) from err
        if response.status_code == 409 and not using_reply:
            data = response.json() if response.content else {}
        elif response.status_code == 400 and using_reply:
            # A rejected reply token was not delivered. Fall back to an idempotent push.
            conversation._mark_reply_token_used(reply_token)
            return self._line_send_message(
                conversation,
                text,
                attachments=attachments,
                idempotency_key=idempotency_key,
                outbound_message=outbound_message,
            )
        elif response.status_code >= 500 or response.status_code == 429:
            raise ChatConnectTransientError(_("LINE temporarily rejected the message: %s") % response.text)
        elif response.status_code >= 400:
            raise ChatConnectPermanentError(_("LINE send failed: %s") % response.text)
        else:
            data = response.json() if response.content else {}
        if using_reply:
            conversation._mark_reply_token_used(reply_token)
        sent = data.get("sentMessages") or []
        external_id = str(sent[0].get("id")) if sent and sent[0].get("id") else ""
        return {
            "external_message_id": external_id,
            "provider_request_id": response.headers.get("x-line-request-id") or response.headers.get("x-line-accepted-request-id") or "",
            "reply_token_used": using_reply,
            "http_status": response.status_code,
        }

    # WeChat Official Account adapter
    def _wechat_verify_signature(self, signature, timestamp, nonce):
        self.ensure_one()
        token = (self.wechat_token or self.webhook_secret or "").strip()
        if not token or not signature:
            return False
        values = sorted([token, timestamp or "", nonce or ""])
        generated = hashlib.sha1("".join(values).encode("utf-8")).hexdigest()
        return hmac.compare_digest(generated, (signature or "").strip())

    def _wechat_verify_msg_signature(self, msg_signature, timestamp, nonce, encrypt_text):
        self.ensure_one()
        token = (self.wechat_token or self.webhook_secret or "").strip()
        if not token or not msg_signature or not encrypt_text:
            return False
        values = sorted([token, timestamp or "", nonce or "", encrypt_text])
        generated = hashlib.sha1("".join(values).encode("utf-8")).hexdigest()
        return hmac.compare_digest(generated, (msg_signature or "").strip())

    def _wechat_verify_callback_signature(
        self,
        signature=None,
        msg_signature=None,
        timestamp=None,
        nonce=None,
        encrypt_text=None,
    ):
        self.ensure_one()
        if self.wechat_safe_mode_enabled:
            return bool(
                encrypt_text
                and self._wechat_verify_msg_signature(
                    msg_signature, timestamp, nonce, encrypt_text
                )
            )
        return self._wechat_verify_signature(signature, timestamp, nonce)

    def _wechat_decrypt_message(self, encrypt_text):
        self.ensure_one()
        aes_key = (self.wechat_encoding_aes_key or "").strip()
        if len(aes_key) != 43:
            raise ChatConnectPermanentError(_("WeChat EncodingAESKey is invalid"))
        try:
            from Crypto.Cipher import AES
        except Exception as err:
            raise ChatConnectPermanentError(_("pycryptodome is required for WeChat safe mode")) from err
        try:
            key = base64.b64decode(aes_key + "=")
            cipher = AES.new(key, AES.MODE_CBC, key[:16])
            plain = cipher.decrypt(base64.b64decode(encrypt_text))
            pad_len = plain[-1]
            if pad_len < 1 or pad_len > 32 or plain[-pad_len:] != bytes([pad_len]) * pad_len:
                raise ValueError("invalid PKCS#7 padding")
            plain = plain[:-pad_len]
            if len(plain) < 20:
                raise ValueError("decrypted payload is too short")
            msg_len = struct.unpack("!I", plain[16:20])[0]
            if msg_len < 0 or 20 + msg_len > len(plain):
                raise ValueError("invalid decrypted message length")
            message = plain[20 : 20 + msg_len]
            from_appid = plain[20 + msg_len :].decode("utf-8")
        except Exception as err:
            raise ChatConnectPermanentError(_("WeChat encrypted message is invalid")) from err
        expected_appid = (self.external_app_id or "").strip()
        if expected_appid and from_appid != expected_appid:
            raise ChatConnectPermanentError(_("WeChat AppID mismatch in encrypted payload"))
        return message.decode("utf-8")

    def _wechat_get_access_token(self, force_refresh=False):
        self.ensure_one()
        now = fields.Datetime.now()
        if not force_refresh and self.external_access_token and self.wechat_access_token_expires_at:
            if self.wechat_access_token_expires_at > now + timedelta(seconds=60):
                return self.external_access_token
        self.env.cr.execute("SELECT pg_advisory_xact_lock(%s)", (920000000 + self.id,))
        self.invalidate_recordset(["external_access_token", "wechat_access_token_expires_at"])
        if not force_refresh and self.external_access_token and self.wechat_access_token_expires_at:
            if self.wechat_access_token_expires_at > fields.Datetime.now() + timedelta(seconds=60):
                return self.external_access_token
        app_id = (self.external_app_id or "").strip()
        app_secret = (self.external_app_secret or "").strip()
        if not app_id or not app_secret:
            raise ChatConnectPermanentError(_("WeChat app id/secret is not configured"))
        try:
            if self.wechat_use_stable_token:
                response = requests.post(
                    "https://api.weixin.qq.com/cgi-bin/stable_token",
                    json={
                        "grant_type": "client_credential",
                        "appid": app_id,
                        "secret": app_secret,
                        "force_refresh": bool(force_refresh),
                    },
                    timeout=20,
                )
            else:
                response = requests.get(
                    "https://api.weixin.qq.com/cgi-bin/token",
                    params={"grant_type": "client_credential", "appid": app_id, "secret": app_secret},
                    timeout=20,
                )
        except requests.RequestException as err:
            raise ChatConnectTransientError(_("WeChat access token request failed: %s") % err) from err
        data = response.json() if response.content else {}
        access_token = data.get("access_token")
        if not access_token:
            raise ChatConnectPermanentError(_("WeChat access token request failed: %s") % data)
        expires_in = int(data.get("expires_in") or 7200)
        self.sudo().write(
            {
                "external_access_token": access_token,
                "wechat_access_token_expires_at": fields.Datetime.now() + timedelta(seconds=max(expires_in - 300, 60)),
            }
        )
        return access_token

    def _wechat_json_request(self, method, endpoint, payload=None, params=None, retry_token=True):
        token = self._wechat_get_access_token()
        request_params = dict(params or {})
        request_params["access_token"] = token
        try:
            response = requests.request(
                method,
                endpoint,
                params=request_params,
                json=payload,
                timeout=20,
            )
        except requests.Timeout as err:
            raise ChatConnectDeliveryUncertain(_("WeChat request timed out; delivery is uncertain")) from err
        except requests.RequestException as err:
            raise ChatConnectTransientError(_("WeChat connection failed: %s") % err) from err
        data = response.json() if response.content else {}
        errcode = int(data.get("errcode") or 0)
        if errcode in (40014, 42001) and retry_token:
            self._wechat_get_access_token(force_refresh=True)
            return self._wechat_json_request(method, endpoint, payload=payload, params=params, retry_token=False)
        if response.status_code >= 500 or errcode in (-1, 45009):
            raise ChatConnectTransientError(_("WeChat temporarily rejected the request: %s") % data)
        if response.status_code >= 400 or errcode:
            raise ChatConnectPermanentError(_("WeChat API request failed: %s") % data)
        return data

    def _wechat_get_user_profile(self, openid):
        self.ensure_one()
        if not openid:
            return {}
        try:
            return self._wechat_json_request(
                "GET",
                "https://api.weixin.qq.com/cgi-bin/user/info",
                params={"openid": openid, "lang": "en"},
            )
        except Exception:
            _logger.info("Unable to fetch WeChat profile for account %s", self.id)
            return {}

    def _wechat_download_content(self, media_id, retry_token=True):
        self.ensure_one()
        config = self._config()
        max_bytes = (config.max_media_mb if config else 20) * 1024 * 1024
        token = self._wechat_get_access_token()
        try:
            response = requests.get(
                "https://api.weixin.qq.com/cgi-bin/media/get",
                params={"access_token": token, "media_id": media_id},
                timeout=30,
                stream=True,
            )
        except requests.RequestException as err:
            raise ChatConnectTransientError(_("WeChat media download failed: %s") % err) from err
        mimetype = (response.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0]
        if mimetype == "application/json" or response.status_code >= 400:
            try:
                data = response.json()
            except ValueError:
                data = {"status": response.status_code}
            if int(data.get("errcode") or 0) in (40014, 42001) and retry_token:
                self._wechat_get_access_token(force_refresh=True)
                return self._wechat_download_content(media_id, retry_token=False)
            raise ChatConnectPermanentError(_("WeChat media download failed: %s") % data)
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > max_bytes:
                raise ChatConnectPermanentError(_("WeChat media exceeds the configured size limit"))
            chunks.append(chunk)
        return {
            "content": b"".join(chunks),
            "mimetype": mimetype,
            "extension": mimetypes.guess_extension(mimetype) or "",
        }

    def _wechat_upload_image(self, attachment, retry_token=True):
        token = self._wechat_get_access_token()
        try:
            response = requests.post(
                "https://api.weixin.qq.com/cgi-bin/media/upload",
                params={"access_token": token, "type": "image"},
                files={
                    "media": (
                        attachment.name or "image.jpg",
                        base64.b64decode(attachment.datas or b""),
                        attachment.mimetype or "image/jpeg",
                    )
                },
                timeout=30,
            )
        except requests.Timeout as err:
            raise ChatConnectDeliveryUncertain(_("WeChat media upload timed out")) from err
        except requests.RequestException as err:
            raise ChatConnectTransientError(_("WeChat media upload failed: %s") % err) from err
        data = response.json() if response.content else {}
        if int(data.get("errcode") or 0) in (40014, 42001) and retry_token:
            self._wechat_get_access_token(force_refresh=True)
            return self._wechat_upload_image(attachment, retry_token=False)
        if data.get("errcode"):
            raise ChatConnectPermanentError(_("WeChat media upload failed: %s") % data)
        media_id = data.get("media_id")
        if not media_id:
            raise ChatConnectPermanentError(_("WeChat media upload returned no media id"))
        return media_id

    def _wechat_send_message(self, conversation, text, attachments=None, outbound_message=None):
        self.ensure_one()
        openid = conversation.external_visitor_id or conversation.external_conversation_id
        if not openid:
            raise ChatConnectPermanentError(_("WeChat recipient openid is empty"))
        conversation._check_wechat_outbound_window()
        endpoint = "https://api.weixin.qq.com/cgi-bin/message/custom/send"
        links = []
        results = []
        for attachment in (attachments or [])[:4]:
            if attachment.mimetype in ("image/jpeg", "image/png"):
                media_id = self._wechat_upload_image(attachment)
                results.append(
                    self._wechat_json_request(
                        "POST",
                        endpoint,
                        payload={"touser": openid, "msgtype": "image", "image": {"media_id": media_id}},
                    )
                )
            else:
                links.append(
                    f"{attachment.name or _('Attachment')}: "
                    f"{self._attachment_public_url(attachment, outbound_message)}"
                )
        combined_text = "\n".join(part for part in [text, *links] if part)
        if combined_text:
            results.append(
                self._wechat_json_request(
                    "POST",
                    endpoint,
                    payload={"touser": openid, "msgtype": "text", "text": {"content": combined_text[:2000]}},
                )
            )
        if not results:
            raise ChatConnectPermanentError(_("WeChat outbound message is empty"))
        conversation._consume_wechat_outbound_quota(len(results))
        result = results[-1]
        return {
            "external_message_id": str(result.get("msgid") or ""),
            "provider_request_id": "",
            "reply_token_used": False,
            "http_status": 200,
        }

    def action_test_connection(self):
        self.ensure_one()
        try:
            details = ""
            if self.platform == "line":
                response = requests.get(
                    "https://api.line.me/v2/bot/info",
                    headers=self._line_headers(),
                    timeout=20,
                )
                if response.status_code >= 400:
                    raise ChatConnectPermanentError(_("LINE connection test failed: %s") % response.text)
                data = response.json() if response.content else {}
                details = data.get("displayName") or data.get("userId") or "LINE"
            elif self.platform in ("wechat", "wechat_service"):
                token = self._wechat_get_access_token(force_refresh=True)
                details = f"WeChat token ...{token[-6:]}"
            else:
                if not self.webhook_secret:
                    raise ChatConnectPermanentError(_("A webhook secret is required for generic providers."))
                details = _("Generic webhook signature is configured.")
        except Exception as err:
            self.sudo().write(
                {
                    "connection_status": "error",
                    "last_error": str(err),
                    "last_error_at": fields.Datetime.now(),
                }
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Connection test failed"),
                    "message": str(err),
                    "type": "danger",
                    "sticky": True,
                },
            }
        self.sudo().write(
            {
                "connection_status": "connected",
                "last_error": False,
                "last_error_at": False,
            }
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Connection test passed"),
                "message": details,
                "type": "success",
                "sticky": False,
            },
        }

    def action_test_translation(self):
        self.ensure_one()
        translated = self._translate_text(
            "Connection test",
            source_lang="en",
            target_lang=self.target_lang or "en",
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Translation test"),
                "message": translated or _("Translation returned no text. Check the endpoint and diagnostic log."),
                "type": "success" if translated else "warning",
                "sticky": not bool(translated),
            },
        }
