-- Bookkeeping for the HaLow radio-health auto-recovery watchdog (see
-- main.py's poll_telemetry / _maybe_auto_recover_radio) - added after the
-- 2026-08-01 outage where an AP stayed SSH-reachable the whole time but
-- its HaLow chip was wedged at the SDIO level, and nothing was watching
-- for "reachable but the radio itself is down" between the on-device
-- boot-time recovery check and a human noticing.
ALTER TABLE devices ADD COLUMN last_auto_reboot_at TIMESTAMPTZ;
ALTER TABLE devices ADD COLUMN consecutive_auto_reboots INTEGER NOT NULL DEFAULT 0;

-- 'unknown': the previous logic asserted a command had been safely
-- reverted purely because the server couldn't reach the device to check -
-- conflating "couldn't verify" with "confirmed reverted" is exactly what
-- hid that outage from the dashboard. See main.py's
-- check_in_flight_commands.
ALTER TYPE command_status ADD VALUE 'unknown';
