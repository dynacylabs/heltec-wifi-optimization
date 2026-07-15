# heltec-wifi-optimization

Central optimization server for a Heltec HT-HD01-V2 Wi-Fi HaLow point-to-point
bridge (one AP, one STA). Ingests telemetry from both devices, runs a
rule-based optimizer, and pushes config changes back down with a
self-reverting safety mechanism that lives on the device, not the server —
so a bad change can't permanently sever the link.

## Requirements

- Two Heltec HT-HD01-V2 dongles, factory reset, paired, firmware 2.8.5 or
  later (OpenWrt-based), one in AP mode and one in STA mode, both reachable
  over Ethernet from wherever you run this server.
- Docker + Docker Compose on the server.
- SSH (root) access to both Heltec devices, for deploying the agent script.

## Deploying the server

Credentials live directly in `docker-compose.yml` (no `.env` file) —
**edit the `changeme` values before deploying, and if you keep this repo
public, do not commit your real values.** Either keep them as a local,
uncommitted edit, or maintain them in a private fork/branch.

1. Clone this repo wherever you want to run it.
2. Edit `docker-compose.yml` and replace every `changeme` with a real
   password (`POSTGRES_PASSWORD` appears in three places and must match
   across all of them; `GF_SECURITY_ADMIN_PASSWORD` is independent).
3. `docker compose up -d --build`
   - First boot runs `db/migrations/001_init.sql` automatically (Postgres
     only runs `/docker-entrypoint-initdb.d` on an empty data volume).
4. Check `http://<host>:8080/health` returns `{"status": "ok"}`.
5. Grafana at `http://<host>:3000` (login `admin` / whatever you set for
   `GF_SECURITY_ADMIN_PASSWORD`) — the TimescaleDB datasource and a starter
   "HoboCams Overview" dashboard are already provisioned.

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

1. Confirm telemetry is landing:
   `docker compose exec timescaledb psql -U hobocams -c "select * from devices;"`
   — both AP and STA should show up with a recent `last_seen` within
   ~30-60s of starting their agents.
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
   and confirm the STA reassociates (typically well under a minute).

Only once this is proven, move on to real channel-plan/scan logic (see
Known Gaps) so the optimizer can issue these commands itself instead of
hand-inserting them.

## Verified against real hardware

Confirmed live via SSH against an HT-HD01-V2 AP/STA pair running OpenWrt
23.05.5 / vendor firmware 2.8.5-20250924:

- HaLow radio interface: `wlan0` on both AP and STA (phy1, Morse Micro
  MM6108A1). The 2.4GHz radio interface name isn't guaranteed the same on
  every unit/role (e.g. `phy0-ap0` was seen on one STA) — the agent looks
  it up dynamically via `ubus call network.wireless status` rather than
  hardcoding it.
- No `curl` on this firmware, only busybox `wget` (supports `--post-data`,
  no `--header` — fine, since FastAPI doesn't require Content-Type to parse
  a JSON body).
- `collect_halow()` / `collect_wifi24()` use `ubus call iwinfo info` /
  `assoclist` (clean JSON) plus `ubus call rangetest morse_cli_channel` for
  exact HaLow bandwidth. Live-tested on both devices — confirmed producing
  correct output (e.g. one AP saw `rssi:-1, noise:-76, mcs:7,
  rate_mbps:15.48, retries:0.062, channel:8, bandwidth_mhz:4`).
- The device's retry/packet counters are cumulative since boot, not a live
  rate — `delta_rate()` in the agent computes the actual per-interval
  fraction between polls. This was a real bug caught by testing against
  real hardware: the schema originally had `retries` as `INTEGER`; it's
  `DOUBLE PRECISION` now, since it's a fraction.
- `apply_halow_operating_freq()` sets `wireless.radio1.channel` — HaLow
  bandwidth turned out to be implied by the channel index itself (no
  separate uci width option exists; LuCI's "Width" field in the vendor web
  UI is sugar over a custom widget that just picks a channel number from a
  bandwidth-aware channel plan).
- Safe-apply uses OpenWrt's native `ubus call uci apply {rollback, timeout}`
  + `uci confirm`/`rollback` — the same mechanism behind LuCI's own "Save &
  Apply" countdown — instead of a hand-rolled config snapshot.

## Known gaps (see task list, not oversights)

- Real channel-scan telemetry isn't available: `iwinfo scan` on the HaLow
  device returned an empty result set live. The optimizer still only
  detects/logs sustained degradation rather than auto-selecting a channel.
  Also unresolved: the actual channel-plan mapping (which channel indices
  correspond to which bandwidths) needed to translate "widen to N MHz"
  into a concrete channel number.
- The `uci apply` rollback mechanism itself hasn't been live-tested end to
  end by the agent specifically (as opposed to a manual channel change via
  the vendor web UI, which has been tested extensively).
- TX power is intentionally not a lever (no battery/power constraint on
  this particular system — remove this line if that doesn't apply to you).
