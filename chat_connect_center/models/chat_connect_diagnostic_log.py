from datetime import timedelta

from odoo import api, fields, models


class ChatConnectDiagnosticLog(models.Model):
    _name = "chat.connect.diagnostic.log"
    _description = "Chat Connect Diagnostic Log"
    _order = "id desc"
    _check_company_auto = True

    level = fields.Selection(
        [("info", "Info"), ("warning", "Warning"), ("error", "Error")],
        default="info",
        required=True,
        index=True,
    )
    event = fields.Char(required=True, index=True)
    message = fields.Text()
    platform = fields.Char(index=True)
    webhook_uid = fields.Char(index=True, groups="chat_connect_center.group_chat_connect_manager")
    account_id = fields.Many2one("chat.connect.account", ondelete="set null", index=True, check_company=True)
    conversation_id = fields.Many2one("chat.connect.conversation", ondelete="set null", index=True, check_company=True)
    chat_message_id = fields.Many2one("chat.connect.message", ondelete="set null", index=True, check_company=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    endpoint = fields.Char()
    http_method = fields.Char()
    http_status = fields.Integer()
    remote_ip = fields.Char()
    request_headers = fields.Json(groups="chat_connect_center.group_chat_connect_manager")
    request_payload = fields.Json(groups="chat_connect_center.group_chat_connect_manager")
    response_payload = fields.Json(groups="chat_connect_center.group_chat_connect_manager")
    exception = fields.Text(groups="chat_connect_center.group_chat_connect_manager")

    @api.model
    def _cron_cleanup_logs(self):
        companies = self.env["res.company"].sudo().search([])
        for company in companies:
            config = self.env["chat.connect.config"].get_active(company)
            retention_days = config.log_retention_days if config else 30
            cutoff = fields.Datetime.now() - timedelta(days=max(1, retention_days))
            self.sudo().search(
                [("company_id", "=", company.id), ("create_date", "<", cutoff)],
                limit=5000,
            ).unlink()
