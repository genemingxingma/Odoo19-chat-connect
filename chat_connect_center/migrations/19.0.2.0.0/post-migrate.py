import uuid

from odoo import Command


def migrate(cr, version):
    from odoo.api import Environment, SUPERUSER_ID

    env = Environment(cr, SUPERUSER_ID, {})
    conversations = env["chat.connect.conversation"].sudo().search([])
    channel_counts = {}
    for conversation in conversations.filtered("mail_channel_id"):
        channel_counts[conversation.mail_channel_id.id] = channel_counts.get(conversation.mail_channel_id.id, 0) + 1

    for conversation in conversations.filtered("mail_channel_id"):
        channel = conversation.mail_channel_id
        unsafe = (
            channel == conversation.account_id.default_channel_id
            or channel_counts.get(channel.id, 0) > 1
        )
        if unsafe:
            conversation.write({"mail_channel_id": False, "livechat_guest_id": False})
            continue
        if not channel.chat_connect_conversation_id:
            channel.chat_connect_conversation_id = conversation.id

    outbound_without_keys = env["chat.connect.message"].sudo().search(
        [("direction", "=", "outbound"), ("idempotency_key", "=", False)]
    )
    for message in outbound_without_keys:
        message.idempotency_key = str(uuid.uuid4())

    operator_group = env.ref("chat_connect_center.group_chat_connect_user")
    operator_users = env["chat.connect.account"].sudo().search([]).operator_user_ids
    if operator_users:
        operator_users.write({"group_ids": [Command.link(operator_group.id)]})
