import logging
import traceback
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import api, fields, models

from .provider_errors import ChatConnectPermanentError


_logger = logging.getLogger(__name__)


class ChatConnectInboundEvent(models.Model):
    _name = "chat.connect.inbound.event"
    _description = "Chat Connect Inbound Event"
    _order = "event_timestamp, id"
    _check_company_auto = True

    account_id = fields.Many2one(
        "chat.connect.account",
        required=True,
        ondelete="cascade",
        index=True,
        check_company=True,
    )
    company_id = fields.Many2one(related="account_id.company_id", store=True, index=True)
    event_uid = fields.Char(required=True, index=True)
    external_message_id = fields.Char(index=True)
    event_timestamp = fields.Datetime(default=fields.Datetime.now, required=True, index=True)
    payload_json = fields.Json(required=True, groups="chat_connect_center.group_chat_connect_manager")
    state = fields.Selection(
        [
            ("queued", "Queued"),
            ("processing", "Processing"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("permanent_failed", "Permanent Failure"),
            ("ignored", "Ignored"),
        ],
        default="queued",
        required=True,
        index=True,
    )
    retry_count = fields.Integer(default=0)
    max_retries = fields.Integer(default=10)
    next_retry_at = fields.Datetime(index=True)
    processing_started_at = fields.Datetime(index=True)
    processed_at = fields.Datetime(index=True)
    error_message = fields.Text(readonly=True, groups="chat_connect_center.group_chat_connect_manager")
    message_id = fields.Many2one("chat.connect.message", ondelete="set null", index=True)

    _event_unique = models.Constraint(
        "UNIQUE(account_id, event_uid)",
        "The provider event has already been received for this account.",
    )

    @api.model
    def enqueue(self, account, event_uid, payload, event_timestamp=None):
        event_uid = str(event_uid or "").strip()
        if not event_uid:
            raise ValueError("event_uid is required")
        domain = [("account_id", "=", account.id), ("event_uid", "=", event_uid)]
        existing = self.sudo().search(domain, limit=1)
        if existing:
            return existing, False

        try:
            with self.env.cr.savepoint():
                event = self.sudo().create(
                    {
                        "account_id": account.id,
                        "event_uid": event_uid,
                        "external_message_id": (payload or {}).get("message_id") or "",
                        "event_timestamp": event_timestamp or fields.Datetime.now(),
                        "payload_json": payload or {},
                    }
                )
        except IntegrityError:
            event = self.sudo().search(domain, limit=1)
            if not event:
                raise
            return event, False

        event._trigger_processing()
        return event, True

    def _trigger_processing(self):
        cron = self.env.ref("chat_connect_center.ir_cron_chat_connect_process_inbound", raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    def _process_event(self):
        self.ensure_one()
        conversation, error = self.env["chat.connect.conversation"].sudo().get_or_create_for_payload(
            self.account_id,
            self.payload_json or {},
        )
        if error:
            self.state = "ignored"
            self.error_message = error
            return False
        return conversation.ingest_inbound(self.payload_json or {})

    def action_retry_manual(self):
        self.filtered(lambda event: event.state != "done").sudo().write(
            {
                "state": "queued",
                "retry_count": 0,
                "next_retry_at": False,
                "processing_started_at": False,
                "processed_at": False,
                "error_message": False,
            }
        )
        self._trigger_processing()

    @api.model
    def _cron_process_pending(self):
        now = fields.Datetime.now()
        stale_before = now - timedelta(minutes=15)
        self.sudo().search(
            [("state", "=", "processing"), ("processing_started_at", "<", stale_before)]
        ).write(
            {
                "state": "failed",
                "next_retry_at": now,
                "error_message": "Recovered a stale processing event.",
            }
        )
        events = self.sudo().search(
            [
                ("state", "in", ("queued", "failed")),
                "|",
                ("next_retry_at", "=", False),
                ("next_retry_at", "<=", now),
            ],
            order="event_timestamp, id",
            limit=100,
        )
        progress = self.env["ir.cron"]
        for index, event in enumerate(events):
            self.env.cr.execute("SELECT pg_try_advisory_xact_lock(%s)", (910000000 + event.id,))
            if not self.env.cr.fetchone()[0]:
                continue
            event.write(
                {
                    "state": "processing",
                    "processing_started_at": fields.Datetime.now(),
                    "error_message": False,
                }
            )
            try:
                with self.env.cr.savepoint():
                    message = event._process_event()
            except ChatConnectPermanentError as err:
                event.write(
                    {
                        "state": "permanent_failed",
                        "processing_started_at": False,
                        "next_retry_at": False,
                        "error_message": str(err),
                    }
                )
                event.account_id.sudo().write(
                    {"last_error": str(err), "last_error_at": fields.Datetime.now()}
                )
                _logger.warning("Inbound event %s permanently failed: %s", event.id, err)
            except Exception as err:
                retry_count = event.retry_count + 1
                delay = min(30, 2 ** min(retry_count, 5))
                event.write(
                    {
                        "state": (
                            "permanent_failed"
                            if retry_count >= max(1, event.max_retries or 1)
                            else "failed"
                        ),
                        "retry_count": retry_count,
                        "next_retry_at": (
                            False
                            if retry_count >= max(1, event.max_retries or 1)
                            else fields.Datetime.now() + timedelta(minutes=delay)
                        ),
                        "processing_started_at": False,
                        "error_message": f"{err}\n{traceback.format_exc()}",
                    }
                )
                event.account_id.sudo().write(
                    {"last_error": str(err), "last_error_at": fields.Datetime.now()}
                )
                _logger.exception("Inbound event %s failed", event.id)
            else:
                if event.state != "ignored":
                    event.write(
                        {
                            "state": "done",
                            "message_id": message.id if message else False,
                            "processed_at": fields.Datetime.now(),
                            "processing_started_at": False,
                            "next_retry_at": False,
                            "error_message": False,
                        }
                    )
            if hasattr(progress, "_commit_progress"):
                if not progress._commit_progress(
                    processed=1,
                    remaining=max(len(events) - index - 1, 0),
                ):
                    break
