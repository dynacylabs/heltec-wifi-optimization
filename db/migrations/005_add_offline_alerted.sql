-- Tracks whether we've already sent an "offline" alert for this device, so
-- the liveness check doesn't re-notify every pass while it stays down. Reset
-- to false (with a "back online" notice) the moment telemetry arrives again.
ALTER TABLE devices ADD COLUMN offline_alerted BOOLEAN NOT NULL DEFAULT false;
