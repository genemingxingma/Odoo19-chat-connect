import base64
import logging
from datetime import datetime, timedelta, timezone

from psycopg2 import IntegrityError

from odoo import _, api, fields, models, Command
from odoo.exceptions import ValidationError
from odoo.tools import plaintext2html

from .provider_errors import ChatConnectPermanentError


_logger = logging.getLogger(__name__)


class ChatConnectConversation(models.Model):
    _name = "chat.connect.conversation"
    _description = "Chat Connect Conversation"
    _order = "last_inbound_at desc, id desc"
    _check_company_auto = True

    name = fields.Char(compute="_compute_name", store=True)
    account_id = fields.Many2one(
        "chat.connect.account",
        required=True,
        ondelete="restrict",
        index=True,
        check_company=True,
    )
    company_id = fields.Many2one(related="account_id.company_id", store=True, index=True)
    platform = fields.Selection(related="account_id.platform", store=True, index=True)
    external_conversation_id = fields.Char(required=True, index=True)
    conversation_type = fields.Selection(
        [("user", "Direct"), ("group", "Group"), ("room", "Room")],
        default="user",
        required=True,
        index=True,
    )
    external_visitor_id = fields.Char(index=True)
    external_visitor_name = fields.Char()
    customer_lang = fields.Char(string="Customer Language")
    state = fields.Selection([("open", "Open"), ("closed", "Closed")], default="open", index=True)

    mail_channel_id = fields.Many2one("discuss.channel", string="Discuss Channel", copy=False, ondelete="set null", index=True)
    livechat_guest_id = fields.Many2one("mail.guest", string="Livechat Guest", copy=False, ondelete="set null")
    message_ids = fields.One2many("chat.connect.message", "conversation_id", string="Messages")
    last_inbound_at = fields.Datetime(index=True)
    last_outbound_at = fields.Datetime(index=True)
    last_reply_token = fields.Char(copy=False, groups="chat_connect_center.group_chat_connect_manager")
    reply_token_expires_at = fields.Datetime(copy=False, groups="chat_connect_center.group_chat_connect_manager")
    reply_token_used = fields.Boolean(default=False, copy=False, groups="chat_connect_center.group_chat_connect_manager")
    wechat_outbound_window_expires_at = fields.Datetime(readonly=True)
    wechat_outbound_quota_remaining = fields.Integer(readonly=True)

    _conversation_unique = models.Constraint(
        "UNIQUE(account_id, external_conversation_id)",
        "Conversation must be unique per account.",
    )

    @api.depends("external_visitor_name", "external_conversation_id", "account_id.name")
    def _compute_name(self):
        for record in self:
            visitor = record.external_visitor_name or record.external_conversation_id
            record.name = f"[{record.account_id.name}] {visitor}"

    @api.constrains("mail_channel_id")
    def _check_unique_discuss_channel(self):
        for record in self.filtered("mail_channel_id"):
            if record.mail_channel_id == record.account_id.default_channel_id:
                raise ValidationError(_("A triage channel cannot be used as a customer conversation."))
            if self.search_count(
                [("id", "!=", record.id), ("mail_channel_id", "=", record.mail_channel_id.id)]
            ):
                raise ValidationError(_("A Discuss channel can only belong to one external conversation."))

    @api.model
    def get_or_create_for_payload(self, account, payload):
        config = self.env["chat.connect.config"].get_active(account.company_id)
        conversation_ref = (
            payload.get("conversation_id")
            or payload.get("chat_id")
            or payload.get("session_id")
            or payload.get("sender_id")
        )
        if not conversation_ref:
            return self.browse(), "conversation_id_required"
        conversation_ref = str(conversation_ref)
        domain = [("account_id", "=", account.id), ("external_conversation_id", "=", conversation_ref)]
        conversation = self.sudo().search(domain, limit=1)
        if not conversation:
            if config and not config.auto_create_conversation:
                return self.browse(), "conversation_not_found"
            try:
                with self.env.cr.savepoint():
                    conversation = self.sudo().create(
                        {
                            "account_id": account.id,
                            "external_conversation_id": conversation_ref,
                            "conversation_type": payload.get("conversation_type") or "user",
                            "external_visitor_id": str(payload.get("sender_id") or ""),
                            "external_visitor_name": payload.get("sender_name") or payload.get("visitor_name") or "",
                            "customer_lang": payload.get("customer_lang") or "",
                        }
                    )
            except IntegrityError:
                conversation = self.sudo().search(domain, limit=1)
                if not conversation:
                    raise
        updates = {}
        if conversation.state == "closed":
            updates["state"] = "open"
        if payload.get("sender_id") and not conversation.external_visitor_id:
            updates["external_visitor_id"] = str(payload["sender_id"])
        if payload.get("sender_name") and not conversation.external_visitor_name:
            updates["external_visitor_name"] = payload["sender_name"]
        if payload.get("customer_lang") and not conversation.customer_lang:
            updates["customer_lang"] = payload["customer_lang"]
        if updates:
            conversation.sudo().write(updates)
        return conversation, None

    def _profile_payload(self, payload):
        self.ensure_one()
        sender_id = payload.get("sender_id") or self.external_visitor_id
        if self.account_id.platform == "line":
            profile = self.account_id._line_get_profile(
                sender_id,
                conversation_type=payload.get("conversation_type") or self.conversation_type,
                conversation_id=payload.get("conversation_id") or self.external_conversation_id,
            )
            return {
                "sender_name": profile.get("displayName") or payload.get("sender_name") or "",
                "customer_lang": profile.get("language") or payload.get("customer_lang") or "",
            }
        if self.account_id.platform in ("wechat", "wechat_service"):
            profile = self.account_id._wechat_get_user_profile(sender_id)
            return {
                "sender_name": profile.get("nickname") or payload.get("sender_name") or "",
                "customer_lang": profile.get("language") or payload.get("customer_lang") or "",
            }
        return {
            "sender_name": payload.get("sender_name") or "",
            "customer_lang": payload.get("customer_lang") or "",
        }

    def _update_inbound_context(self, payload):
        self.ensure_one()
        profile = self._profile_payload(payload)
        vals = {"last_inbound_at": fields.Datetime.now(), "state": "open"}
        if profile.get("sender_name") and not self.external_visitor_name:
            vals["external_visitor_name"] = profile["sender_name"]
        if profile.get("customer_lang"):
            vals["customer_lang"] = profile["customer_lang"]
        reply_token = payload.get("reply_token")
        if reply_token:
            vals.update(
                {
                    "last_reply_token": reply_token,
                    "reply_token_expires_at": fields.Datetime.now() + timedelta(seconds=55),
                    "reply_token_used": False,
                }
            )
        if self.account_id.platform in ("wechat", "wechat_service"):
            raw = payload.get("raw_xml") or {}
            if (raw.get("MsgType") or "") != "event":
                vals.update(
                    {
                        "wechat_outbound_window_expires_at": fields.Datetime.now() + timedelta(hours=48),
                        "wechat_outbound_quota_remaining": 5,
                    }
                )
            elif (raw.get("Event") or "").lower() in ("subscribe", "scan", "click"):
                vals.update(
                    {
                        "wechat_outbound_window_expires_at": fields.Datetime.now() + timedelta(minutes=1),
                        "wechat_outbound_quota_remaining": 3,
                    }
                )
        self.sudo().write(vals)
        self.account_id.sudo().write(
            {"last_inbound_at": fields.Datetime.now(), "last_webhook_at": fields.Datetime.now()}
        )
        return profile

    def _get_valid_reply_token(self):
        self.ensure_one()
        if (
            self.last_reply_token
            and not self.reply_token_used
            and self.reply_token_expires_at
            and self.reply_token_expires_at > fields.Datetime.now()
        ):
            return self.last_reply_token
        return ""

    def _mark_reply_token_used(self, token):
        self.ensure_one()
        if token and token == self.last_reply_token:
            self.sudo().write({"reply_token_used": True})

    def _check_wechat_outbound_window(self):
        self.ensure_one()
        if not self.wechat_outbound_window_expires_at:
            raise ChatConnectPermanentError(_("No active WeChat customer-service reply window is available."))
        if self.wechat_outbound_window_expires_at <= fields.Datetime.now():
            raise ChatConnectPermanentError(_("The WeChat customer-service reply window has expired."))
        if self.wechat_outbound_quota_remaining == 0 and self.wechat_outbound_window_expires_at:
            raise ChatConnectPermanentError(_("The WeChat customer-service message quota is exhausted."))

    def _consume_wechat_outbound_quota(self, count=1):
        self.ensure_one()
        if self.wechat_outbound_window_expires_at and self.wechat_outbound_quota_remaining > 0:
            self.sudo().write(
                {"wechat_outbound_quota_remaining": max(0, self.wechat_outbound_quota_remaining - count)}
            )

    def _detach_unsafe_channel(self):
        self.ensure_one()
        channel = self.mail_channel_id
        if not channel:
            return
        is_triage = channel == self.account_id.default_channel_id
        other_count = self.sudo().search_count(
            [("id", "!=", self.id), ("mail_channel_id", "=", channel.id)]
        )
        linked_conversation = channel.sudo().chat_connect_conversation_id
        if is_triage or other_count or (linked_conversation and linked_conversation != self):
            self.sudo().write({"mail_channel_id": False, "livechat_guest_id": False})
        elif not linked_conversation:
            channel.sudo().chat_connect_conversation_id = self.id

    def _ensure_discuss_channel(self):
        self.ensure_one()
        self._detach_unsafe_channel()
        if self.mail_channel_id and self.mail_channel_id.sudo().chat_connect_conversation_id == self:
            return self.mail_channel_id
        if self.account_id.livechat_channel_id:
            try:
                channel, guest = self.account_id._create_livechat_discuss_channel(self)
                if channel:
                    self.sudo().write({"mail_channel_id": channel.id, "livechat_guest_id": guest.id})
                    self._initialize_livechat_session(channel)
                    self._notify_triage_channel(channel)
                    return channel
            except Exception as err:
                _logger.warning("Livechat session create failed for conversation %s: %s", self.id, err)
                self._diag_log("livechat.create.failed", "error", str(err))
        channel_name = f"{self.account_id.platform.upper()} | {self.external_visitor_name or self.external_conversation_id}"
        channel = self.env["discuss.channel"].sudo().create(
            {
                "name": channel_name,
                "channel_type": "channel",
                "chat_connect_conversation_id": self.id,
            }
        )
        partner_ids = self.account_id.operator_user_ids.partner_id.ids
        if partner_ids:
            channel.add_members(partner_ids=partner_ids, post_joined_message=False)
        self.sudo().write({"mail_channel_id": channel.id})
        self._notify_triage_channel(channel)
        return channel

    def _notify_triage_channel(self, customer_channel):
        self.ensure_one()
        triage = self.account_id.default_channel_id
        if not triage or triage == customer_channel:
            return
        triage.with_context(chat_connect_skip_outbound_sync=True).message_post(
            body=plaintext2html(_("New external conversation: %s") % self.name),
            message_type="notification",
            subtype_xmlid="mail.mt_note",
        )

    def _diag_log(self, event, level="info", message=""):
        try:
            with self.env.cr.savepoint():
                self.env["chat.connect.diagnostic.log"].sudo().create(
                    {
                        "level": level,
                        "event": event,
                        "message": message,
                        "platform": self.account_id.platform,
                        "webhook_uid": self.account_id.webhook_uid,
                        "account_id": self.account_id.id,
                        "conversation_id": self.id,
                        "company_id": self.company_id.id,
                        "endpoint": "internal:conversation",
                        "http_method": "INTERNAL",
                    }
                )
        except Exception:
            _logger.exception("Could not create diagnostic log for conversation %s", self.id)

    def _initialize_livechat_session(self, channel):
        if channel and channel.channel_type == "livechat" and channel.livechat_operator_id:
            channel._broadcast([channel.livechat_operator_id.id])

    def _trigger_livechat_ai_agent(self, channel, inbound_mail_message):
        self.ensure_one()
        if not channel or channel.channel_type != "livechat" or "ai.agent" not in self.env:
            return False
        ai_agent = getattr(channel.sudo(), "ai_agent_id", False)
        if not ai_agent or not inbound_mail_message:
            return False
        ai_agent.sudo()._generate_response_for_channel(inbound_mail_message, channel.sudo())
        return True

    def _trigger_livechat_chatbot(self, channel, visitor_text):
        self.ensure_one()
        if not channel or channel.channel_type != "livechat" or not channel.chatbot_current_step_id:
            return False
        if channel.livechat_agent_history_ids:
            return False
        current_step = channel.sudo().chatbot_current_step_id
        chatbot = current_step.chatbot_script_id
        if current_step.is_forward_operator and channel.livechat_operator_id != chatbot.operator_partner_id:
            return False
        next_step = current_step._process_answer(channel, visitor_text or "")
        if not next_step:
            channel.sudo().livechat_end_dt = fields.Datetime.now()
            return True
        channel.sudo().chatbot_current_step_id = next_step.id
        next_step._process_step(channel)
        return True

    def _trigger_livechat_automation(self, channel, inbound_mail_message, visitor_text):
        if self._trigger_livechat_ai_agent(channel, inbound_mail_message):
            return
        self._trigger_livechat_chatbot(channel, visitor_text)

    def _post_to_discuss(self, message_body, attachment_ids=None, **kwargs):
        self.ensure_one()
        channel = self._ensure_discuss_channel()
        return channel.with_context(chat_connect_skip_outbound_sync=True).message_post(
            body=plaintext2html(message_body or ""),
            attachment_ids=attachment_ids or [],
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            **kwargs,
        )

    def _download_inbound_media(self, payload, channel):
        self.ensure_one()
        media_id = payload.get("media_id") or ""
        if not media_id:
            return self.env["ir.attachment"]
        if self.account_id.platform == "line":
            media = self.account_id._line_download_content(media_id)
        elif self.account_id.platform in ("wechat", "wechat_service"):
            media = self.account_id._wechat_download_content(media_id)
        else:
            return self.env["ir.attachment"]
        filename = payload.get("file_name") or f"{payload.get('message_id') or media_id}{media.get('extension') or ''}"
        return self.env["ir.attachment"].sudo().create(
            {
                "name": filename,
                "datas": base64.b64encode(media["content"]),
                "mimetype": media.get("mimetype") or "application/octet-stream",
                "res_model": "discuss.channel",
                "res_id": channel.id,
            }
        )

    def ingest_inbound(self, payload):
        self.ensure_one()
        payload = dict(payload or {})
        event_uid = str((payload or {}).get("event_uid") or (payload or {}).get("message_id") or "")
        if event_uid:
            existing = self.env["chat.connect.message"].sudo().search(
                [
                    ("account_id", "=", self.account_id.id),
                    ("direction", "=", "inbound"),
                    ("external_event_id", "=", event_uid),
                ],
                limit=1,
            )
            if existing:
                return existing
        profile = self._update_inbound_context(payload)
        if profile.get("sender_name"):
            payload["sender_name"] = profile["sender_name"]
        message_type = payload.get("message_type") or "text"
        text = payload.get("text") or ""
        translated_text = self.account_id._translate_text(
            text,
            source_lang=self.customer_lang or self.account_id.source_lang,
            target_lang=self.account_id.target_lang,
        )
        channel = self._ensure_discuss_channel()
        attachment = self._download_inbound_media(payload, channel)
        rendered_text = text
        if self.conversation_type in ("group", "room") and profile.get("sender_name"):
            rendered_text = f"{profile['sender_name']}: {rendered_text}".rstrip()
        if not rendered_text and not attachment:
            rendered_text = f"[{message_type}]"
        if translated_text and translated_text != text:
            rendered_text = f"{rendered_text}\n\n[{_('Translation')}] {translated_text}".strip()
        post_kwargs = {}
        if channel.channel_type == "livechat" and self.livechat_guest_id:
            post_kwargs["author_guest_id"] = self.livechat_guest_id.id
        mail_message = self._post_to_discuss(
            rendered_text,
            attachment_ids=attachment.ids,
            **post_kwargs,
        )
        bridge_message = self.env["chat.connect.message"].sudo().create(
            {
                "conversation_id": self.id,
                "direction": "inbound",
                "state": "received",
                "external_event_id": event_uid,
                "external_message_id": payload.get("message_id") or "",
                "message_type": message_type,
                "media_id": payload.get("media_id") or "",
                "media_url": payload.get("media_url") or "",
                "text": text,
                "translated_text": translated_text,
                "payload_json": payload,
                "mail_message_id": mail_message.id,
                "attachment_ids": [Command.set(attachment.ids)],
            }
        )
        if channel.channel_type == "livechat":
            self._trigger_livechat_automation(channel, mail_message, text)
        return bridge_message
