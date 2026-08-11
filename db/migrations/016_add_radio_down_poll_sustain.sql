-- Requires the radio to be reported down for multiple consecutive polls
-- before the auto-recovery watchdog reboots the device - added 2026-08-11
-- code review. A single bad `radio_up=false` reading can come from a
-- genuinely wedged radio, but it can equally come from a marginal RF link
-- flapping for a few seconds (confirmed live: the STA did exactly this,
-- recovering on its own within seconds) - rebooting on the first bad
-- reading reacts to noise as if it were a real fault, and a reboot is
-- disruptive enough (1-3 minutes down) that doing it for a transient flap
-- is worse than doing nothing. See main.py's _maybe_auto_recover_radio.
ALTER TABLE devices ADD COLUMN consecutive_radio_down_polls INTEGER NOT NULL DEFAULT 0;
