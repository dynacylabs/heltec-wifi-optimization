import os

DATABASE_URL = os.environ["DATABASE_URL"]

# Shared secret required on every dashboard-facing /api/* route. There is
# no device-facing HTTP API anymore (see "Reaching the devices" below) -
# the server reaches out to the devices over SSH instead, so this token's
# only remaining job is gating the browser-facing dashboard/API, which
# stays behind whatever reverse proxy/auth layer you put in front of this
# domain. Required, no default.
API_TOKEN = os.environ["API_TOKEN"]

DEFAULT_COMMAND_TTL_SECONDS = {
    "halow_operating_freq": 120,
    "wifi24_channel": 90,
    "reboot": 60,
}

# Reaching the devices: the server initiates every connection over SSH
# (app/ssh_client.py, app/device_client.py) rather than the old design
# where each device's agent pushed telemetry/commands-polling out to the
# server over HTTP. That flip exists specifically so this server never
# needs to accept inbound connections from the devices' network segment -
# see README's "Reaching the devices over SSH" for the full rationale.
#
# Which host/port/user to dial for each role (AP/STA) now lives in the
# `device_targets` DB table, editable from the dashboard's Device Setup
# section (see main.py's /api/device-targets) - not here. This lets a
# fresh deployment be pointed at brand-new devices without touching
# docker-compose.yml or restarting the container. What stays here is only
# the one thing shared across every device regardless of host/user: the
# server's own SSH keypair.
#
# Path *inside the container* to the private half of a keypair dedicated
# to this purpose (not your personal admin key - see README, same
# reasoning as the ntfy token being scoped to one job). Mount it in via
# docker-compose as a read-only volume; never bake it into the image.
#
# This (along with DATABASE_URL/API_TOKEN above) is deliberately one of
# the few things NOT DB-backed/dashboard-editable, unlike everything else
# in the old config.py - it's a bootstrapping secret the app needs before
# it can even open a DB connection, or a file path tied to a docker-compose
# volume mount that a running container can't retarget on its own anyway.
SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", "/run/secrets/wifi_optimizer_ssh_key")
# Public half of the same keypair - read at provisioning time to install
# onto a brand-new device's authorized_keys (see device_client.provision).
SSH_PUBLIC_KEY_PATH = os.environ.get("SSH_PUBLIC_KEY_PATH", SSH_KEY_PATH + ".pub")

# Standard non-overlapping 2.4GHz channels (US). Unlike HaLow, there's no
# bandwidth-dependent numbering complexity here. Structural (which channels
# exist), not a tunable threshold, so it stays here rather than in the
# DB-backed settings tables.
WIFI24_CHANNELS = [1, 6, 11]

# Everything else that used to live here as an env var - poll intervals
# (SSH/command/backup/optimizer/liveness), offline alert threshold,
# telemetry/backup retention, and ntfy alerting config - now lives in the
# DB (`app_settings` table, migration 011), editable from the dashboard's
# System Settings section, same as the optimizer's detection thresholds
# already were (`optimizer_state`, migration 007). See main.py's
# `_get_app_settings` and README's "Configuring app settings".
