# heltec-wifi-optimization

Central optimization server for a Heltec HT-HD01-V2 Wi-Fi HaLow point-to-point
bridge (one AP, one STA). The server initiates every connection itself —
it SSHes into each device on a schedule to pull telemetry, push config
changes, and pull config backups (see "Reaching the devices over SSH"
below) — runs a rule-based optimizer against the telemetry, and applies
config changes with a self-reverting safety mechanism that lives on the
device, not the server, so a bad change can't permanently sever the link.

## Requirements

- Two Heltec HT-HD01-V2 dongles, factory reset, paired, firmware 2.8.5 or
  later (OpenWrt-based), one in AP mode and one in STA mode, both reachable
  over SSH (dropbear, root) from wherever you run this server — this is
  the only connectivity the server needs to and from the devices; no
  inbound connectivity from the devices to the server is required at all.
- Docker + Docker Compose on the server.
- An SSH keypair dedicated to this server (not your personal key) — see
  "Reaching the devices over SSH" below.

## Deploying the server

Credentials live directly in `docker-compose.yml` (no `.env` file) —
**edit the `changeme` values before deploying, and if you keep this repo
public, do not commit your real values.** Either keep them as a local,
uncommitted edit, or maintain them in a private fork/branch.

1. Clone this repo wherever you want to run it.
2. Generate a dedicated SSH keypair for the server to use against the
   devices (don't reuse your personal key):
   ```sh
   mkdir -p secrets
   ssh-keygen -t ed25519 -N "" -C wifi-optimizer-app -f secrets/wifi_optimizer_ed25519
   ```
   `secrets/` is gitignored — this key never gets committed. See
   "Reaching the devices over SSH" below for installing the public half
   on both devices.
3. Edit `docker-compose.yml` and replace every `changeme` with a real
   value (`POSTGRES_PASSWORD` is set on `timescaledb` and must match the
   password embedded in the `app` service's `DATABASE_URL`).
   Also set `API_TOKEN` on the `app` service — this now only protects the
   dashboard/its backing `/api/*` endpoints (see "API authentication"
   below), the devices themselves are never given this token. The AP/STA
   SSH host/port/user are no longer set here at all — they're seeded with
   placeholder values by the first migration and edited afterward from
   the dashboard's Device Setup section (see "Configuring device
   connections" below), so a brand-new deployment can be pointed at real
   hardware without touching this file or rebuilding. Poll intervals,
   alert/retention thresholds, and ntfy push alerts are likewise not set
   here — they're edited from the dashboard's System Settings section
   after deploying (see "Configuring app settings" below).
4. `docker compose up -d --build`
   - First boot runs everything in `db/migrations/` automatically
     (Postgres only runs `/docker-entrypoint-initdb.d` on an empty data
     volume). On an **existing** deployment (data volume already has
     data), Postgres won't re-run these — apply any new migration files
     by hand, e.g.:
     `docker compose exec -T timescaledb psql -U wifioptimizer -d wifioptimizer < db/migrations/005_add_offline_alerted.sql`
     (repeat for each new numbered file you haven't applied yet).
5. Check `http://<host>:8080/health` returns `{"status": "ok"}`.
6. `http://<host>:8080/` (redirects to `/dashboard`) is the built-in status
   page — see below for what it shows.

## Status page

`http://<host>:8080/` (redirects to `/dashboard`) is a self-contained
status page served directly by the app — a single HTML document with two
tabs (`/dashboard` and `/settings`, switched client-side without a full
page reload): **Dashboard** for live status/history/command activity,
**Settings** for everything configuration-related (device connections,
optimizer thresholds, poll intervals, alerting).

**Dashboard tab:**

- **Live status cards** — online/offline per device, current HaLow
  channel/bandwidth/RSSI/noise/rate/retries, current 2.4GHz channel and
  client count, and uptime % over whichever time range is selected below
  (computed via gap analysis over the telemetry heartbeat, bookended at
  the window edges so an outage right at the start or still ongoing "now"
  both count).
- **HaLow history charts** — RSSI/noise, retries, bandwidth — over a
  selectable time range (1h/6h/1d/7d/30d/90d/6mo/12mo). Longer ranges are
  downsampled server-side (`time_bucket`, capped around 600 points per
  chart regardless of span) rather than shipping raw 30s-interval rows to
  the browser — at 12mo that'd be over a million rows per series.
- **2.4GHz history charts** — channel over time, and per-client RSSI (one
  line per connected device on the STA's downstream SSID) — a client with
  no line for a stretch was
  disconnected during that window.
- **Command history table** — what was attempted, target value, and
  whether it was kept (`pending` → `applied` → `acked`/`reverted`/`expired`).
- **Kill switch** — pauses the optimizer so it stops issuing any *new*
  commands (`optimizer_state` table, `GET`/`POST /api/optimizer`).
  Deliberately does not touch anything already in flight - those still go
  through their own rollback safety net regardless. Meant for exactly the
  moment the STA is somewhere hard to physically reach and you want to
  just observe real telemetry for a while before trusting the optimizer
  to act on its own.
- **Reboot button** — queues a `reboot` command for that device through
  the normal command flow (`POST /api/devices/{mac}/reboot`), asks for
  confirmation first. Unlike `halow_operating_freq`/`wifi24_channel`,
  there's no uci apply/rollback/verify around this — the device is about
  to disappear, so there's nothing to roll back. Meant for "the device is
  up but wedged in a bad radio/network state," which otherwise has no
  remote recovery path once the STA is somewhere you can't easily walk
  over to.

**Settings tab:**

- **Device Setup** — per-role (AP/STA) SSH host/port/user/label, editable
  directly (`device_targets` table, `GET`/`POST /api/device-targets/{role}`),
  plus a "Provision Device" action that fully bootstraps a brand-new or
  factory-reset device over one password-authenticated SSH session — see
  "Configuring device connections and zero-touch provisioning" below.
- **Config Backups** — a table of every stored config backup version
  (time, device, size, hash), a direct download link per row, and Restore
  to push a version back onto its device now — see "Automated config
  backups" below.
- **Optimizer Settings** — the rule-based thresholds (retry-rate
  degradation trigger, sustain windows, bandwidth widen/narrow
  utilization) are editable here directly (`optimizer_state` table,
  `GET`/`POST /api/settings`) instead of being hardcoded in `config.py` —
  tune them as real telemetry comes in without a rebuild/redeploy.
- **System Settings** — every poll interval, alert/retention threshold,
  and the ntfy push alert config, editable here directly (`app_settings`
  table, `GET`/`POST /api/app-settings`) instead of being fixed
  `docker-compose.yml` env vars — changes take effect immediately (the
  scheduler's jobs are rescheduled live), no restart needed. See
  "Configuring app settings" below.

The API token is injected server-side when rendering the page — no
prompt, nothing stored in the browser. Access control for a human is
expected to happen at the reverse proxy (Authelia or similar) in front of
whichever domain you point at this page; the token itself stays required
on every backing endpoint (`/api/status`, `/api/telemetry/{mac}`,
`/api/radio-clients/{mac}`, `/api/commands`) as defense in depth — see
"API authentication" below.

## Deploying the agent (on each Heltec device)

No package installs needed — the script only uses tools already on the
firmware (`jsonfilter`, `ubus`, `uci`, `tar`). Unlike the old design,
there's no config file and nothing continuously running: the server
invokes this script over SSH, one short-lived command at a time.

**This entire section can be skipped** if you use the dashboard's
"Provision Device" action instead (see "Configuring device connections
and zero-touch provisioning" below) — it does all three steps below for
you over one password-authenticated SSH session. Do it by hand only if
you'd rather not type the device's root password into the dashboard, or
you're troubleshooting a provisioning failure.

1. Copy `agent/wifi-agent.sh` to `/usr/bin/wifi-agent.sh` on the
   device, `chmod +x` it.
2. Copy `agent/wifi-agent-boot.init` to `/etc/init.d/wifi-agent-boot`,
   `chmod +x` it, then `/etc/init.d/wifi-agent-boot enable`. This only
   runs the HaLow radio recovery check once at boot (see the script's
   header comment for why that specifically can't wait for the server) —
   it does not start any long-running process.
3. Make sure the server's public key is in that device's
   `authorized_keys` — see "Reaching the devices over SSH" below.

Repeat for both the AP and the STA — the script itself is identical on
both; the server decides which role it's talking to based on which
configured host (see "Configuring device connections" below) it dialed.

## Reaching the devices over SSH

The Heltec devices sit on a VLAN that can reach the server, but not the
other way around by default in most segmented setups — so rather than
exposing an HTTP API for the devices to call home to (which would need
either a hole punched from their VLAN to the server, or a public-facing
endpoint), the server dials out to the devices instead. Every telemetry
poll, config change, and backup pull is a short SSH command the server
initiates on its own schedule (`ssh_poll_interval_seconds`,
`command_poll_interval_seconds`, `backup_poll_interval_seconds` — see
"Configuring app settings" below) — nothing needs to be reachable in the
other direction at all.

**Trade-off, stated plainly:** this trades a narrow, dashboard-scoped
bearer token (the old device-facing API's `API_TOKEN`, which could only
ever POST telemetry/read a queued command/report a result) for a
full-root SSH key that can run anything on the device. That's a strictly
bigger blast radius if the server itself, or this key, is ever
compromised. It's the right trade here specifically because it lets the
devices stay on a VLAN with zero inbound exposure to anything — the
alternative (an internet-reachable HTTP API) has its own, arguably worse,
exposure. Keep the private key readable only by the `app` container
(`secrets/` is `chmod`-restricted and gitignored; the compose file mounts
it read-only), and rotate it if you ever suspect it's been exposed.

Setup (repeat for both the AP and the STA — see "Configuring device
connections" below for where each device's host/port/user is set):

1. Generate the key once (see "Deploying the server" step 2 above) —
   `secrets/wifi_optimizer_ed25519` (private) and
   `secrets/wifi_optimizer_ed25519.pub` (public).
2. Copy the **public** key onto each device's dropbear `authorized_keys`.
   Dropbear (this firmware's SSH server, not full OpenSSH) reads
   `/etc/dropbear/authorized_keys`:
   ```sh
   ssh root@<device-ip> "mkdir -p /etc/dropbear && cat >> /etc/dropbear/authorized_keys" < secrets/wifi_optimizer_ed25519.pub
   ```
   Or skip this step entirely and let the dashboard's "Provision Device"
   action install it for you via a one-time password login — see
   "Configuring device connections" below.
3. Confirm the server can log in non-interactively with the new key
   (from wherever `docker compose` runs, using the private key):
   ```sh
   ssh -i secrets/wifi_optimizer_ed25519 -o BatchMode=yes root@<device-ip> echo ok
   ```
4. `docker compose up -d` (or restart the `app` service) once both
   devices accept the key — the scheduler starts polling immediately.

## Configuring device connections and zero-touch provisioning

Each device's SSH host/port/user lives in the `device_targets` DB table
(seeded with placeholder addresses by migration `010_add_device_targets.sql`),
not in `docker-compose.yml` — edit it from the dashboard's **Device
Setup** section (`GET`/`POST /api/device-targets/{role}`). This is what
lets a fresh deployment be pointed at brand-new hardware without touching
config files or restarting the container: point the dashboard at the new
devices' real IPs, then use provisioning (below) to set them up.

**Provisioning** (the dashboard's "Provision Device…" button, `POST
/api/device-targets/{role}/provision`) does the full "Deploying the
agent" + "Reaching the devices over SSH" setup in one action, against a
brand-new or factory-reset device that's reachable at the configured
host/port but doesn't have the server's key installed yet:

1. Logs in once using the root password you type into the dashboard form.
2. Installs the server's public key into `/etc/dropbear/authorized_keys`
   (idempotent — safe to re-run against an already-provisioned device).
3. Pushes `agent/wifi-agent.sh` and `agent/wifi-agent-boot.init` onto the
   device via SFTP, `chmod`s them executable, and enables the boot-init
   script.
4. Optionally restores a previously-stored config backup (pick one from
   the dropdown) onto the device before it comes back up — see
   "Restoring a backup" below.
5. Reboots the device to apply everything cleanly.

**The password is never stored, logged, or written to disk** — it's held
only in memory for the duration of that one bootstrap SSH session
(`device_client.provision`) and discarded immediately after. All ongoing
polling afterward uses only the shared SSH keypair, exactly as described
above.

Each role's card also shows whether/when it was last provisioned
(`provisioned_at`, `last_provision_status`, `last_provision_error`) so a
failed attempt (wrong password, unreachable host, etc.) is visible
without digging through server logs.

**Rotating the key:** generate a new keypair, append the new public key
to both devices' `authorized_keys` alongside the old one, point
`SSH_KEY_PATH` (or the `secrets/` file the compose volume mount uses) at
the new private key and restart `app`, confirm polling resumes
successfully, then remove the old public key line from both devices.

## Configuring app settings

Every poll interval, alert/retention threshold, and the ntfy push alert
config lives in the singleton `app_settings` DB table (migration
`011_add_app_settings.sql`, seeded with defaults that match the old
`docker-compose.yml` env vars they replace) — edit them from the
dashboard's **System Settings** section (`GET`/`POST /api/app-settings`)
instead of editing `docker-compose.yml` and redeploying. Changes take
effect immediately: the affected APScheduler job(s) are rescheduled live
(`scheduler.reschedule_job`, matched by the fixed job ids assigned in
`main.py`'s `lifespan()`), and a changed `telemetry_retention_days` is
re-applied via `ensure_retention_policies` in the same request — no
restart needed.

Fields, grouped as they appear in the dashboard:

- **Polling Intervals** — `ssh_poll_interval_seconds` (telemetry),
  `command_poll_interval_seconds` (applying/verifying queued commands),
  `command_verify_delay_seconds` (how long to wait after applying a
  command before checking whether it stuck), `backup_poll_interval_seconds`
  (config backup pulls), `optimizer_interval_seconds` (rule evaluation
  pass), `liveness_check_interval_seconds` (offline detection).
- **Alerting & Retention** — `offline_alert_seconds` (how long without
  telemetry before a device is considered offline), `telemetry_retention_days`
  (0 = keep forever), `backup_retention_count` (0 = keep every version
  forever).
- **Push Notifications (ntfy)** — `ntfy_url`, `ntfy_topic`, and an access
  `ntfy_token` — see "Alerting" below for what these do and how to
  generate a token.

**The ntfy token is never sent back to the browser.** The GET endpoint
only reports whether one is currently set (`ntfy_token_set`), never the
value itself; the dashboard's token field is always blank on load, and
saving with it left blank leaves whatever's already stored untouched
(same pattern as most "update credentials" forms — there's no separate
"clear the token" action besides also blanking `ntfy_url`/`ntfy_topic`,
which disables alerting entirely). It's stored in plaintext in the
database, same trust boundary as everything else in it (`DATABASE_URL`
credentials, device SSH targets) — not currently encrypted at rest.

Migrating from an existing deployment that had these set as env vars: the
migration seeds the table with the same defaults the code used to have,
but does **not** read your old `docker-compose.yml` values — re-enter any
customized values (a non-default poll interval, your `NTFY_URL`/`NTFY_TOPIC`/`NTFY_TOKEN`)
into the dashboard's System Settings section once after upgrading. The
env vars themselves are no longer read at all and can be removed from
`docker-compose.yml`.

## API authentication

`API_TOKEN` now only protects the **dashboard** and its backing endpoints
(`/api/status`, `/api/telemetry/{mac}`, `/api/radio-clients/{mac}`,
`/api/commands`, `/api/optimizer`, `/api/settings`, `/api/app-settings`,
`/api/app-settings/test-notification`, `/api/devices/{mac}/reboot`,
`/api/backups*`, `/api/device-targets*`) —
passed as a `?token=` query parameter, matching the existing pattern used
throughout this API. `/health` is intentionally left open for plain
uptime checks. There is no longer any device-facing API at all — the
devices never receive this token and never call back into the server;
see "Reaching the devices over SSH" above for how that communication
happens instead.

This matters most if you put this server behind a reverse proxy reachable
from the internet. In that case, don't put this endpoint behind an
SSO/forward-auth layer for the machine-readable JSON endpoints (they have
no login flow) — the token is the only protection for those, so treat it
like a real secret. The dashboard HTML page itself is a good candidate
for an additional auth layer (Authelia or similar) since a human is the
one loading it.

## Alerting

Optional [ntfy](https://ntfy.sh) push alerts (self-hosted or ntfy.sh) —
configured entirely from the dashboard's System Settings section (`ntfy_url`,
`ntfy_topic`, `ntfy_token` — `app_settings` table, `GET`/`POST
/api/app-settings`), not `docker-compose.yml`. Leave `ntfy_url` or
`ntfy_topic` blank and alerting is entirely disabled (`app/notify.py`
no-ops). A failed push never breaks the caller — it's logged and swallowed,
never raised. The token is write-only from the dashboard's perspective: the
GET endpoint only ever reports whether one is currently set, never its
value, and leaving the token field blank when saving leaves whatever's
already stored untouched.

**Send Test Notification** (System Settings section, `POST
/api/app-settings/test-notification`) sends a real push using whatever's
currently saved in the database — not whatever's currently typed into the
form, so save any URL/topic/token changes first. Unlike the real alert
paths, a failed send here is surfaced back to the button (bad URL, wrong
token, unreachable ntfy instance) instead of only being logged
server-side, since the whole point is confirming the config actually
works before relying on it.

If your ntfy instance doesn't allow anonymous publish (e.g.
`auth-default-access: deny-all`), also set a token. Recommended setup:
a dedicated user scoped to write-only access on just this topic, rather
than reusing a personal account:
```
docker exec -e NTFY_PASSWORD=... <ntfy-container> ntfy user add --role=user wifi-optimizer
docker exec <ntfy-container> ntfy access wifi-optimizer <topic> write-only
docker exec <ntfy-container> ntfy token add -l wifi-optimizer-app wifi-optimizer
```
Use the `tk_...` token output from the last command as the dashboard's
access token field — it can be revoked independently of the account
password later.

What triggers an alert:

- **Device offline** — no telemetry collected for longer than
  `offline_alert_seconds` (default 300s; checked every
  `liveness_check_interval_seconds`, default 60s — both editable from
  System Settings). Alerts once per outage, not on every check — and
  sends a follow-up "back online" notice the moment telemetry resumes.
- **Command reverted** — any command (channel/bandwidth change) that
  `check_in_flight_commands` marks as `reverted` (target never actually
  reached, or the device couldn't be reached to confirm within its TTL).
- **Sustained degradation** — specifically when the optimizer's
  degradation detector fires and cycles a channel (HaLow or 2.4GHz). Note
  this does *not* fire for bandwidth widen/narrow — those happen on an
  otherwise-healthy link and aren't actionable/urgent the way real
  degradation is.

This intentionally does not try to be a general-purpose monitoring
solution — it's a small, direct wiring of the events that actually matter
for "is the link still doing its job," using infrastructure (`ntfy`) most
homelab setups already have running rather than adding a new dependency.

## Automated config backups

The server pulls a versioned snapshot of each device's config on its own
schedule (`backup_poll_interval_seconds` — dashboard's System Settings
section, default 21600s / 6h) — `tar czf` over `/etc/config`
(all UCI config), `/usr/bin/wifi-agent.sh`,
`/etc/init.d/wifi-agent-boot`, and `/etc/crontabs/root` if present,
streamed back over the same SSH connection everything else uses (see
"Reaching the devices over SSH"). This is the same set of files a manual
SSH-based backup (like `hobo_cams-main`'s `scripts/backup.sh`) would
pull, just automated and versioned server-side.

- **Automated** — runs on its own scheduler job (`poll_backups` in
  `main.py`) at `backup_poll_interval_seconds`. No
  cron, no external host, no separate key to manage — reuses the same SSH
  connection as telemetry/commands.
- **Versioned** — every pulled archive is hashed (sha256) server-side; a
  new row (`device_backups` table) is only ever inserted when the content
  actually *changed* since the last stored version for that device. An
  unchanged config on every 6-hourly check is a no-op, not a new row - so
  "versions" line up with real config changes, not poll attempts.
- **History management** — `backup_retention_count` (default 30, editable
  from the dashboard's System Settings section) caps how many historical
  versions are kept per device; the oldest are pruned once a new version
  actually lands. `0` keeps every version forever.
- **Visibility** — the dashboard's "Config Backups" section lists every
  stored version (time, device, size, hash) with a direct download link
  per row.

Requires `tar` on the device in addition to the tools the rest of the
script already uses (`jsonfilter`, `ubus`, `uci`) - a standard BusyBox
applet present on stock OpenWrt firmware. If it's missing, `wifi-agent.sh
backup` exits non-zero and `poll_backups` logs a warning and skips that
device for the tick, without affecting telemetry or command handling.

### Restoring a backup

The dashboard's "Config Backups" table has a **Restore** link on every
row (`POST /api/backups/{id}/restore`) — it pushes that archive back onto
the device over the existing SSH connection, extracts it over `/`, and
restarts networking, no manual scp/ssh required. Usable any time the
device is reachable, not just during initial provisioning — e.g. "this
device got reset or swapped for a new one, put the last known-good config
back on it." It still asks for confirmation first, since a wholesale
config restore plus a service restart is high-risk enough to not want an
accidental click.

To restore by hand instead (useful when troubleshooting, or restoring
onto a device the server can't currently reach over SSH), same idea as
`hobo_cams-main`'s `scripts/restore.sh`, just sourced from the server
instead of a local dated folder:

1. Find the version you want in the dashboard's "Config Backups" table
   and click Download (or `curl` the same URL:
   `GET /api/backups/{id}/download?token=...`).
2. Copy it to the device and extract over `/`:
   ```sh
   scp backup.tar.gz root@<device-ip>:/tmp/restore.tar.gz
   ssh root@<device-ip> "tar -xzf /tmp/restore.tar.gz -C / && rm /tmp/restore.tar.gz"
   ```
3. Restart the affected services (or just reboot, simplest for a full
   config restore):
   ```sh
   ssh root@<device-ip> "/etc/init.d/network restart && /etc/init.d/cron restart"
   ```

## Validating the full loop before writing real rules

The optimizer doesn't issue commands yet (see Known Gaps) — so to prove
the telemetry → command → apply → rollback/ack path actually works
end-to-end, inject a test command by hand rather than waiting for real
rule logic:

1. Confirm telemetry is landing:
   `docker compose exec timescaledb psql -U wifioptimizer -c "select * from devices;"`
   — both AP and STA should show up with a recent `last_seen` within
   ~30-60s of `docker compose up` (the server SSHes in and pulls their
   first `collect` on its own, nothing to start on the devices).
2. Grab the AP's device id from that query, then insert a deliberately
   *safe* test command (a channel it's already on, so there's nothing to
   actually break) to prove the mechanics:
   ```sql
   insert into commands (device_id, param, target_value, ttl_seconds)
   values ('<ap-device-id>', 'halow_operating_freq', '{"channel": 8}', 120);
   ```
3. Within `command_poll_interval_seconds` (dashboard's System Settings
   section) the server should SSH in and
   apply it (`wifi-agent.sh apply`), which calls `ubus call uci apply`
   and `network.wireless reconf` — and, once
   `command_verify_delay_seconds` has passed, SSH back in to run
   `verify-confirm`, which calls `uci confirm` and reports back
   `"acked"`. Check the `commands` table again; `status` should move
   `pending` → `applied` → `acked`.
4. Then try a real (but still recoverable) channel change to prove the
   rollback side works too — same insert with a different valid channel,
   and confirm the STA reassociates (typically well under a minute).

Only once this is proven, move on to real channel-plan/scan logic (see
Known Gaps) so the optimizer can issue these commands itself instead of
hand-inserting them.

## Verified against real hardware

Confirmed live via SSH against an HT-HD01-V2 AP/STA pair running OpenWrt
23.05.5 / vendor firmware 2.8.5-20250924:

- **The dashboard's browser `fetch()` calls (`set_optimizer_state`,
  `set_optimizer_settings`) send their JSON string body with no explicit
  `Content-Type` header, so the browser defaults to `text/plain`.**
  FastAPI's automatic pydantic-body parsing (`async def endpoint(report:
  SomeModel)`) rejects this even though the JSON payload itself arrives
  byte-for-byte intact — confirmed via a local FastAPI instance that the
  exact same payload succeeds with `Content-Type: application/json` and
  fails with this one, producing a `model_attributes_type` / "Input should
  be a valid dictionary" 422 with the whole payload double-quoted in
  `input`. Those two POST endpoints parse the raw body themselves via
  `model.model_validate_json(await request.body())` instead, which is
  content-type-agnostic. (This used to also apply to the device-facing
  push endpoints from the old HTTP-based agent design — those endpoints
  no longer exist; see "Reaching the devices over SSH".)

- HaLow radio interface: `wlan0` on both AP and STA (phy1, Morse Micro
  MM6108A1). The 2.4GHz radio interface name isn't guaranteed the same on
  every unit/role (e.g. `phy0-ap0` was seen on one STA) — the script looks
  it up dynamically via `ubus call network.wireless status` rather than
  hardcoding it.
- `collect_halow()` / `collect_wifi24()` use `ubus call iwinfo info` /
  `assoclist` (clean JSON) plus `ubus call rangetest morse_cli_channel` for
  exact HaLow bandwidth. Live-tested on both devices — confirmed producing
  correct output (e.g. one AP saw `rssi:-1, noise:-76, mcs:7,
  rate_mbps:15.48, channel:8, bandwidth_mhz:4`).
- The device's retry/packet counters are cumulative since boot, not a live
  rate. This originally became a per-interval fraction on-device
  (`delta_rate()` in the old continuously-running agent); now that
  `collect` is a stateless one-shot SSH invocation with nothing to persist
  state between polls on-device, the raw cumulative counters travel to
  the server as-is and the delta is computed server-side instead (see
  `_upsert_radio_counters` in `app/main.py` and migration 009). This was a
  real bug caught by testing against real hardware, back when the delta
  was still computed on-device: the schema originally had `retries` as
  `INTEGER`; it's `DOUBLE PRECISION` now, since it's a fraction.
- `apply_halow_operating_freq()` sets `wireless.radio1.channel` — HaLow
  bandwidth turned out to be implied by the channel index itself (no
  separate uci width option exists; LuCI's "Width" field in the vendor web
  UI is sugar over a custom widget that just picks a channel number from a
  bandwidth-aware channel plan).
- Safe-apply uses OpenWrt's native `ubus call uci apply {rollback, timeout}`
  + `uci confirm`/`rollback` — the same mechanism behind LuCI's own "Save &
  Apply" countdown — instead of a hand-rolled config snapshot. **Confirmed
  live this is not sufficient on its own**: it does not reliably push a
  wireless change to the radio - an explicit `ubus call network.wireless
  reconf` is required afterward, and even then an invalid channel/bandwidth
  combination is silently ignored by the driver rather than erroring. The
  `cmd_verify_confirm()` (the `verify-confirm` subcommand) now reads the
  live channel back and compares it to the target before ever calling
  `uci confirm`; a mismatch reports `reverted` immediately with a real
  reason instead of relying on a generic ttl timeout. A live test (channel
  8→12 while at 4MHz bandwidth) reproduced exactly this failure mode and
  confirmed the fix catches it correctly, with the radio never actually
  disrupted either time.
- That verification originally only checked the AP's *own* radio state,
  which proves nothing about whether the STA (the actual peer on this P2P
  bridge) reassociated - the AP's path to the server is Ethernet, entirely
  independent of HaLow, so "the AP is still reachable" was never a
  meaningful signal about link health. `cmd_verify_confirm()` (the
  `verify-confirm` subcommand) now also requires at least one associated
  peer in `iwinfo assoclist` for
  `halow_operating_freq` changes specifically (not for `wifi24_channel` -
  downstream 2.4GHz clients are legitimately transient, so "zero clients right
  now" doesn't mean a change failed the way "zero HaLow peers" does on a
  link that's supposed to always have exactly one).
- The real US HaLow channel-plan (`app/halow_channel_plan.py`) is sourced
  directly from `/usr/share/morse-regdb/channels.csv` on the device
  (package `morse-regdb`, confirmed live) - not guessed, not derived from
  public docs. Channel numbering is bandwidth-dependent and the valid
  channel sets per bandwidth don't overlap (e.g. channel 12 is only valid
  at 8MHz; at 4MHz the valid set is 8/16/24/32/40/48) - this is exactly
  what caused the silent-failure bug above.
- **Confirmed live in production (not a bench artifact): narrowing to 2MHz
  never actually applies on this hardware.** With genuinely zero real
  traffic on the bench, the optimizer's narrow-bandwidth logic correctly
  fired (`sustained low utilization (0.00 over 1440m)`) and tried to move
  4MHz → 2MHz, channel 2 (the first/simplest valid channel at 2MHz). The
  `verify-confirm` subcommand correctly caught that the radio never
  actually got there and reverted - but because channel selection for
  widen/narrow was otherwise deterministic (always the same first valid
  channel), it retried the identical, never-working target every single
  cooldown period (6h) indefinitely. Fixed by tracking channels that have
  ever reverted for a given device+param and skipping them in every
  channel-picking path (widen/narrow *and* the degradation round-robin,
  which had the same latent bug: once a cycled-to channel reverts,
  `cur_channel` never advances, so the "next" channel computed from it is
  the same failed one every time). See `_reverted_channels()` in
  `app/optimizer.py`. This blacklist is permanent and has no expiry - if
  every channel at a bandwidth eventually fails, the optimizer logs a
  warning and stops attempting that bandwidth rather than looping forever;
  clearing it requires manual DB intervention (or investigating why that
  channel/bandwidth doesn't actually work on this hardware/regulatory
  setup).

## Known gaps (see task list, not oversights)

- The optimizer's HaLow channel selection (`app/optimizer.py`) is
  deliberately simple, not scan-informed: real channel-scan telemetry
  isn't available (`iwinfo scan` returns empty on this driver, confirmed
  live), so rather than invent a scoring heuristic on data that doesn't
  exist, it just cycles to the next valid channel *at the same bandwidth*
  in a fixed round-robin order when degradation is sustained past
  cooldown. That's a real, working decision, just not a smart one - good
  enough to start accumulating real before/after data, which a smarter
  strategy would need anyway.
- 2.4GHz (STA-side) channel selection now works the same way: sustained
  per-client retry degradation (`radio_clients.retries`, computed the same
  delta-based way as HaLow) cycles through the standard non-overlapping
  channels (1/6/11), round-robin.
- Bandwidth (widen/narrow) decisions are implemented, but only evaluated
  when the link is otherwise healthy (degradation always takes priority -
  widening/narrowing a struggling link seems more likely to make things
  worse than better). Utilization is real `throughput_mbps` (a new byte-
  counter-delta metric, distinct from the PHY `rate_mbps` already
  tracked) against the currently negotiated PHY rate - not a fixed
  theoretical capacity table. Widen/narrow use much longer sustain windows
  than a same-bandwidth channel cycle (60min / 24h respectively) since
  they're more disruptive changes. **The specific thresholds (70%/10%
  utilization, 60min/24h windows) are reasonable-sounding defaults, not
  empirically validated against real traffic** - there's no meaningful
  downstream load on the bench to tune against yet. They're now editable
  from the dashboard's Optimizer Settings section (`optimizer_state`
  table) without a rebuild, so revisit them once real usage data exists
  rather than editing `config.py` and redeploying. The channel picked for
  the new bandwidth is also the simplest possible choice (first valid
  channel), not frequency-proximity aware.
- TX power is intentionally not a lever (no battery/power constraint on
  this particular system — remove this line if that doesn't apply to you).
- `telemetry_retention_days` (default **0 = keep forever**, editable from
  the dashboard's System Settings section) is applied as a
  TimescaleDB retention policy both at startup and immediately whenever
  it's changed from the dashboard. Setting it to a positive number
  uses `if_not_exists => true`, so it's safe to call every time - but that
  also means changing a *nonzero* value on a deployment that already has
  the policy won't take effect on its own; you'd need to remove the old
  one by hand first:
  `docker compose exec timescaledb psql -U wifioptimizer -d wifioptimizer -c "SELECT remove_retention_policy('telemetry'); SELECT remove_retention_policy('radio_clients');"`
  then save the new value from System Settings again.
  Setting it *back to 0*, though, is handled automatically - the app
  removes any existing policy whenever the value is 0, so you don't
  need the manual step in that direction.
