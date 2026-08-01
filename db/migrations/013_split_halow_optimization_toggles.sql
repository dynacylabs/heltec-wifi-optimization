-- Replaces the single halow_channel_optimization_enabled switch
-- (migration 012) with three independently toggleable ones, matching
-- the optimizer's actual distinct behaviors:
--   - halow_channel_cycling_enabled: same-bandwidth channel hop on a
--     degraded link (_evaluate_halow_link branch 1)
--   - halow_bandwidth_changes_enabled: widen/narrow (branches 2/3) -
--     was unconditionally hard-disabled until 2026-08-01's fix (see
--     migration 012's original comment); now independently toggleable
--     like the rest, since the actual apply-mechanism bug is fixed
--     rather than worked around
--   - wifi24_channel_cycling_enabled: the 2.4GHz link's degraded-
--     response channel cycling (_evaluate_wifi24_link) - previously
--     had no toggle at all, always ran whenever the master `enabled`
--     switch was on. Defaults to true here specifically to preserve
--     that existing behavior for deployments upgrading in place -
--     everything else here defaults to false, matching the
--     conservative default migration 012 established for HaLow.
--
-- No "2.4GHz bandwidth" toggle: standard Wi-Fi channels don't have
-- HaLow's bandwidth-tier numbering, so there's no widen/narrow
-- behavior on that radio to gate - see _evaluate_wifi24_link's own
-- comment.
ALTER TABLE optimizer_state
    ADD COLUMN halow_channel_cycling_enabled BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN halow_bandwidth_changes_enabled BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN wifi24_channel_cycling_enabled BOOLEAN NOT NULL DEFAULT true;

-- Carry forward whatever the old combined switch was already set to,
-- for deployments where it had been changed from the default.
UPDATE optimizer_state SET
    halow_channel_cycling_enabled = halow_channel_optimization_enabled,
    halow_bandwidth_changes_enabled = halow_channel_optimization_enabled;

ALTER TABLE optimizer_state DROP COLUMN halow_channel_optimization_enabled;
