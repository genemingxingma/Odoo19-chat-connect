from markupsafe import escape

from odoo import api, fields, models


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
        if self.mail_channel_id:
            return self.mail_channel_id

        channel_name = f"{self.account_id.platform.upper()} | {self.external_visitor_name or self.external_conversation_id}"
        channel = self.env["discuss.channel"].sudo().create(
            {
                "name": channel_name,
                "channel_type": "channel",
            }
        )
        self.mail_channel_id = channel
        return channel

    def _post_to_discuss(self, message_body):
        self.ensure_one()
        channel = self._ensure_discuss_channel()
        return channel.message_post(
            body=message_body,
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
        )

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
        body = f"<p><b>Inbound</b>: {escape(rendered_text)}</p>"
        if media_url:
            body += f"<p><b>Media URL</b>: {escape(media_url)}</p>"
        if translated_text:
            body += f"<p><b>Translated</b>: {escape(translated_text)}</p>"

        mail_message = self._post_to_discuss(body)
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
