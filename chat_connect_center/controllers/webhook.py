import hashlib
import hmac
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from odoo import fields, http
from odoo.http import content_disposition, request


class ChatConnectWebhookController(http.Controller):
    _SAFE_HEADERS = {"content-type", "content-length", "user-agent", "x-forwarded-for", "x-real-ip"}
    _SENSITIVE_KEYS = {
        "authorization",
        "cookie",
        "token",
        "secret",
        "signature",
        "content",
        "text",
        "body",
        "encrypt",
        "appsecret",
        "access_token",
    }

    def _collect_headers(self):
        return {
            key: str(value)[:500]
            for key, value in request.httprequest.headers.items()
            if key.lower() in self._SAFE_HEADERS
        }

    def _sanitize_for_log(self, value, key="", depth=0):
        if depth > 4:
            return "[truncated]"
        if key.lower() in self._SENSITIVE_KEYS:
            return "[redacted]"
        if isinstance(value, dict):
            return {
                str(item_key)[:100]: self._sanitize_for_log(item_value, str(item_key), depth + 1)
                for item_key, item_value in list(value.items())[:50]
            }
        if isinstance(value, list):
            return [self._sanitize_for_log(item, key, depth + 1) for item in value[:50]]
        if isinstance(value, str):
            return value[:1000]
        return value

    def _log_diag(
        self,
        event,
        level="info",
        account=None,
        platform="",
        webhook_uid="",
        message="",
        payload=None,
        response_payload=None,
        http_status=200,
        exception="",
        conversation=None,
        chat_message=None,
    ):
        try:
            # Logging is best-effort and must never abort webhook processing.
            with request.env.cr.savepoint():
                request.env["chat.connect.diagnostic.log"].sudo().create(
                    {
                        "level": level,
                        "event": event,
                        "message": message,
                        "platform": platform or (account.platform if account else ""),
                        "webhook_uid": webhook_uid or (account.webhook_uid if account else ""),
                        "account_id": account.id if account else False,
                        "conversation_id": conversation.id if conversation else False,
                        "chat_message_id": chat_message.id if chat_message else False,
                        "company_id": account.company_id.id if account else request.env.company.id,
                        "endpoint": request.httprequest.path,
                        "http_method": request.httprequest.method,
                        "http_status": http_status,
                        "remote_ip": request.httprequest.remote_addr,
                        "request_headers": self._collect_headers(),
                        "request_payload": self._sanitize_for_log(payload or {}),
                        "response_payload": self._sanitize_for_log(response_payload or {}),
                        "exception": str(exception or "")[:4000],
                    }
                )
        except Exception:
            return

    @staticmethod
    def _json(data, status=200):
        return request.make_json_response(data, status=status)

    @staticmethod
    def _text(data, status=200):
        return request.make_response(
            data,
            headers=[("Content-Type", "text/plain; charset=utf-8")],
            status=status,
        )

    @staticmethod
    def _parse_xml_body(raw_body):
        root = ET.fromstring(raw_body.decode("utf-8"))
        return {child.tag: child.text or "" for child in root}

    @staticmethod
    def _payload_event_uid(payload):
        explicit = (payload or {}).get("event_uid") or (payload or {}).get("message_id")
        if explicit:
            return str(explicit)
        canonical = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _event_datetime(timestamp_ms):
        if not timestamp_ms:
            return fields.Datetime.now()
        try:
            return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError, OSError):
            return fields.Datetime.now()

    def _resolve_account(self, platform, webhook_uid):
        return request.env["chat.connect.account"].sudo().search(
            [
                ("active", "=", True),
                ("platform", "=", platform),
                ("webhook_uid", "=", webhook_uid),
            ],
            limit=1,
        )

    def _raw_body(self, account):
        config = request.env["chat.connect.config"].get_active(account.company_id)
        max_bytes = (config.max_webhook_payload_kb if config else 1024) * 1024
        content_length = request.httprequest.content_length or 0
        if content_length and content_length > max_bytes:
            return None, self._json({"ok": False, "error": "payload_too_large"}, status=413)
        raw_body = request.httprequest.get_data(cache=True) or b""
        if len(raw_body) > max_bytes:
            return None, self._json({"ok": False, "error": "payload_too_large"}, status=413)
        return raw_body, None

    def _enqueue(self, account, event_uid, payload, event_timestamp=None):
        return request.env["chat.connect.inbound.event"].sudo().enqueue(
            account,
            event_uid,
            payload,
            event_timestamp=event_timestamp,
        )

    @http.route(
        "/chat_connect_center/webhook/<string:platform>/<string:webhook_uid>",
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
    )
    def receive_webhook(self, platform, webhook_uid, **kwargs):
        account = self._resolve_account(platform, webhook_uid)
        if not account:
            self._log_diag(
                "receive_webhook.account_not_found",
                "warning",
                platform=platform,
                webhook_uid=webhook_uid,
                message="Active account not found for webhook callback.",
                http_status=404,
            )
            return self._json({"ok": False, "error": "account_not_found"}, status=404)

        if platform in ("wechat", "wechat_service") and request.httprequest.method == "GET":
            args = request.httprequest.args
            timestamp = args.get("timestamp")
            nonce = args.get("nonce")
            echostr = args.get("echostr") or ""
            msg_signature = args.get("msg_signature")
            if account.wechat_safe_mode_enabled and msg_signature:
                valid = account._wechat_verify_msg_signature(msg_signature, timestamp, nonce, echostr)
                if valid:
                    try:
                        challenge = account._wechat_decrypt_message(echostr)
                    except Exception as err:
                        self._log_diag("wechat.verify.decrypt_failed", "error", account, exception=err, http_status=400)
                        return self._text("invalid encrypted challenge", status=400)
                    return self._text(challenge)
            elif account._wechat_verify_signature(args.get("signature"), timestamp, nonce):
                return self._text(echostr)
            self._log_diag("wechat.verify.invalid_signature", "warning", account, http_status=401)
            return self._text("invalid signature", status=401)

        raw_body, error_response = self._raw_body(account)
        if error_response is not None:
            self._log_diag("webhook.payload_too_large", "warning", account, http_status=413)
            return error_response

        if platform == "line":
            signature = request.httprequest.headers.get("X-Line-Signature")
            if not account._line_verify_signature(raw_body, signature):
                self._log_diag("line.verify.invalid_signature", "warning", account, http_status=401)
                return self._json({"ok": False, "error": "invalid_line_signature"}, status=401)
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._json({"ok": False, "error": "invalid_json"}, status=400)
            created_count = 0
            duplicate_count = 0
            for item in account._line_parse_events(payload):
                event, created = self._enqueue(
                    account,
                    item["event_uid"],
                    item,
                    event_timestamp=self._event_datetime(item.get("event_timestamp_ms")),
                )
                created_count += int(created)
                duplicate_count += int(not created)
                self._log_diag(
                    "line.webhook.queued" if created else "line.webhook.duplicate",
                    account=account,
                    payload={
                        "event_uid": item["event_uid"],
                        "message_type": item.get("message_type"),
                        "conversation_type": item.get("conversation_type"),
                        "is_redelivery": item.get("is_redelivery"),
                    },
                    response_payload={"event_id": event.id},
                )
            return self._json(
                {"ok": True, "queued": created_count, "duplicates": duplicate_count},
                status=200,
            )

        if platform in ("wechat", "wechat_service"):
            args = request.httprequest.args
            timestamp = args.get("timestamp")
            nonce = args.get("nonce")
            if not account._wechat_verify_signature(args.get("signature"), timestamp, nonce):
                self._log_diag("wechat.verify.invalid_signature", "warning", account, http_status=401)
                return self._text("invalid signature", status=401)
            try:
                data = self._parse_xml_body(raw_body)
            except (UnicodeDecodeError, ET.ParseError):
                self._log_diag("wechat.webhook.invalid_xml", "warning", account, http_status=400)
                return self._text("invalid xml", status=400)
            if account.wechat_safe_mode_enabled and data.get("Encrypt"):
                encrypt_text = data["Encrypt"]
                if not account._wechat_verify_msg_signature(
                    args.get("msg_signature"), timestamp, nonce, encrypt_text
                ):
                    self._log_diag("wechat.verify.invalid_msg_signature", "warning", account, http_status=401)
                    return self._text("invalid msg signature", status=401)
                try:
                    data = self._parse_xml_body(account._wechat_decrypt_message(encrypt_text).encode("utf-8"))
                except Exception as err:
                    self._log_diag("wechat.webhook.decrypt_failed", "error", account, exception=err, http_status=400)
                    return self._text("invalid encrypted message", status=400)
            msg_type = data.get("MsgType") or "event"
            message_type = {
                "text": "text",
                "image": "image",
                "voice": "audio",
                "video": "video",
                "shortvideo": "video",
            }.get(msg_type, "event")
            text = (data.get("Content") or "") if msg_type == "text" else ""
            if msg_type == "voice" and data.get("Recognition"):
                text = data["Recognition"]
            elif msg_type == "location":
                text = f"{data.get('Label') or 'Location'}: {data.get('Location_X', '')},{data.get('Location_Y', '')}"
            elif msg_type == "link":
                text = f"{data.get('Title') or 'Link'}: {data.get('Url') or ''}"
            elif msg_type == "event":
                text = f"[WECHAT {data.get('Event') or 'event'}]"
            elif not text and message_type != "text":
                text = f"[WECHAT {msg_type}]"
            message_id = data.get("MsgId") or ""
            event_seed = "|".join(
                [
                    data.get("FromUserName") or "",
                    data.get("CreateTime") or "",
                    msg_type,
                    data.get("Event") or "",
                    data.get("EventKey") or "",
                    message_id,
                ]
            )
            event_uid = message_id or hashlib.sha256(event_seed.encode("utf-8")).hexdigest()
            payload = {
                "event_uid": event_uid,
                "conversation_id": data.get("FromUserName") or "",
                "conversation_type": "user",
                "sender_id": data.get("FromUserName") or "",
                "sender_name": "",
                "message_id": message_id or event_uid,
                "message_type": message_type,
                "provider_message_type": msg_type,
                "media_id": data.get("MediaId") or "",
                "text": text,
                "platform": "wechat",
                "raw_xml": data,
            }
            event, created = self._enqueue(account, event_uid, payload)
            self._log_diag(
                "wechat.webhook.queued" if created else "wechat.webhook.duplicate",
                account=account,
                payload={"event_uid": event_uid, "message_type": message_type},
                response_payload={"event_id": event.id},
            )
            return self._text("success")

        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json({"ok": False, "error": "invalid_json"}, status=400)
        given_token = request.httprequest.headers.get("X-Chat-Token") or ""
        expected_token = account.webhook_secret or ""
        if not expected_token or not hmac.compare_digest(given_token, expected_token):
            self._log_diag("generic.verify.invalid_token", "warning", account, http_status=401)
            return self._json({"ok": False, "error": "invalid_token"}, status=401)
        event_uid = self._payload_event_uid(payload)
        event, created = self._enqueue(account, event_uid, {**payload, "event_uid": event_uid})
        return self._json(
            {"ok": True, "event_id": event.id, "queued": bool(created), "duplicate": not created},
            status=200,
        )

    @http.route(
        "/chat_connect_center/webhook/<string:platform>/<string:webhook_uid>/send",
        type="http",
        auth="user",
        methods=["POST"],
    )
    def send_message(self, platform, webhook_uid, **kwargs):
        user = request.env.user
        if not user.has_group("chat_connect_center.group_chat_connect_user"):
            return self._json({"ok": False, "error": "forbidden"}, status=403)
        account = self._resolve_account(platform, webhook_uid)
        if not account or account.company_id not in user.company_ids:
            return self._json({"ok": False, "error": "account_not_found"}, status=404)
        is_manager = user.has_group("chat_connect_center.group_chat_connect_manager")
        if not is_manager and user not in account.operator_user_ids:
            return self._json({"ok": False, "error": "forbidden"}, status=403)
        try:
            payload = request.httprequest.get_json(silent=True) or {}
        except Exception:
            payload = {}
        conversation_ref = payload.get("conversation_id")
        if not conversation_ref:
            return self._json({"ok": False, "error": "conversation_id_required"}, status=400)
        conversation = request.env["chat.connect.conversation"].sudo().search(
            [
                ("account_id", "=", account.id),
                ("external_conversation_id", "=", str(conversation_ref)),
            ],
            limit=1,
        )
        if not conversation:
            return self._json({"ok": False, "error": "conversation_not_found"}, status=404)
        text = payload.get("text") or ""
        if not text:
            return self._json({"ok": False, "error": "text_required"}, status=400)
        message = request.env["chat.connect.message"].sudo().create(
            {
                "conversation_id": conversation.id,
                "direction": "outbound",
                "text": text,
                "payload_json": {"source": "authenticated_send_endpoint"},
            }
        )
        self._log_diag(
            "send_message.queued",
            account=account,
            conversation=conversation,
            chat_message=message,
            response_payload={"message_id": message.id},
            http_status=202,
        )
        return self._json({"ok": True, "message_id": message.id, "state": message.state}, status=202)

    @http.route(
        "/chat_connect_center/media/<int:media_id>/<string:access_token>",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def download_media(self, media_id, access_token, **kwargs):
        media = request.env["chat.connect.media"].sudo().search(
            [("id", "=", media_id), ("access_token", "=", access_token)],
            limit=1,
        )
        if not media or media.expires_at <= fields.Datetime.now() or not media.attachment_id:
            return request.not_found()
        attachment = media.attachment_id.sudo()
        headers = [
            ("Content-Type", attachment.mimetype or "application/octet-stream"),
            ("Content-Disposition", content_disposition(attachment.name or "attachment")),
            ("Cache-Control", "private, max-age=300"),
            ("X-Content-Type-Options", "nosniff"),
        ]
        return request.make_response(attachment.raw or b"", headers=headers)
