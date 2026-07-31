-- Separate, narrower kill switch for HaLow channel/bandwidth changes,
-- independent of the master `enabled` switch (migration 004). Added after
-- a live incident (2026-07-31): every attempted halow_operating_freq
-- change (two same-bandwidth channel hops, one bandwidth narrow) failed
-- to actually apply on the Morse Micro MM6108A1 hardware, and the third
-- attempt left the AP's radio in a broken state that required a physical
-- reboot plus manual recovery to fix - the on-device self-recovery script
-- only recovers toward whatever channel is in its uci config, which was
-- left pointing at the failed target, not the last-known-good channel.
--
-- Defaults to false (disabled) since the HaLow channel plan has an
-- unresolved, confirmed-live reliability problem on this hardware - see
-- the README/Gotchas. The master `enabled` switch can stay on for the
-- unrelated, unaffected 2.4GHz degraded-link channel cycling
-- (_evaluate_wifi24_link) without this flag also being on.
ALTER TABLE optimizer_state
    ADD COLUMN halow_channel_optimization_enabled BOOLEAN NOT NULL DEFAULT false;
