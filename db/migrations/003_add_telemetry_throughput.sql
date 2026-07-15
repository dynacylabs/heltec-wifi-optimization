-- Actual data throughput (both directions combined) in Mbit/s, computed
-- from a cumulative byte counter delta - distinct from rate_mbps, which
-- is just the negotiated PHY link rate. Needed to compare real demand
-- against capacity for bandwidth widen/narrow decisions.
ALTER TABLE telemetry ADD COLUMN throughput_mbps DOUBLE PRECISION;
