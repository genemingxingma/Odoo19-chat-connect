import uuid


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    return bool(cr.fetchone()[0])


def _column_exists(cr, table, column):
    cr.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = %s
           AND column_name = %s
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def _merge_duplicate_conversations(cr):
    if not _table_exists(cr, "chat_connect_conversation"):
        return
    required_columns = ("account_id", "external_conversation_id")
    if not all(
        _column_exists(cr, "chat_connect_conversation", column)
        for column in required_columns
    ):
        return

    order_parts = []
    if _column_exists(cr, "chat_connect_conversation", "mail_channel_id"):
        order_parts.append("(mail_channel_id IS NOT NULL) DESC")
    if _column_exists(cr, "chat_connect_conversation", "last_inbound_at"):
        order_parts.append("last_inbound_at DESC NULLS LAST")
    order_parts.append("id DESC")
    order_sql = ", ".join(order_parts)
    cr.execute(
        f"""
        SELECT account_id, external_conversation_id, array_agg(
            id ORDER BY {order_sql}
        )
          FROM chat_connect_conversation
         GROUP BY account_id, external_conversation_id
        HAVING count(*) > 1
        """
    )
    for _account_id, _external_id, conversation_ids in cr.fetchall():
        canonical_id, *duplicate_ids = conversation_ids
        if not duplicate_ids:
            continue

        if _column_exists(cr, "chat_connect_message", "conversation_id"):
            cr.execute(
                "UPDATE chat_connect_message SET conversation_id = %s WHERE conversation_id = ANY(%s)",
                (canonical_id, duplicate_ids),
            )
        if _column_exists(cr, "chat_connect_diagnostic_log", "conversation_id"):
            cr.execute(
                "UPDATE chat_connect_diagnostic_log SET conversation_id = %s WHERE conversation_id = ANY(%s)",
                (canonical_id, duplicate_ids),
            )
        if (
            _column_exists(cr, "discuss_channel", "chat_connect_conversation_id")
            and _column_exists(
                cr, "chat_connect_conversation", "mail_channel_id"
            )
        ):
            cr.execute(
                """
                UPDATE discuss_channel
                   SET chat_connect_conversation_id = CASE
                       WHEN id = (
                           SELECT mail_channel_id
                             FROM chat_connect_conversation
                            WHERE id = %s
                       ) THEN %s
                       ELSE NULL
                   END
                 WHERE chat_connect_conversation_id = ANY(%s)
                """,
                (canonical_id, canonical_id, duplicate_ids),
            )
        cr.execute(
            "DELETE FROM chat_connect_conversation WHERE id = ANY(%s)",
            (duplicate_ids,),
        )


def _repair_idempotency_keys(cr):
    if not _table_exists(cr, "chat_connect_message") or not _column_exists(
        cr, "chat_connect_message", "idempotency_key"
    ):
        return
    cr.execute(
        "SELECT id, direction, idempotency_key FROM chat_connect_message ORDER BY id"
    )
    seen = set()
    updates = []
    for message_id, direction, key in cr.fetchall():
        if direction != "outbound":
            if key:
                updates.append((None, message_id))
            continue
        if not key or key in seen:
            key = str(uuid.uuid4())
            updates.append((key, message_id))
        seen.add(key)
    if updates:
        cr.executemany(
            "UPDATE chat_connect_message SET idempotency_key = %s WHERE id = %s",
            updates,
        )


def _repair_duplicate_event_ids(cr):
    if not _table_exists(cr, "chat_connect_message") or not _column_exists(
        cr, "chat_connect_message", "external_event_id"
    ):
        return
    cr.execute(
        """
        SELECT account_id, direction, external_event_id, array_agg(id ORDER BY id)
          FROM chat_connect_message
         WHERE external_event_id IS NOT NULL
         GROUP BY account_id, direction, external_event_id
        HAVING count(*) > 1
        """
    )
    duplicate_ids = []
    for _account_id, _direction, _event_id, message_ids in cr.fetchall():
        duplicate_ids.extend(message_ids[1:])
    if duplicate_ids:
        cr.execute(
            "UPDATE chat_connect_message SET external_event_id = NULL WHERE id = ANY(%s)",
            (duplicate_ids,),
        )


def migrate(cr, version):
    _merge_duplicate_conversations(cr)
    _repair_idempotency_keys(cr)
    _repair_duplicate_event_ids(cr)
