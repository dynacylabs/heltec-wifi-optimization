import logging

import httpx

from config import NTFY_ENABLED, NTFY_TOKEN, NTFY_TOPIC, NTFY_URL

logger = logging.getLogger("hobocams.notify")


async def notify(title: str, message: str, priority: str = "default", tags: str | None = None):
    # Best-effort - a failed push shouldn't ever break the caller (telemetry
    # ingestion, command reporting, the optimizer pass). Silently a no-op
    # when NTFY_URL/NTFY_TOPIC aren't set, so this repo has no hard
    # dependency on ntfy specifically.
    if not NTFY_ENABLED:
        return
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{NTFY_URL}/{NTFY_TOPIC}", content=message.encode(), headers=headers)
            resp.raise_for_status()
    except Exception:
        logger.exception("ntfy notify failed (title=%r)", title)
