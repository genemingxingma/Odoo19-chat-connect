from odoo import fields, models


class DiscussChannel(models.Model):
    _inherit = "discuss.channel"

    chat_connect_conversation_id = fields.Many2one(
        "chat.connect.conversation",
        string="External Chat Conversation",
        copy=False,
        index=True,
        ondelete="set null",
        groups="chat_connect_center.group_chat_connect_user",
    )
