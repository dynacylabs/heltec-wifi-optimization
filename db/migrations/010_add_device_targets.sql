-- Device connection config, editable from the dashboard instead of being
-- baked into docker-compose.yml env vars (AP_SSH_HOST/STA_SSH_HOST/
-- SSH_USER are gone as of this migration - see README's "Configuring
-- device connections"). `role` is plain TEXT rather than reusing the
-- `device_role` enum (which is fixed at 'AP'/'STA') so this table itself
-- doesn't hardcode a two-device limit, even though the optimizer and
-- `devices` telemetry table still only reason about AP/STA today.
CREATE TABLE device_targets (
    id BIGSERIAL PRIMARY KEY,
    role TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    ssh_host TEXT NOT NULL,
    ssh_port INTEGER NOT NULL DEFAULT 22,
    ssh_user TEXT NOT NULL DEFAULT 'root',
    -- Set once a `/provision` call (dashboard "Provision Device" button)
    -- completes successfully - installs our SSH key, deploys
    -- wifi-agent.sh + the boot-init script, and optionally restores a
    -- config backup onto a brand-new device. NULL means never
    -- provisioned via the dashboard (e.g. set up by hand per the README's
    -- manual steps, which still works fine).
    provisioned_at TIMESTAMPTZ,
    last_provision_status TEXT,
    last_provision_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed with the same two real, already-deployed hosts this repo has
-- hardcoded since the SSH-pull design was introduced, so upgrading an
-- existing deployment doesn't start with blank/broken targets - edit
-- these from the dashboard's Device Setup section if they ever change.
INSERT INTO device_targets (role, label, ssh_host, ssh_port, ssh_user) VALUES
    ('AP', 'Access Point', '192.168.2.2', 22, 'root'),
    ('STA', 'Station', '192.168.2.3', 22, 'root');
