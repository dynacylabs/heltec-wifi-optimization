-- Lets the dashboard issue a remote reboot (e.g. the agent process is wedged
-- but the device itself is still up) without needing the uci apply/rollback
-- machinery that halow_operating_freq/wifi24_channel use - there's nothing
-- to roll back from a reboot.
ALTER TYPE command_param ADD VALUE 'reboot';
