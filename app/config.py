import os

DATABASE_URL = os.environ["DATABASE_URL"]
OPTIMIZER_INTERVAL_SECONDS = int(os.environ.get("OPTIMIZER_INTERVAL_SECONDS", "300"))

# Shared secret required on every device-facing endpoint (as a ?token=
# query param, not a header - the OpenWrt agent only has busybox wget,
# which has no --header support). Required, no default: this becomes the
# only thing standing between the internet and this API once it's exposed
# through a reverse proxy.
API_TOKEN = os.environ["API_TOKEN"]

DEFAULT_COMMAND_TTL_SECONDS = {
    "halow_operating_freq": 120,
    "wifi24_channel": 90,
    "reboot": 60,
}

# Standard non-overlapping 2.4GHz channels (US). Unlike HaLow, there's no
# bandwidth-dependent numbering complexity here. Structural (which channels
# exist), not a tunable threshold, so it stays here rather than in the
# DB-backed optimizer_state settings (see optimizer.py).
WIFI24_CHANNELS = [1, 6, 11]

# All *thresholds* the optimizer uses (retry rate, sustain windows,
# utilization) now live in the DB (optimizer_state table, tunable from the
# dashboard's Settings section) instead of here - see optimizer.py and
# migration 007. What's left here is structural, not something you'd tune
# per-deployment.

# ntfy (https://ntfy.sh or self-hosted) push alerts for: a device going
# offline/coming back, a command getting reverted, and sustained
# degradation triggering an optimizer command. Optional - leave both blank
# to disable alerting entirely (this repo stays host-agnostic; ntfy is a
# convenience, not a dependency).
NTFY_URL = os.environ.get("NTFY_URL", "").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_ENABLED = bool(NTFY_URL and NTFY_TOPIC)

# How long a device can go without a telemetry POST before it's considered
# offline and worth alerting on (vs. the dashboard's 90s "online" dot, which
# is meant to be a quick glance, not gate a push notification).
OFFLINE_ALERT_SECONDS = int(os.environ.get("OFFLINE_ALERT_SECONDS", "300"))
# How often the liveness check runs.
LIVENESS_CHECK_INTERVAL_SECONDS = int(os.environ.get("LIVENESS_CHECK_INTERVAL_SECONDS", "60"))

# TimescaleDB retention: telemetry/radio_clients rows older than this get
# dropped automatically so the DB doesn't grow unbounded on a system meant
# to run unattended for months. Applied idempotently at startup (db.py) via
# add_retention_policy(if_not_exists=True) - changing this value after the
# policy already exists requires manually removing the old one first (see
# README), since if_not_exists won't update an existing policy's interval.
TELEMETRY_RETENTION_DAYS = int(os.environ.get("TELEMETRY_RETENTION_DAYS", "180"))
