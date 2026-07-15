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
}

# Rule-based optimizer thresholds. Deliberately simple for v1 - see
# optimizer.py for why channel/width selection itself is round-robin
# rather than scan-scored.
RETRY_RATE_DEGRADED_THRESHOLD = 0.15  # fraction of frames retried
DEGRADED_SUSTAIN_MINUTES = 10
CHANNEL_COOLDOWN_MINUTES = 360  # don't re-evaluate more than every 6h

# Bandwidth (widen/narrow) is a bigger, rarer, more disruptive decision
# than a same-bandwidth channel cycle, so it uses much longer sustain
# windows rather than just different thresholds - not evaluated at all
# while the link is currently degraded (see optimizer.py: fixing
# instability takes priority over widening/narrowing a healthy link).
# Utilization = throughput_mbps / rate_mbps, i.e. actual data demand
# against the currently negotiated PHY rate, not a fixed theoretical
# capacity table.
BANDWIDTH_WIDEN_UTILIZATION_THRESHOLD = 0.7
BANDWIDTH_WIDEN_SUSTAIN_MINUTES = 60
BANDWIDTH_NARROW_UTILIZATION_THRESHOLD = 0.1
BANDWIDTH_NARROW_SUSTAIN_MINUTES = 1440  # 24h

# Standard non-overlapping 2.4GHz channels (US). Unlike HaLow, there's no
# bandwidth-dependent numbering complexity here.
WIFI24_CHANNELS = [1, 6, 11]
