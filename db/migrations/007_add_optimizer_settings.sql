-- Moves the optimizer's tunable thresholds from hardcoded config.py
-- constants into the DB (same single-row table the kill switch already
-- uses), so they can be adjusted from the dashboard without a rebuild.
-- Defaults below match the config.py values they replace, so behavior is
-- unchanged until someone actually tunes them.
ALTER TABLE optimizer_state
    ADD COLUMN retry_rate_degraded_threshold DOUBLE PRECISION NOT NULL DEFAULT 0.15,
    ADD COLUMN degraded_sustain_minutes INTEGER NOT NULL DEFAULT 10,
    ADD COLUMN channel_cooldown_minutes INTEGER NOT NULL DEFAULT 360,
    ADD COLUMN bandwidth_widen_utilization_threshold DOUBLE PRECISION NOT NULL DEFAULT 0.7,
    ADD COLUMN bandwidth_widen_sustain_minutes INTEGER NOT NULL DEFAULT 60,
    ADD COLUMN bandwidth_narrow_utilization_threshold DOUBLE PRECISION NOT NULL DEFAULT 0.1,
    ADD COLUMN bandwidth_narrow_sustain_minutes INTEGER NOT NULL DEFAULT 1440;
