-- Unified activity log for the dashboard - added 2026-08-11 per direct
-- request: one place to see every reboot (and why), every channel change
-- (commanded or not), and on-device recovery actions (chip-reset,
-- corrupted-defaults detection) that previously only ever showed up in an
-- ntfy push (if configured), `docker logs`, or a device's own `logread` -
-- none of which is a durable, queryable, dashboard-visible trail.
--
-- device_seq is only ever set for source='device' rows (see
-- wifi-agent.sh's record_event / cmd_collect) - it's the on-device
-- sequence number that lets the same on-device event log line, resent on
-- every ~30s `collect` poll until this server has actually seen it, be
-- ingested as a no-op past the first time rather than duplicated.
-- source='server' rows never set it (each is inserted exactly once, at
-- the moment the server itself decides/observes something).
CREATE TABLE device_events (
    id BIGSERIAL PRIMARY KEY,
    device_id UUID NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source TEXT NOT NULL CHECK (source IN ('server', 'device')),
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    details JSONB,
    device_seq BIGINT
);

CREATE INDEX device_events_occurred_at_idx ON device_events (occurred_at DESC);
CREATE UNIQUE INDEX device_events_device_seq_uidx ON device_events (device_id, device_seq) WHERE device_seq IS NOT NULL;
