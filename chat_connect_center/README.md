# Chat Connect Center for Odoo 19

将 LINE Messaging API、微信公众号消息接入 Odoo Discuss/Live Chat 的独立对接中心。

## 当前能力

### LINE Messaging API

- 官方 `X-Line-Signature` HMAC-SHA256 验签。
- 按 `webhookEventId` 幂等接收入站事件。
- 支持单聊、群组和聊天室，群消息保留实际发言人。
- 支持文本、图片、文件、音频和视频入站归档。
- 使用 Reply API 或 Push API 回复。
- Push 请求使用 `X-Line-Retry-Key`，避免网络重试造成重复发送。
- 支持文本、图片及带时效签名的文件下载链接。

### 微信公众号

- 支持明文模式和安全模式的签名验证、消息解密及 GET 服务器验证。
- 支持普通 access token 和 stable access token 缓存、过期刷新。
- 支持文本及媒体入站归档。
- 使用官方客服消息接口发送文本和图片。
- 强制检查客服回复窗口和剩余消息额度。

### Odoo 集成

- 每个外部会话对应一个独立的 `discuss.channel`，禁止多个客户共用出站频道。
- 可选择 Odoo Live Chat 频道，并动态使用该频道配置的 AI Agent 或 Chatbot 规则。
- AI 回答自动按客户语言添加“AI 自动回复”标识。
- 入站和出站使用持久队列；Webhook 快速返回，网络发送失败可重试。
- 保存原文、译文、实际发送文本、附件、提供商消息 ID、请求 ID 和诊断日志。
- 支持 OpenAI-compatible `/v1/chat/completions`、`/v1/responses` 或自定义翻译端点。
- 中英文和泰文界面翻译。

## 能力边界

- `line` 和 `wechat` / `wechat_service` 是官方 API 适配器。
- `whatsapp` 和 `wecom` 当前只支持带 `X-Chat-Token` 的通用入站 Webhook。
- WhatsApp Cloud API 和企业微信官方出站接口尚未实现；配置页会明确显示此限制。

## 配置

安装后打开：

`Chat Connect -> Configuration -> Accounts`

### LINE

1. 新建平台为 `LINE` 的账号。
2. 填写 `LINE Channel Secret` 和 `LINE Channel Access Token`。
3. 在账号页面直接复制 `Webhook URL`。
4. 将 URL 填入 LINE Developers 的 Messaging API Webhook URL，启用 `Use webhook`。
5. 点击模块中的 `Test Connection`，再在 LINE Developers 中执行 Verify。

### 微信公众号

1. 新建平台为 `WeChat` 或 `WeChat Service Account` 的账号。
2. 填写 AppID、AppSecret 和 `WeChat Verify Token`。
3. 如公众号启用安全模式，填写 43 字符的 `EncodingAESKey` 并启用安全模式。
4. 将账号页面显示的 `Webhook URL` 填入微信公众号服务器配置。
5. 微信后台的 Token 必须与 Odoo 中的 `WeChat Verify Token` 完全一致。

### Odoo Live Chat / AI

1. 在 Odoo Live Chat 频道规则中配置 AI Agent 或 Chatbot。
2. 在 Chat Connect 账号的 `Odoo Livechat Channel` 选择该频道。
3. 不需要在 Chat Connect 代码中写死机器人；每次创建外部会话时会读取 Live Chat 规则。
4. `Triage Notification Channel` 仅用于新会话通知，不用于向客户发送消息。

## Webhook 和队列

Webhook 格式：

```text
/chat_connect_center/webhook/<platform>/<webhook_uid>
```

入站请求先写入 `Inbound Queue`，后台任务再创建会话、下载媒体、翻译并触发 AI/Chatbot。操作员在对应 Discuss 频道回复后，消息进入出站队列并由后台任务发送。

## 安全设计

- 提供商凭据、原始载荷和诊断详情仅 Chat Connect Manager 可见。
- 操作员只能读取分配给自己的账号、会话和消息。
- 通用 Webhook 必须配置共享密钥，不能匿名关闭验签。
- 媒体链接使用随机令牌并自动过期。
- 日志对 Authorization、Cookie、token、正文等敏感字段进行脱敏。
- 数据按 Odoo 公司隔离。

## 自动化测试

模块包含 Odoo `post_install` 回归测试，覆盖：

- LINE 签名、群聊解析、官方成员资料 URL 和幂等 Push。
- Webhook 入站事件去重。
- 一个会话一个 Discuss 频道及跨客户误发防护。
- 分诊频道和非成员消息不能触发外部发送。
- 凭据字段权限。
- 微信客服回复窗口。
- AI 与 Chatbot 互斥触发。
- 诊断日志脱敏。

测试命令示例：

```bash
odoo-bin -c /path/to/odoo.conf -d database \
  -u chat_connect_center --workers=0 --no-http \
  --test-enable --test-tags /chat_connect_center --stop-after-init
```

## 版本

当前模块版本：`19.0.2.1.2`

开发者：`mamingxing`
公司：`iMyTest`
