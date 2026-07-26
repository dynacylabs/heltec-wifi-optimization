-- Server-side delta-rate state. The old push-based agent kept per-poll
-- cumulative counters in /tmp on the device itself and computed a delta
-- there; now that the server initiates every collection over SSH as a
-- one-shot command invocation (see app/device_client.py) rather than a
-- continuously-running on-device process, there's nowhere on the device
-- to keep "since the last poll" state between invocations - so the
-- previous cumulative counters live here instead, and main.py computes
-- the rate against them each time a new collection lands.
CREATE TABLE device_radio_counters (
    device_id UUID NOT NULL REFERENCES devices(id),
    radio radio_type NOT NULL,
    retries_cum BIGINT,
    packets_cum BIGINT,
    tx_bytes_cum BIGINT,
    rx_bytes_cum BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, radio)
);

-- Same idea, but per downstream 2.4GHz client (the wifi24 radio can have
-- several associated clients, each with its own independent retry counter
-- - HaLow only ever has the one peer, which device_radio_counters above
-- already covers).
CREATE TABLE device_radio_client_counters (
    device_id UUID NOT NULL REFERENCES devices(id),
    radio radio_type NOT NULL,
    client_mac MACADDR NOT NULL,
    retries_cum BIGINT,
    packets_cum BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, radio, client_mac)
);
