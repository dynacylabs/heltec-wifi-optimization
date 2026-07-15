# hobo-cams-brain

Central optimization server for the Hobo Cams HaLow bridge (AP `192.168.2.2` /
STA `192.168.2.3`). Ingests telemetry from both Heltec HT-HD01-V2 devices,
runs a rule-based optimizer, and pushes config changes back down with a
self-reverting safety mechanism that lives on the device, not the server.

## Deploying the server (on masha)

masha deploys everything as a **Portainer stack**, not raw `docker compose`
CLI, with data under `/Volumes/raid_usb/mounts/` so restic backs it up
nightly — this project follows that convention, not a generic
docker-compose layout. (Also intentionally staying off NPM/Authelia for
now — internal tool, reachable directly by host port on the LAN, not a
public subdomain. Revisit if it ever needs family-facing access.)

1. **Clone the repo** into the mounts dir (same pattern as
   `blinkbridge`/`jumpbox` - code + config living together). `git clone`
   requires the target to be empty, so this must happen *before* creating
   any data dirs inside it:
   ```bash
   git clone git@github.com:dynacylabs/heltec-wifi-optimization.git \
     /Volumes/raid_usb/mounts/heltec-wifi-optimization
   ```
2. **Bind mount dirs** (Checklist step 1 in MASHA.md) - created as
   siblings of the cloned repo's files, after cloning:
   ```bash
   mkdir -p /Volumes/raid_usb/mounts/heltec-wifi-optimization/pgdata
   mkdir -p /Volumes/raid_usb/mounts/heltec-wifi-optimization/grafana-data
   ```
3. **Build the app image** (locally built, like Blinkbridge/jumpbox/resume
   — Portainer stacks here reference a pre-built tag, not a `build:`
   context):
   ```bash
   cd /Volumes/raid_usb/mounts/heltec-wifi-optimization/app
   docker build -t hobocams-brain-app:latest .
   ```
4. **Deploy via Portainer:** Stacks → Add Stack → name `hobocams` → paste
   the contents of `docker-compose.yml` → fill in `POSTGRES_PASSWORD` and
   `GRAFANA_ADMIN_PASSWORD` as environment variables in the Portainer UI
   (or use `.env.example` as a reference for what's needed) → Deploy.
5. Check `http://masha:8080/health` returns `{"status": "ok"}`.
6. Grafana at `http://masha:3000` (login `admin` / whatever you set for
   `GRAFANA_ADMIN_PASSWORD`) — the TimescaleDB datasource and a starter
   "HoboCams Overview" dashboard are already provisioned.

Don't add this to the Running Services table / Authelia / NPM yet per the
MASHA.md new-service checklist — those steps are for services that need a
public subdomain, which this doesn't (yet).

## Deploying the agent (on each Heltec device)

No package installs needed — the script only uses tools already on the
firmware (`wget`, `jsonfilter`, `ubus`, `uci`).

1. Copy `agent/hobocams-agent.sh` to `/usr/bin/hobocams-agent.sh` on the
   device, `chmod +x` it.
2. Copy `agent/hobocams-agent.init` to `/etc/init.d/hobocams-agent`,
   `chmod +x` it.
3. Copy `agent/hobocams-agent.conf.example` to `/etc/hobocams-agent.conf`
   and edit `HOBOCAMS_ROLE` (`AP` or `STA`) and `HOBOCAMS_SERVER_URL` for
   that specific device.
4. `/etc/init.d/hobocams-agent enable && /etc/init.d/hobocams-agent start`

## Validating the full loop before writing real rules

The optimizer doesn't issue commands yet (see Known Gaps) — so to prove
the telemetry → command → apply → rollback/ack path actually works
end-to-end, inject a test command by hand rather than waiting for real
rule logic:

1. Confirm telemetry is landing: `docker exec -it hobocams-timescaledb
   psql -U hobocams -c "select * from devices;"` — both AP and STA should
   show up with a recent `last_seen` within ~30-60s of starting their
   agents.
2. Grab the AP's device id from that query, then insert a deliberately
   *safe* test command (a channel it's already on, so there's nothing to
   actually break) to prove the mechanics:
   ```sql
   insert into commands (device_id, param, target_value, ttl_seconds)
   values ('<ap-device-id>', 'halow_operating_freq', '{"channel": 8}', 120);
   ```
3. Within ~30s the agent should poll it up, run `apply_halow_operating_freq`,
   call `ubus call uci apply`, and — since 20s later it can still reach the
   server — call `uci confirm` and report `"acked"` back. Check the
   `commands` table again; `status` should move `pending` → `applied` →
   `acked`.
4. Then try a real (but still recoverable) channel change to prove the
   rollback side works too — same insert with a different valid channel,
   and confirm the STA reassociates within its usual ~30s.

Only once this is proven, move on to Task #13 (real channel-plan/scan
logic) so the optimizer can issue these commands itself instead of us
hand-inserting them.

## Verified against real hardware (2026-07-15, SSH on 192.168.2.2 / .3)

- HaLow radio interface: `wlan0` on both AP and STA (phy1, Morse Micro
  MM6108A1). STA's 2.4GHz radio interface: `phy0-ap0` (phy0, MediaTek
  MT7628) — looked up dynamically via `ubus call network.wireless status`,
  not hardcoded.
- No `curl` on this firmware, only busybox `wget` (supports `--post-data`,
  no `--header` — fine, since FastAPI doesn't require Content-Type to parse
  a JSON body).
- `collect_halow()` / `collect_wifi24()` use `ubus call iwinfo info` /
  `assoclist` (clean JSON) plus `ubus call rangetest morse_cli_channel` for
  exact HaLow bandwidth. Live-tested on both devices — confirmed producing
  correct output (e.g. AP saw `rssi:-1, noise:-76, mcs:7, rate_mbps:15.48,
  retries:0.062, channel:8, bandwidth_mhz:4`).
- The device's retry/packet counters are cumulative since boot, not a live
  rate — `delta_rate()` computes the actual per-interval fraction between
  polls. This was a real bug in the original schema (`retries` was
  `INTEGER`, now `DOUBLE PRECISION`) caught by testing against the device.
- `apply_halow_operating_freq()` sets `wireless.radio1.channel` — HaLow
  bandwidth turned out to be implied by the channel index itself (no
  separate uci width option exists; LuCI's "Width" field is UI sugar over
  a custom widget that just picks a channel number from a bandwidth-aware
  channel plan).
- Safe-apply uses OpenWrt's native `ubus call uci apply {rollback, timeout}`
  + `uci confirm`/`rollback` — the same mechanism behind LuCI's own "Save &
  Apply" countdown — instead of a hand-rolled config snapshot.

## Known gaps (see task list, not oversights)

- Real channel-scan telemetry isn't available: `iwinfo scan` on the HaLow
  device returned an empty result set live. The optimizer still only
  detects/logs sustained degradation rather than auto-selecting a channel —
  see Task "Research HaLow channel-plan-to-bandwidth mapping."
- The `uci apply` rollback mechanism itself hasn't been live-tested end to
  end by our agent specifically (as opposed to a manual channel change via
  LuCI, which has been tested extensively) — see Task "Live-test the uci
  apply rollback safe-apply mechanism."
- TX power is intentionally not a lever (no battery/power constraint on
  this system, per project scope).
