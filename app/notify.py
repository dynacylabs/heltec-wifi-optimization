import logging

import asyncpg
import httpx

logger = logging.getLogger("wifi_optimizer.notify")


async def notify(conn: asyncpg.Connection, title: str, message: str, priority: str = "default", tags: str | None = None):
    # Best-effort - a failed push shouldn't ever break the caller (telemetry
    # ingestion, command reporting, the optimizer pass). ntfy_url/topic/token
    # live in the app_settings table (migration 011, dashboard's System
    # Settings section) rather than env vars, so they're read fresh here on
    # every call instead of once at import time - same reasoning as
    # optimizer_state's thresholds being re-fetched every optimizer pass.
    # A no-op whenever url/topic aren't set, so this repo has no hard
    # dependency on ntfy specifically.
    row = await conn.fetchrow("SELECT ntfy_url, ntfy_topic, ntfy_token FROM app_settings LIMIT 1")
    if not row or not row["ntfy_url"] or not row["ntfy_topic"]:
        return
    try:
        await _send(row, title, message, priority, tags)
    except Exception:
        logger.exception("ntfy notify failed (title=%r)", title)


async def _send(row, title: str, message: str, priority: str, tags: str | None):
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if row["ntfy_token"]:
        headers["Authorization"] = f"Bearer {row['ntfy_token']}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{row['ntfy_url'].rstrip('/')}/{row['ntfy_topic']}", content=message.encode(), headers=headers,
        )
        resp.raise_for_status()


async def send_test_notification(conn: asyncpg.Connection) -> bool:
    # Backs the dashboard's System Settings "Send Test Notification"
    # button (POST /api/app-settings/test-notification). Returns False
    # without attempting anything if ntfy isn't configured. Unlike
    # notify(), deliberately does NOT swallow the send failing - a bad
    # url/token should be visible to whoever just clicked the button, not
    # silently logged server-side like the real alert paths.
    row = await conn.fetchrow("SELECT ntfy_url, ntfy_topic, ntfy_token FROM app_settings LIMIT 1")
    if not row or not row["ntfy_url"] or not row["ntfy_topic"]:
        return False
    await _send(
        row, "\u2705 Test notification", "ntfy alerts are configured correctly.",
        "default", "white_check_mark",
    )
    return True
