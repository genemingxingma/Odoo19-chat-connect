from markupsafe import escape
from datetime import timedelta

from odoo import fields, models


class ChatConnectMessage(models.Model):
    _name = "chat.connect.message"
    _description = "Chat Connect Message"
    _order = "id desc"

    conversation_id = fields.Many2one("chat.connect.conversation", required=True, ondelete="cascade", index=True)
    account_id = fields.Many2one(related="conversation_id.account_id", store=True, index=True)

    direction = fields.Selection(
        [("inbound", "Inbound"), ("outbound", "Outbound")], required=True, default="inbound", index=True
    )
    external_message_id = fields.Char(index=True)
    message_type = fields.Selection(
        [("text", "Text"), ("image", "Image"), ("file", "File"), ("audio", "Audio"), ("video", "Video"), ("event", "Event")],
        default="text",
        index=True,
    )
    media_id = fields.Char()
    media_url = fields.Char()
    text = fields.Text()
    translated_text = fields.Text(readonly=True)
    payload_json = fields.Json()
    mail_message_id = fields.Many2one("mail.message", copy=False)
    state = fields.Selection(
        [("draft", "Draft"), ("received", "Received"), ("sent", "Sent"), ("failed", "Failed")],
        default="draft",
        index=True,
    )
    error_message = fields.Text(readonly=True)
    retry_count = fields.Integer(default=0)
    max_retries = fields.Integer(default=5)
    next_retry_at = fields.Datetime()
    last_attempt_at = fields.Datetime()

    def _diag_log(self, event, level="info", message="", payload=None, response_payload=None, http_status=200, exception=""):
        for record in self:
            record.env["chat.connect.diagnostic.log"].sudo().create(
                {
                    "level": level,
                    "event": event,
                    "message": message,
                    "platform": record.account_id.platform or "",
                    "webhook_uid": record.account_id.webhook_uid or "",
                    "account_id": record.account_id.id,
                    "conversation_id": record.conversation_id.id,
                    "chat_message_id": record.id,
                    "endpoint": "internal:outbound",
                    "http_method": "INTERNAL",
                    "http_status": http_status,
                    "request_payload": payload or {},
                    "response_payload": response_payload or {},
                    "exception": exception or "",
                }
            )

    def action_send_outbound(self):
        for record in self:
            if record.direction != "outbound":
                continue
            if not record.text:
                record.write({"state": "failed", "error_message": "Text is empty", "last_attempt_at": fields.Datetime.now()})
                record._diag_log(
                    event="outbound.invalid_payload",
                    level="warning",
                    message="Outbound text is empty.",
                    payload=record.payload_json or {},
                    http_status=400,
                )
                continue
            try:
                payload = record.payload_json or {}
                external_message_id = record.account_id._send_external_message(
                    record.conversation_id,
                    record.text,
                    reply_token=payload.get("reply_token"),
                )
                body = f"Outbound: {escape(record.text)}"
                message = record.conversation_id._post_to_discuss(body)
                record.write(
                    {
                        "state": "sent",
                        "external_message_id": external_message_id,
                        "mail_message_id": message.id,
                        "error_message": False,
                        "last_attempt_at": fields.Datetime.now(),
                        "next_retry_at": False,
                    }
                )
                record._diag_log(
                    event="outbound.sent",
                    level="info",
                    message="Outbound message sent successfully.",
                    payload=payload,
                    response_payload={"external_message_id": external_message_id},
                )
            except Exception as err:  # pragma: no cover - runtime integration error path
                retry_count = (record.retry_count or 0) + 1
                next_retry = fields.Datetime.now() + timedelta(minutes=min(30, retry_count * 2))
                record.write(
                    {
                        "state": "failed",
                        "error_message": str(err),
                        "retry_count": retry_count,
                        "last_attempt_at": fields.Datetime.now(),
                        "next_retry_at": next_retry if retry_count < (record.max_retries or 1) else False,
                    }
                )
                record._diag_log(
                    event="outbound.failed",
                    level="error",
                    message=f"Outbound send failed: {err}",
                    payload=record.payload_json or {},
                    response_payload={"retry_count": retry_count, "max_retries": record.max_retries},
                    http_status=500,
                    exception=str(err),
                )

    def action_reset_draft(self):
        self.write({"state": "draft", "error_message": False, "retry_count": 0, "next_retry_at": False})

    def _cron_retry_failed_outbound(self):
        now = fields.Datetime.now()
        messages = self.env["chat.connect.message"].sudo().search(
            [
                ("direction", "=", "outbound"),
                ("state", "=", "failed"),
                ("next_retry_at", "!=", False),
                ("next_retry_at", "<=", now),
            ],
            limit=100,
        )
        for message in messages:
            if (message.retry_count or 0) >= (message.max_retries or 1):
                continue
            message.action_send_outbound()
