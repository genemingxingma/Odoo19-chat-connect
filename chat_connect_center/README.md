# Chat Connect Center (Odoo 19)

Unified integration center that bridges third-party chat channels into Odoo Discuss.

## Supported channel types

- WeChat (`wechat`)
- LINE (`line`)
- WhatsApp (`whatsapp`)
- WeCom / Enterprise WeChat (`wecom`)
- WeChat Service Account (`wechat_service`)

## Core capabilities

- Multi-platform account registry and webhook endpoints
- Conversation mapping (`external_conversation_id` -> `mail.channel`)
- Inbound message archival
- Outbound message dispatch flow
- AI translation hook during inbound processing
- Full chat message audit trail in Odoo models

## Official integration status

- LINE Messaging API:
  - Webhook signature validation (`X-Line-Signature`, HMAC-SHA256)
  - Inbound event parsing (`events[]`)
  - Outbound send via official API (`/v2/bot/message/push`)
- WeChat Official Account:
  - Callback signature verification (`signature/timestamp/nonce`)
  - GET challenge response (`echostr`)
  - POST XML inbound parsing
  - Outbound custom message send (`/cgi-bin/message/custom/send`)
  - Access token fetch/cache (`/cgi-bin/token`)

## Installation

1. Put this module under Odoo addons path.
2. Update app list.
3. Install **Chat Connect Center**.

## Quick setup

1. Create a Chat Account with platform.
2. Set `Webhook Secret` (optional but recommended).
3. Configure AI translation endpoint if needed.
4. Use generated webhook url format:
   - `/chat_connect_center/webhook/<platform>/<webhook_uid>`

## Generic webhook URL

`/chat_connect_center/webhook/<platform>/<webhook_uid>`

## Generic webhook example

```bash
curl -X POST 'http://127.0.0.1:8069/chat_connect_center/webhook/wechat/<webhook_uid>' \
  -H 'Content-Type: application/json' \
  -H 'X-Chat-Token: <webhook_secret>' \
  -d '{
    "conversation_id": "wx_user_1001",
    "sender_id": "wx_user_1001",
    "sender_name": "Alice",
    "message_id": "msg_001",
    "text": "你好，我想了解产品报价"
  }'
```

## LINE setup notes

1. Set platform to `line`.
2. Fill:
   - `LINE Channel Secret`
   - `LINE Channel Access Token`
3. Set webhook URL in LINE Console:
   - `https://<your-domain>/chat_connect_center/webhook/line/<webhook_uid>`

## WeChat Official Account setup notes

1. Set platform to `wechat` or `wechat_service`.
2. Fill:
   - `External App ID` (AppID)
   - `External App Secret` (AppSecret)
   - `WeChat Verify Token` (same as token configured in WeChat backend)
3. Set callback URL in WeChat backend:
   - `https://<your-domain>/chat_connect_center/webhook/wechat/<webhook_uid>`
4. WeChat will call GET verification on this URL and module will return `echostr` when signature passes.


Configure this module on remote Odoo and call webhook using external platform callbacks.

## Notes

- LINE currently sends outbound text using official push API.
- WeChat currently sends outbound text using official customer service API.
- Media/image/file official APIs can be added in next iteration.
