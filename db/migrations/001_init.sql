CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE device_role AS ENUM ('AP', 'STA');
CREATE TYPE radio_type AS ENUM ('halow', 'wifi24');
CREATE TYPE command_param AS ENUM ('halow_operating_freq', 'wifi24_channel');
CREATE TYPE command_status AS ENUM ('pending', 'applied', 'acked', 'reverted', 'expired');

CREATE TABLE devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role device_role NOT NULL,
    mac MACADDR NOT NULL UNIQUE,
    hostname TEXT NOT NULL DEFAULT '',
    last_seen TIMESTAMPTZ
);

CREATE TABLE telemetry (
    time TIMESTAMPTZ NOT NULL,
    device_id UUID NOT NULL REFERENCES devices(id),
    radio radio_type NOT NULL,
    rssi INTEGER,
    noise INTEGER,
    mcs INTEGER,
    rate_mbps DOUBLE PRECISION,
    retries DOUBLE PRECISION, -- fraction of frames retried this interval, not a raw count
    channel INTEGER,
    bandwidth_mhz INTEGER
);
SELECT create_hypertable('telemetry', 'time');
CREATE INDEX ON telemetry (device_id, radio, time DESC);

CREATE TABLE radio_clients (
    time TIMESTAMPTZ NOT NULL,
    device_id UUID NOT NULL REFERENCES devices(id),
    radio radio_type NOT NULL,
    client_mac MACADDR NOT NULL,
    host TEXT,
    rssi INTEGER,
    rate_mbps DOUBLE PRECISION
);
SELECT create_hypertable('radio_clients', 'time');
CREATE INDEX ON radio_clients (device_id, radio, time DESC);

CREATE TABLE commands (
    id BIGSERIAL PRIMARY KEY,
    device_id UUID NOT NULL REFERENCES devices(id),
    param command_param NOT NULL,
    target_value JSONB NOT NULL,
    previous_value JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ttl_seconds INTEGER NOT NULL,
    status command_status NOT NULL DEFAULT 'pending',
    applied_at TIMESTAMPTZ,
    acked_at TIMESTAMPTZ,
    reason TEXT
);
CREATE INDEX ON commands (device_id, status);
