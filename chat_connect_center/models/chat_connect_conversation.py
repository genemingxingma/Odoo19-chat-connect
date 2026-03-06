from markupsafe import escape
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ChatConnectConversation(models.Model):
    _name = "chat.connect.conversation"
    _description = "Chat Connect Conversation"
    _order = "id desc"

    name = fields.Char(compute="_compute_name", store=True)
    account_id = fields.Many2one("chat.connect.account", required=True, ondelete="cascade", index=True)
    external_conversation_id = fields.Char(required=True, index=True)
    external_visitor_id = fields.Char(index=True)
    external_visitor_name = fields.Char()
    state = fields.Selection([("open", "Open"), ("closed", "Closed")], default="open")

    mail_channel_id = fields.Many2one("discuss.channel", string="Discuss Channel", copy=False)
    livechat_guest_id = fields.Many2one("mail.guest", string="Livechat Guest", copy=False)
    message_ids = fields.One2many("chat.connect.message", "conversation_id", string="Messages")

    _sql_constraints = [
        (
            "chat_connect_conversation_uniq",
            "unique(account_id, external_conversation_id)",
            "Conversation must be unique per account.",
        ),
    ]

    @api.depends("external_visitor_name", "external_conversation_id", "account_id.name")
    def _compute_name(self):
        for record in self:
            visitor = record.external_visitor_name or record.external_conversation_id
            record.name = f"[{record.account_id.name}] {visitor}"

    def _ensure_discuss_channel(self):
        self.ensure_one()
        if self.account_id.livechat_channel_id and (
            not self.mail_channel_id or self.mail_channel_id.channel_type != "livechat"
        ):
            try:
                channel, guest = self.account_id._create_livechat_discuss_channel(
                    visitor_name=self.external_visitor_name or "",
                    visitor_external_id=self.external_conversation_id or "",
                )
                if channel:
                    self.mail_channel_id = channel
                    self.livechat_guest_id = guest
                    self._initialize_livechat_session(channel)
                    return channel
            except Exception as err:
                _logger.warning(
                    "Livechat session create failed for conversation %s account %s: %s",
                    self.id,
                    self.account_id.id,
                    err,
                )
                self.env["chat.connect.diagnostic.log"].sudo().create(
                    {
                        "level": "error",
                        "event": "livechat.create.failed",
                        "message": "Failed to create native livechat session, fallback to default channel.",
                        "platform": self.account_id.platform or "",
                        "webhook_uid": self.account_id.webhook_uid or "",
                        "account_id": self.account_id.id,
                        "conversation_id": self.id,
                        "endpoint": "internal:conversation",
                        "http_method": "INTERNAL",
                        "http_status": 500,
                        "request_payload": {
                            "external_conversation_id": self.external_conversation_id,
                            "external_visitor_id": self.external_visitor_id,
                        },
                        "exception": str(err),
                    }
                )
                # Fallback to default/static discuss channel if livechat session
                # cannot be created (e.g. no available operator/chatbot).
                pass

        if (
            self.account_id.default_channel_id
            and (not self.mail_channel_id or self.mail_channel_id.channel_type != "livechat")
            and self.mail_channel_id != self.account_id.default_channel_id
        ):
            self.mail_channel_id = self.account_id.default_channel_id
            return self.mail_channel_id
        if self.mail_channel_id:
            return self.mail_channel_id

        channel_name = f"{self.account_id.platform.upper()} | {self.external_visitor_name or self.external_conversation_id}"
        partner_ids = self.account_id.operator_user_ids.partner_id.ids
        channel = self.env["discuss.channel"].sudo().create(
            {
                "name": channel_name,
                "channel_type": "channel",
            }
        )
        if partner_ids:
            channel.sudo().add_members(partner_ids=partner_ids, post_joined_message=False)
        self.mail_channel_id = channel
        return channel

    def _initialize_livechat_session(self, channel):
        self.ensure_one()
        if not channel or channel.channel_type != "livechat":
            return
        if channel.livechat_operator_id:
            channel._broadcast([channel.livechat_operator_id.id])

    def _ensure_livechat_ai_agent(self, channel):
        self.ensure_one()
        if not channel or channel.channel_type != "livechat":
            return
        if "ai.agent" not in self.env:
            return
        if getattr(channel, "ai_agent_id", False):
            return
        rule = self.env["im_livechat.channel.rule"].sudo()
        if channel.livechat_channel_id:
            rule = rule.match_rule(channel_id=channel.livechat_channel_id.id, url="", country_id=False) or channel.livechat_channel_id.rule_ids[:1]
        ai_agent = rule.ai_agent_id if rule and getattr(rule, "ai_agent_id", False) else self.env["ai.agent"]
        if ai_agent:
            channel.sudo().write({"ai_agent_id": ai_agent.id})

    def _trigger_livechat_ai_agent(self, channel, inbound_mail_message):
        self.ensure_one()
        if not channel or channel.channel_type != "livechat":
            return
        if "ai.agent" not in self.env:
            return
        ai_agent = getattr(channel.sudo(), "ai_agent_id", False)
        if not ai_agent or not inbound_mail_message:
            return
        ai_agent.sudo()._generate_response_for_channel(inbound_mail_message, channel.sudo())

    def _post_to_discuss(self, message_body, **kwargs):
        self.ensure_one()
        channel = self._ensure_discuss_channel()
        return channel.with_context(chat_connect_skip_outbound_sync=True).message_post(
            body=message_body,
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            **kwargs,
        )

    def _trigger_livechat_chatbot(self, channel, visitor_text):
        self.ensure_one()
        if not channel or channel.channel_type != "livechat" or not channel.chatbot_current_step_id:
            return
        # If a human agent has already taken over, chatbot should not process further.
        if channel.livechat_agent_history_ids:
            return
        current_step = channel.sudo().chatbot_current_step_id
        chatbot = current_step.chatbot_script_id
        if current_step.is_forward_operator and channel.livechat_operator_id != chatbot.operator_partner_id:
            return
        next_step = current_step._process_answer(channel, visitor_text or "")
        if not next_step:
            channel.sudo().livechat_end_dt = fields.Datetime.now()
            return
        channel.sudo().chatbot_current_step_id = next_step.id
        next_step._process_step(channel)

    def ingest_inbound(self, payload):
        self.ensure_one()
        message_type = (payload or {}).get("message_type") or "text"
        media_id = (payload or {}).get("media_id") or ""
        media_url = (payload or {}).get("media_url") or ""
        text = (payload or {}).get("text") or ""
        translated_text = self.account_id._translate_text(text)

        rendered_text = text or "(empty)"
        if message_type != "text":
            rendered_text = f"[{message_type}] {rendered_text}".strip()
        body_parts = [f"Inbound: {escape(rendered_text)}"]
        if media_url:
            body_parts.append(f"Media URL: {escape(media_url)}")
        if translated_text:
            body_parts.append(f"Translated: {escape(translated_text)}")
        body = "\n".join(body_parts)

        post_kwargs = {}
        channel = self._ensure_discuss_channel()
        if channel.channel_type == "livechat":
            self._ensure_livechat_ai_agent(channel)
        if channel.channel_type == "livechat" and self.livechat_guest_id:
            post_kwargs["author_guest_id"] = self.livechat_guest_id.id
        mail_message = self._post_to_discuss(body, **post_kwargs)
        if channel.channel_type == "livechat":
            self._trigger_livechat_ai_agent(channel, mail_message)
            self._trigger_livechat_chatbot(channel, text)
        return self.env["chat.connect.message"].sudo().create(
            {
                "conversation_id": self.id,
                "direction": "inbound",
                "message_type": message_type,
                "media_id": media_id,
                "media_url": media_url,
                "external_message_id": payload.get("message_id"),
                "text": text,
                "translated_text": translated_text,
                "payload_json": payload,
                "mail_message_id": mail_message.id,
                "state": "received",
            }
        )
