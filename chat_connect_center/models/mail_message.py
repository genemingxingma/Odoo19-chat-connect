from odoo import api, models
from odoo.tools.mail import html2plaintext


class MailMessage(models.Model):
    _inherit = "mail.message"

    @staticmethod
    def _chat_connect_all_ai_prefixes():
        return (
            "[AI Auto-Reply]",
            "[AI自动回复]",
            "[ข้อความตอบกลับอัตโนมัติจาก AI]",
            "[AI自動返信]",
            "[AI 자동 응답]",
            "[Réponse automatique IA]",
            "[Automatische KI-Antwort]",
            "[Respuesta automática de IA]",
            "[Resposta automática da IA]",
            "[Risposta automatica AI]",
        )

    def _chat_connect_ai_author_partners(self, conversation):
        channel = conversation.mail_channel_id
        ai_agent = getattr(channel.sudo(), "ai_agent_id", False) if channel else False
        partners = self.env["res.partner"]
        if not ai_agent:
            return partners
        if getattr(ai_agent, "partner_id", False):
            partners |= ai_agent.partner_id
        if getattr(ai_agent, "user_id", False) and getattr(ai_agent.user_id, "partner_id", False):
            partners |= ai_agent.user_id.partner_id
        return partners

    def _chat_connect_ai_prefix(self, conversation):
        lang_code = (conversation.customer_lang or "").lower()
        if not lang_code:
            lang_code = (conversation.account_id.source_lang or "").lower()
        if lang_code.startswith("zh"):
            return "[AI自动回复]"
        if lang_code.startswith("th"):
            return "[ข้อความตอบกลับอัตโนมัติจาก AI]"
        if lang_code.startswith("ja"):
            return "[AI自動返信]"
        if lang_code.startswith("ko"):
            return "[AI 자동 응답]"
        if lang_code.startswith("fr"):
            return "[Réponse automatique IA]"
        if lang_code.startswith("de"):
            return "[Automatische KI-Antwort]"
        if lang_code.startswith("es"):
            return "[Respuesta automática de IA]"
        if lang_code.startswith("pt"):
            return "[Resposta automática da IA]"
        if lang_code.startswith("it"):
            return "[Risposta automatica AI]"
        return "[AI Auto-Reply]"

    def _chat_connect_has_ai_prefix(self, text):
        return any(text.startswith(prefix) for prefix in self._chat_connect_all_ai_prefixes())

    def _chat_connect_is_ai_generated(self, conversation, message):
        author = message.author_id
        if not author:
            return False
        partners = self._chat_connect_ai_author_partners(conversation)
        return bool(partners and author in partners)

    def _chat_connect_is_chatbot_generated(self, channel, message):
        if not message.author_id or channel.channel_type != "livechat":
            return False
        bot_partners = channel.sudo().channel_member_ids.filtered(
            lambda member: member.livechat_member_type == "bot"
        ).partner_id
        return message.author_id in bot_partners

    def _chat_connect_author_can_send(self, conversation, message):
        channel = conversation.mail_channel_id.sudo()
        if not message.author_id or getattr(message, "author_guest_id", False):
            return False
        if self._chat_connect_is_ai_generated(conversation, message):
            return True
        if self._chat_connect_is_chatbot_generated(channel, message):
            return True
        internal_users = message.author_id.user_ids.filtered(lambda user: user._is_internal())
        if not internal_users:
            return False
        return message.author_id in channel.channel_member_ids.partner_id

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        if self.env.context.get("chat_connect_skip_outbound_sync"):
            return messages
        outbound_model = self.env["chat.connect.message"].sudo()
        for message in messages:
            if message.model != "discuss.channel" or message.message_type != "comment" or not message.res_id:
                continue
            channel = self.env["discuss.channel"].sudo().browse(message.res_id).exists()
            conversation = channel.chat_connect_conversation_id if channel else False
            if not conversation or conversation.mail_channel_id != channel:
                continue
            account = conversation.account_id
            if channel == account.default_channel_id or not account.outbound_enabled:
                continue
            if not self._chat_connect_author_can_send(conversation, message):
                continue
            if outbound_model.search_count([("mail_message_id", "=", message.id)]):
                continue
            text = (html2plaintext(message.body or "") or "").strip()
            attachments = message.attachment_ids
            if not text and not attachments:
                continue
            ai_generated = self._chat_connect_is_ai_generated(conversation, message)
            if ai_generated and not self._chat_connect_has_ai_prefix(text):
                text = f"{self._chat_connect_ai_prefix(conversation)} {text}".strip()
            message_type = "text"
            if attachments and not text:
                message_type = "image" if all(
                    attachment.mimetype in ("image/jpeg", "image/png") for attachment in attachments
                ) else "file"
            outbound_model.create(
                {
                    "conversation_id": conversation.id,
                    "direction": "outbound",
                    "message_type": message_type,
                    "text": text,
                    "mail_message_id": message.id,
                    "attachment_ids": [(6, 0, attachments.ids)],
                    "ai_generated": ai_generated,
                    "payload_json": {
                        "source": "discuss_reply",
                        "mail_message_id": message.id,
                        "ai_generated": ai_generated,
                    },
                }
            )
        return messages
