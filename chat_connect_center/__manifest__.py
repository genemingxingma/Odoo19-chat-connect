{
    "name": "Chat Connect Center",
    "summary": "Unified chat bridge for WeChat/LINE/WhatsApp to Odoo Discuss",
    "version": "19.0.1.0.0",
    "category": "Discuss",
    "author": "mamingxing",
    "maintainer": "mamingxing",
    "company": "iMyTest",
    "images": ["static/description/icon.png"],
    "license": "LGPL-3",
    "depends": ["base", "mail", "im_livechat"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/chat_connect_account_views.xml",
        "views/chat_connect_config_views.xml",
        "views/chat_connect_conversation_views.xml",
        "views/chat_connect_message_views.xml",
        "views/chat_connect_diagnostic_log_views.xml",
        "views/chat_connect_menus.xml"
    ],
    "installable": True,
    "application": True,
    "external_dependencies": {
        "python": ["requests", "pycryptodome"],
    },
}
