from odoo import fields, models


class ChatConnectDiagnosticLog(models.Model):
    _name = "chat.connect.diagnostic.log"
    _description = "Chat Connect Diagnostic Log"
    _order = "id desc"

    level = fields.Selection(
        [("info", "Info"), ("warning", "Warning"), ("error", "Error")],
        default="info",
        required=True,
        index=True,
    )
    event = fields.Char(required=True, index=True)
    message = fields.Text()
    platform = fields.Char(index=True)
    webhook_uid = fields.Char(index=True)
    account_id = fields.Many2one("chat.connect.account", ondelete="set null", index=True)
    conversation_id = fields.Many2one("chat.connect.conversation", ondelete="set null", index=True)
    chat_message_id = fields.Many2one("chat.connect.message", ondelete="set null", index=True)
    endpoint = fields.Char()
    http_method = fields.Char()
    http_status = fields.Integer()
    remote_ip = fields.Char()
    request_headers = fields.Json()
    request_payload = fields.Json()
    response_payload = fields.Json()
    exception = fields.Text()
