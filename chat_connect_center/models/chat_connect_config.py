from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ChatConnectConfig(models.Model):
    _name = "chat.connect.config"
    _description = "Chat Connect Configuration"
    _order = "id desc"
    _check_company_auto = True

    name = fields.Char(required=True, default="Default Configuration")
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        domain=lambda self: [("id", "in", self.env.companies.ids)],
        index=True,
    )

    default_source_lang = fields.Char(default="auto")
    default_target_lang = fields.Char(default="en")
    default_translation_endpoint = fields.Char()
    default_translation_api_key = fields.Char(groups="chat_connect_center.group_chat_connect_manager")
    default_translation_model = fields.Char(default="gpt-4o-mini")

    auto_create_conversation = fields.Boolean(
        default=True,
        help="Automatically create conversation records when inbound webhook receives unknown conversation IDs.",
    )
    webhook_enforce_token = fields.Boolean(
        default=True,
        readonly=True,
        help="Generic webhook endpoints always require an account secret. Official providers use their signatures.",
    )
    max_webhook_payload_kb = fields.Integer(default=1024, required=True)
    max_media_mb = fields.Integer(default=20, required=True)
    media_link_ttl_hours = fields.Integer(default=24, required=True)
    log_retention_days = fields.Integer(default=30, required=True)
    note = fields.Text()

    @api.constrains("active", "company_id")
    def _check_single_active_config(self):
        for record in self.filtered("active"):
            duplicate = self.search_count(
                [
                    ("id", "!=", record.id),
                    ("company_id", "=", record.company_id.id),
                    ("active", "=", True),
                ]
            )
            if duplicate:
                raise ValidationError(_("Only one active Chat Connect configuration is allowed per company."))

    @api.constrains("max_webhook_payload_kb", "max_media_mb", "media_link_ttl_hours", "log_retention_days")
    def _check_positive_limits(self):
        for record in self:
            if min(
                record.max_webhook_payload_kb,
                record.max_media_mb,
                record.media_link_ttl_hours,
                record.log_retention_days,
            ) <= 0:
                raise ValidationError(_("Chat Connect limits and retention values must be greater than zero."))

    @api.model
    def get_active(self, company=None):
        company = company or self.env.company
        return self.sudo().search(
            [("active", "=", True), ("company_id", "=", company.id)],
            limit=1,
        )
