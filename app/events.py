from datetime import datetime, timezone


async def record_event(
    conn, device_id, source: str, event_type: str, message: str,
    details: dict | None = None, device_seq: int | None = None, occurred_at: datetime | None = None,
) -> None:
    """Append a row to device_events - the dashboard's unified activity log
    (reboots and why, channel changes and whether they were commanded,
    on-device recovery actions), distinct from notify() (ntfy pushes meant
    to page a human, gated on ntfy being configured at all) and the
    commands table (per-command apply/verify lifecycle) - this is the one
    place meant to answer "what has this thing been doing" at a glance.

    device_seq is set only for source='device' events (see
    main.py's _ingest_device_events) - ON CONFLICT DO NOTHING against the
    partial unique index on (device_id, device_seq) (migration 017) is
    what makes re-ingesting the same on-device event line on a later
    `collect` poll harmless instead of duplicating it. source='server'
    events always have device_seq=None, which that partial index doesn't
    cover, so they always insert fresh - each is recorded exactly once, at
    the moment the server itself decides or observes something.
    """
    await conn.execute(
        """
        INSERT INTO device_events (device_id, occurred_at, source, event_type, message, details, device_seq)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (device_id, device_seq) WHERE device_seq IS NOT NULL DO NOTHING
        """,
        device_id, occurred_at or datetime.now(timezone.utc), source, event_type, message, details, device_seq,
    )
