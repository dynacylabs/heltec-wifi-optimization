-- fraction of frames retried this interval for this specific client, same
-- delta-based reasoning as telemetry.retries (see agent's delta_rate()).
ALTER TABLE radio_clients ADD COLUMN retries DOUBLE PRECISION;
