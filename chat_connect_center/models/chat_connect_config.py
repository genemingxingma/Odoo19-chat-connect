from odoo import fields, models


class ChatConnectConfig(models.Model):
    _name = "chat.connect.config"
    _description = "Chat Connect Configuration"
    _order = "id desc"

    name = fields.Char(required=True, default="Default Configuration")
    active = fields.Boolean(default=True)

    default_source_lang = fields.Char(default="auto")
    default_target_lang = fields.Char(default="en")
    default_translation_endpoint = fields.Char()
    default_translation_api_key = fields.Char()
    default_translation_model = fields.Char(default="gpt-4o-mini")

    auto_create_conversation = fields.Boolean(
        default=True, help="Automatically create conversation records when inbound webhook receives unknown conversation IDs."
    )
    webhook_enforce_token = fields.Boolean(
        default=True, help="If enabled, account webhook secrets are enforced for inbound requests."
    )
    note = fields.Text()
