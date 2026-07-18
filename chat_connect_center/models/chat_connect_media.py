import secrets
from datetime import timedelta

from odoo import api, fields, models


class ChatConnectMedia(models.Model):
    _name = "chat.connect.media"
    _description = "Chat Connect Temporary Media"
    _order = "id desc"
    _check_company_auto = True

    attachment_id = fields.Many2one(
        "ir.attachment",
        required=True,
        ondelete="cascade",
        index=True,
    )
    outbound_message_id = fields.Many2one(
        "chat.connect.message",
        ondelete="cascade",
        index=True,
    )
    access_token = fields.Char(
        required=True,
        copy=False,
        default=lambda self: secrets.token_urlsafe(32),
        index=True,
        groups="chat_connect_center.group_chat_connect_manager",
    )
    expires_at = fields.Datetime(required=True, index=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )

    _access_token_unique = models.Constraint(
        "UNIQUE(access_token)",
        "Temporary media access tokens must be unique.",
    )

    @api.model
    def create_for_attachment(self, attachment, outbound_message=None, ttl_hours=24):
        company = outbound_message.company_id if outbound_message else self.env.company
        return self.sudo().create(
            {
                "attachment_id": attachment.id,
                "outbound_message_id": outbound_message.id if outbound_message else False,
                "expires_at": fields.Datetime.now() + timedelta(hours=max(1, int(ttl_hours or 24))),
                "company_id": company.id,
            }
        )

    @api.model
    def _cron_cleanup_expired(self):
        expired = self.sudo().search([("expires_at", "<", fields.Datetime.now())], limit=1000)
        expired.unlink()
