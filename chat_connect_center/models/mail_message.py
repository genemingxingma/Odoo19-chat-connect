from odoo import api, models
from odoo.tools.mail import html2plaintext


class MailMessage(models.Model):
    _inherit = "mail.message"

    def _chat_connect_all_ai_prefixes(self):
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
        ai_agent = getattr(channel, "ai_agent_id", False) if channel else False
        partners = self.env["res.partner"]
        if not ai_agent:
            return partners
        if getattr(ai_agent, "partner_id", False):
            partners |= ai_agent.partner_id
        if getattr(ai_agent, "user_id", False) and getattr(ai_agent.user_id, "partner_id", False):
            partners |= ai_agent.user_id.partner_id
        return partners

    def _chat_connect_ai_prefix(self, conversation):
        channel = conversation.mail_channel_id
        lang_code = ""
        if channel and getattr(channel, "livechat_lang_id", False):
            lang_code = (channel.livechat_lang_id.code or "").lower()
        if not lang_code:
            lang_code = (conversation.account_id.target_lang or "").lower()
        if not lang_code:
            lang_code = (self.env.lang or "en_US").lower()

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
        """Only mark AI when the author can be positively identified."""
        channel = conversation.mail_channel_id
        if not channel or channel.channel_type != "livechat":
            return False
        if not getattr(channel, "ai_agent_id", False):
            return False
        author = message.author_id
        if not author:
            return False
        ai_partners = self._chat_connect_ai_author_partners(conversation)
        return bool(ai_partners and author in ai_partners)

    @api.model_create_multi
    def create(self, vals_list):
        messages = super().create(vals_list)
        if self.env.context.get("chat_connect_skip_outbound_sync"):
            return messages

        conversation_model = self.env["chat.connect.conversation"].sudo()
        outbound_model = self.env["chat.connect.message"].sudo()

        for message in messages:
            if message.model != "discuss.channel" or message.message_type != "comment" or not message.res_id:
                continue
            conversation = conversation_model.search([("mail_channel_id", "=", message.res_id)], limit=1)
            if not conversation or not conversation.account_id.outbound_enabled:
                continue

            text = (html2plaintext(message.body or "") or "").strip()
            if not text:
                continue
            ai_generated = self._chat_connect_is_ai_generated(conversation, message)
            if ai_generated and not self._chat_connect_has_ai_prefix(text):
                text = f"{self._chat_connect_ai_prefix(conversation)} {text}"

            outbound = outbound_model.create(
                {
                    "conversation_id": conversation.id,
                    "direction": "outbound",
                    "text": text,
                    "payload_json": {
                        "source": "discuss_reply",
                        "mail_message_id": message.id,
                        "ai_generated": ai_generated,
                    },
                }
            )
            outbound.action_send_outbound()
        return messages
