-- Versioned config backups pushed by the agent (see wifi-agent.sh's
-- post_backup and main.py's /backup) - a small table, not a hypertable,
-- since a "row per meaningful config change" is a handful of rows per
-- device rather than a telemetry-style time series.
CREATE TABLE device_backups (
    id BIGSERIAL PRIMARY KEY,
    device_id UUID NOT NULL REFERENCES devices(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    archive BYTEA NOT NULL
);
CREATE INDEX ON device_backups (device_id, created_at DESC);
