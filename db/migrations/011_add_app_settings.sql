-- Moves every remaining "how often/how long/where to alert" tunable out
-- of docker-compose.yml env vars into the DB, so the whole app can be
-- configured from the dashboard's new System Settings section without a
-- restart - the same reasoning migration 007 already applied to the
-- optimizer's detection thresholds. Single-row table, same `id = true`
-- singleton pattern as optimizer_state (migration 004).
--
-- Defaults below match the env var defaults these replace, so behavior is
-- unchanged immediately after migrating - but if your docker-compose.yml
-- had any of these customized away from the default, that customization
-- is NOT picked up automatically; re-enter it once via the dashboard
-- after migrating.
--
-- ntfy_token is a bearer credential and is stored here in plaintext, same
-- as any other DB-resident secret in this schema - restrict DB access
-- accordingly. The dashboard/API never echo it back in the clear once
-- set (GET only reports whether it's set); POSTing a blank value leaves
-- the existing token unchanged rather than clearing it.
CREATE TABLE app_settings (
    id BOOLEAN PRIMARY KEY DEFAULT true CHECK (id = true),
    ssh_poll_interval_seconds INTEGER NOT NULL DEFAULT 30,
    command_poll_interval_seconds INTEGER NOT NULL DEFAULT 10,
    command_verify_delay_seconds INTEGER NOT NULL DEFAULT 25,
    backup_poll_interval_seconds INTEGER NOT NULL DEFAULT 21600,
    optimizer_interval_seconds INTEGER NOT NULL DEFAULT 300,
    liveness_check_interval_seconds INTEGER NOT NULL DEFAULT 60,
    offline_alert_seconds INTEGER NOT NULL DEFAULT 300,
    telemetry_retention_days INTEGER NOT NULL DEFAULT 0,
    backup_retention_count INTEGER NOT NULL DEFAULT 30,
    ntfy_url TEXT NOT NULL DEFAULT '',
    ntfy_topic TEXT NOT NULL DEFAULT '',
    ntfy_token TEXT NOT NULL DEFAULT ''
);
INSERT INTO app_settings DEFAULT VALUES;
