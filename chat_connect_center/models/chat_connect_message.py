import logging
import uuid
from datetime import timedelta

from odoo import _, api, fields, models

from .provider_errors import (
    ChatConnectDeliveryUncertain,
    ChatConnectPermanentError,
    ChatConnectTransientError,
)


_logger = logging.getLogger(__name__)


class ChatConnectMessage(models.Model):
    _name = "chat.connect.message"
    _description = "Chat Connect Message"
    _order = "id desc"
    _check_company_auto = True

    conversation_id = fields.Many2one(
        "chat.connect.conversation",
        required=True,
        ondelete="restrict",
        index=True,
        check_company=True,
    )
    account_id = fields.Many2one(related="conversation_id.account_id", store=True, index=True)
    company_id = fields.Many2one(related="conversation_id.company_id", store=True, index=True)
    direction = fields.Selection(
        [("inbound", "Inbound"), ("outbound", "Outbound")],
        required=True,
        default="inbound",
        index=True,
    )
    external_event_id = fields.Char(index=True)
    external_message_id = fields.Char(index=True)
    provider_request_id = fields.Char(index=True, readonly=True)
    idempotency_key = fields.Char(
        copy=False,
        index=True,
        groups="chat_connect_center.group_chat_connect_manager",
    )
    message_type = fields.Selection(
        [
            ("text", "Text"),
            ("image", "Image"),
            ("file", "File"),
            ("audio", "Audio"),
            ("video", "Video"),
            ("event", "Event"),
        ],
        default="text",
        index=True,
    )
    media_id = fields.Char()
    media_url = fields.Char()
    text = fields.Text()
    translated_text = fields.Text(readonly=True)
    delivered_text = fields.Text(readonly=True)
    payload_json = fields.Json(groups="chat_connect_center.group_chat_connect_manager")
    mail_message_id = fields.Many2one("mail.message", copy=False, ondelete="set null", index=True)
    attachment_ids = fields.Many2many(
        "ir.attachment",
        "chat_connect_message_attachment_rel",
        "message_id",
        "attachment_id",
        string="Attachments",
    )
    ai_generated = fields.Boolean(readonly=True, index=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("queued", "Queued"),
            ("sending", "Sending"),
            ("received", "Received"),
            ("sent", "Sent"),
            ("failed", "Failed"),
            ("permanent_failed", "Permanent Failure"),
            ("uncertain", "Delivery Uncertain"),
        ],
        default="draft",
        required=True,
        index=True,
    )
    error_message = fields.Text(readonly=True)
    retry_count = fields.Integer(default=0)
    max_retries = fields.Integer(default=5)
    next_retry_at = fields.Datetime(index=True)
    last_attempt_at = fields.Datetime()
    sending_started_at = fields.Datetime(index=True)
    sent_at = fields.Datetime(index=True)
    provider_http_status = fields.Integer(readonly=True)

    _inbound_event_unique = models.Constraint(
        "UNIQUE(account_id, direction, external_event_id)",
        "The provider event has already been archived for this account.",
    )
    _idempotency_key_unique = models.Constraint(
        "UNIQUE(idempotency_key)",
        "Outbound idempotency keys must be unique.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        outbound_created = False
        for vals in vals_list:
            values = dict(vals)
            if values.get("direction") == "outbound":
                if "state" not in values:
                    values["state"] = "queued"
                if values.get("state") in ("queued", "failed"):
                    outbound_created = True
                if not values.get("idempotency_key"):
                    values["idempotency_key"] = str(uuid.uuid4())
            prepared.append(values)
        records = super().create(prepared)
        if outbound_created:
            records.filtered(lambda record: record.direction == "outbound")._trigger_outbound_processing()
        return records

    def _trigger_outbound_processing(self):
        cron = self.env.ref("chat_connect_center.ir_cron_chat_connect_retry_failed_outbound", raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    def _diag_log(self, event, level="info", message="", response_payload=None, http_status=200, exception=""):
        for record in self:
            try:
                with record.env.cr.savepoint():
                    record.env["chat.connect.diagnostic.log"].sudo().create(
                        {
                            "level": level,
                            "event": event,
                            "message": message,
                            "platform": record.account_id.platform or "",
                            "webhook_uid": record.account_id.webhook_uid or "",
                            "account_id": record.account_id.id,
                            "conversation_id": record.conversation_id.id,
                            "chat_message_id": record.id,
                            "company_id": record.company_id.id,
                            "endpoint": "internal:outbound",
                            "http_method": "INTERNAL",
                            "http_status": http_status,
                            "request_payload": {
                                "direction": record.direction,
                                "message_type": record.message_type,
                                "attachment_count": len(record.attachment_ids),
                                "idempotency_key_suffix": (record.idempotency_key or "")[-8:],
                            },
                            "response_payload": response_payload or {},
                            "exception": exception or "",
                        }
                    )
            except Exception:
                _logger.exception("Could not create diagnostic log for outbound message %s", record.id)

    def _delivery_text(self):
        self.ensure_one()
        text = self.text or ""
        account = self.account_id
        conversation = self.conversation_id
        if (
            account.translation_enabled
            and account.outbound_translation_enabled
            and conversation.customer_lang
            and text
        ):
            translated = account._translate_text(
                text,
                source_lang=account.target_lang,
                target_lang=conversation.customer_lang,
            )
            if translated:
                self.sudo().write({"translated_text": translated})
                return translated
        return text

    def _schedule_retry(self, error):
        self.ensure_one()
        retry_count = self.retry_count + 1
        max_retries = max(1, self.max_retries or 1)
        delay_minutes = min(30, 2 ** min(retry_count, 5)) + (self.id % 3)
        self.sudo().write(
            {
                "state": "permanent_failed" if retry_count >= max_retries else "failed",
                "error_message": str(error),
                "retry_count": retry_count,
                "last_attempt_at": fields.Datetime.now(),
                "sending_started_at": False,
                "next_retry_at": (
                    fields.Datetime.now() + timedelta(minutes=delay_minutes)
                    if retry_count < max_retries
                    else False
                ),
            }
        )

    def _mark_account_error(self, error):
        for record in self:
            record.account_id.sudo().write(
                {"last_error": str(error), "last_error_at": fields.Datetime.now()}
            )

    def action_send_outbound(self):
        for record in self:
            if record.direction != "outbound" or record.state in ("sent", "received"):
                continue
            self.env.cr.execute("SELECT pg_try_advisory_xact_lock(%s)", (930000000 + record.id,))
            if not self.env.cr.fetchone()[0]:
                continue
            record.invalidate_recordset()
            if record.state == "sending" and record.sending_started_at:
                if record.sending_started_at > fields.Datetime.now() - timedelta(minutes=15):
                    continue
            if not record.text and not record.attachment_ids:
                error = _("Outbound message has no text or attachments.")
                record.sudo().write(
                    {
                        "state": "permanent_failed",
                        "error_message": error,
                        "last_attempt_at": fields.Datetime.now(),
                        "next_retry_at": False,
                    }
                )
                record._diag_log("outbound.invalid_payload", "warning", error, http_status=400)
                continue
            record.sudo().write(
                {
                    "state": "sending",
                    "sending_started_at": fields.Datetime.now(),
                    "last_attempt_at": fields.Datetime.now(),
                    "error_message": False,
                }
            )
            delivery_text = record._delivery_text()
            try:
                result = record.account_id._send_external_message(
                    record.conversation_id,
                    delivery_text,
                    attachments=record.attachment_ids,
                    reply_token=(record.payload_json or {}).get("reply_token"),
                    idempotency_key=record.idempotency_key,
                    outbound_message=record,
                )
            except ChatConnectDeliveryUncertain as err:
                record.sudo().write(
                    {
                        "state": "uncertain",
                        "error_message": str(err),
                        "sending_started_at": False,
                        "next_retry_at": False,
                    }
                )
                record._mark_account_error(err)
                record._diag_log("outbound.uncertain", "error", str(err), http_status=503, exception=str(err))
            except ChatConnectPermanentError as err:
                record.sudo().write(
                    {
                        "state": "permanent_failed",
                        "error_message": str(err),
                        "sending_started_at": False,
                        "next_retry_at": False,
                    }
                )
                record._mark_account_error(err)
                record._diag_log("outbound.permanent_failed", "error", str(err), http_status=400, exception=str(err))
            except ChatConnectTransientError as err:
                record._schedule_retry(err)
                record._mark_account_error(err)
                record._diag_log("outbound.failed", "error", str(err), http_status=503, exception=str(err))
            except Exception as err:  # pragma: no cover - provider/library runtime failure
                _logger.exception("Unexpected outbound failure for message %s", record.id)
                record._schedule_retry(err)
                record._mark_account_error(err)
                record._diag_log("outbound.failed", "error", str(err), http_status=500, exception=str(err))
            else:
                now = fields.Datetime.now()
                record.sudo().write(
                    {
                        "state": "sent",
                        "external_message_id": result.get("external_message_id") or "",
                        "provider_request_id": result.get("provider_request_id") or "",
                        "provider_http_status": result.get("http_status") or 200,
                        "delivered_text": delivery_text,
                        "error_message": False,
                        "sending_started_at": False,
                        "next_retry_at": False,
                        "sent_at": now,
                    }
                )
                record.conversation_id.sudo().write({"last_outbound_at": now})
                record.account_id.sudo().write(
                    {"last_outbound_at": now, "last_error": False, "last_error_at": False}
                )
                record._diag_log(
                    "outbound.sent",
                    "info",
                    _("Outbound message sent successfully."),
                    response_payload={
                        "external_message_id": result.get("external_message_id") or "",
                        "provider_request_id": result.get("provider_request_id") or "",
                        "reply_token_used": bool(result.get("reply_token_used")),
                    },
                    http_status=result.get("http_status") or 200,
                )
        return True

    def action_reset_draft(self):
        self.filtered(lambda record: record.direction == "outbound").sudo().write(
            {
                "state": "queued",
                "error_message": False,
                "retry_count": 0,
                "next_retry_at": False,
                "sending_started_at": False,
            }
        )
        self._trigger_outbound_processing()

    def action_retry_manual(self):
        self.filtered(
            lambda record: record.direction == "outbound" and record.state != "sent"
        ).sudo().write(
            {
                "state": "queued",
                "error_message": False,
                "retry_count": 0,
                "next_retry_at": False,
                "sending_started_at": False,
            }
        )
        self._trigger_outbound_processing()

    @api.model
    def _cron_retry_failed_outbound(self):
        now = fields.Datetime.now()
        stale_before = now - timedelta(minutes=15)
        exhausted = self.sudo().search(
            [
                ("direction", "=", "outbound"),
                ("state", "=", "failed"),
                ("next_retry_at", "=", False),
            ],
            limit=1000,
        ).filtered(
            lambda message: message.retry_count >= max(1, message.max_retries or 1)
        )
        if exhausted:
            exhausted.write({"state": "permanent_failed"})
        messages = self.sudo().search(
            [
                ("direction", "=", "outbound"),
                "|",
                ("state", "=", "queued"),
                "|",
                "&",
                ("state", "=", "failed"),
                ("next_retry_at", "<=", now),
                "&",
                ("state", "=", "sending"),
                ("sending_started_at", "<", stale_before),
            ],
            order="id",
            limit=100,
        )
        for index, message in enumerate(messages):
            if message.state == "failed" and message.retry_count >= max(1, message.max_retries or 1):
                message.sudo().write({"state": "permanent_failed", "next_retry_at": False})
                continue
            message.action_send_outbound()
            if hasattr(self.env["ir.cron"], "_commit_progress"):
                if not self.env["ir.cron"]._commit_progress(
                    processed=1,
                    remaining=max(0, len(messages) - index - 1),
                ):
                    break
