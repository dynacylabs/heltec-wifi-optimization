-- The auto-recovery watchdog (migration 014) went silent after exhausting
-- its reboot attempts - each individual reboot got a notification, but
-- once consecutive_auto_reboots hit the cap, every subsequent poll just
-- returned early with no further alert, so a radio that stayed down past
-- the last attempt could sit broken indefinitely with no signal that it
-- now needs a human. This flag lets that transition get exactly one clear
-- "giving up, manual intervention needed" notification, mirroring how
-- offline_alerted already works for plain unreachability.
ALTER TABLE devices ADD COLUMN auto_recovery_exhausted_alerted BOOLEAN NOT NULL DEFAULT false;
