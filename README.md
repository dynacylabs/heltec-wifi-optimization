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
   value (`POSTGRES_PASSWORD` appears in three places and must match
   across all of them; `GF_SECURITY_ADMIN_PASSWORD` is independent).
   Also set `API_TOKEN` on the `app` service — this is the shared secret
   every device-facing endpoint requires (see "API authentication"
   below) — and use the *same* value when configuring
   `HOBOCAMS_API_TOKEN` in each device's `/etc/hobocams-agent.conf`.
3. `docker compose up -d --build`
   - First boot runs `db/migrations/001_init.sql` automatically (Postgres
     only runs `/docker-entrypoint-initdb.d` on an empty data volume).
4. Check `http://<host>:8080/health` returns `{"status": "ok"}`.
5. Grafana at `http://<host>:3000` (login `admin` / whatever you set for
   `GF_SECURITY_ADMIN_PASSWORD`) — the TimescaleDB datasource and a starter
   "HoboCams Overview" dashboard are already provisioned. Optional — see
   the built-in status page below for day-to-day troubleshooting instead.

## Status page

`http://<host>:8080/` (redirects to `/dashboard`) is a self-contained
status page (no Grafana knowledge required) served directly by the app:

- **Live status cards** — online/offline per device, current HaLow
  channel/bandwidth/RSSI/noise/rate/retries, current 2.4GHz channel and
  client count, and uptime % over whichever time range is selected below
  (computed via gap analysis over the telemetry heartbeat, bookended at
  the window edges so an outage right at the start or still ongoing "now"
  both count).
- **HaLow history charts** — RSSI/noise, retries, bandwidth — over a
  selectable time range (1h/6h/24h/7d).
- **2.4GHz history charts** — channel over time, and per-client RSSI (one
  line per connected device on the STA's downstream SSID, e.g. Blink Sync
  Module, Shelly relay) — a client with no line for a stretch was
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

The API token is injected server-side when rendering the page — no
prompt, nothing stored in the browser. Access control for a human is
expected to happen at the reverse proxy (Authelia or similar) in front of
whichever domain you point at this page; the token itself stays required
on every backing endpoint (`/api/status`, `/api/telemetry/{mac}`,
`/api/radio-clients/{mac}`, `/api/commands`) as defense in depth, since
this same container may also be reachable through a domain that isn't
behind your auth layer (e.g. the device-facing API domain, if you're
exposing that separately - see "API authentication" below).

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

## API authentication

Every device-facing endpoint (`/telemetry`, `/commands/{mac}`,
`/commands/{id}/report`) requires a `?token=` query parameter matching the
server's `API_TOKEN` — a query param rather than an `Authorization` header,
since the agent only has busybox `wget` on the device side, which has no
`--header` support. `/health` is intentionally left open for plain
uptime checks.

This matters most if you put this server behind a reverse proxy reachable
from the internet (e.g. because your Heltec devices sit on a network
segment that can only reach the internet, not your LAN directly — in which
case a public subdomain is the only path back to the server). In that
case, don't put this endpoint behind an SSO/forward-auth layer (it has no
login flow, it's a plain machine-to-machine API) — the token is the only
protection, so treat it like a real secret.

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

- **The agent's `wget` (`uclient-fetch`) always sends
  `Content-Type: application/x-www-form-urlencoded` for `--post-data`, with
  no way to override it (no `--header` support).** FastAPI's automatic
  pydantic-body parsing (`async def endpoint(report: SomeModel)`) rejects
  this even though the JSON payload itself arrives byte-for-byte intact —
  confirmed via a local FastAPI instance that the exact same payload
  succeeds with `Content-Type: application/json` and fails with this one,
  producing a `model_attributes_type` / "Input should be a valid dictionary"
  422 with the whole payload double-quoted in `input`. Every POST endpoint
  here parses the raw body itself via `model.model_validate_json(await
  request.body())` instead, which is content-type-agnostic. If you see this
  exact error shape from a client that isn't a browser/curl, check its
  Content-Type header first.

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
  Apply" countdown — instead of a hand-rolled config snapshot. **Confirmed
  live this is not sufficient on its own**: it does not reliably push a
  wireless change to the radio - an explicit `ubus call network.wireless
  reconf` is required afterward, and even then an invalid channel/bandwidth
  combination is silently ignored by the driver rather than erroring. The
  agent now reads the live channel back and compares it to the target
  before ever calling `uci confirm`; a mismatch reports `reverted`
  immediately with a real reason instead of relying on a generic ttl
  timeout. A live test (channel 8→12 while at 4MHz bandwidth) reproduced
  exactly this failure mode and confirmed the fix catches it correctly,
  with the radio never actually disrupted either time.
- That verification originally only checked the AP's *own* radio state,
  which proves nothing about whether the STA (the actual peer on this P2P
  bridge) reassociated - the AP's path to the server is Ethernet, entirely
  independent of HaLow, so "the AP is still reachable" was never a
  meaningful signal about link health. `verify_command_applied()` now
  also requires at least one associated peer in `iwinfo assoclist` for
  `halow_operating_freq` changes specifically (not for `wifi24_channel` -
  Blink/Shelly clients are legitimately transient, so "zero clients right
  now" doesn't mean a change failed the way "zero HaLow peers" does on a
  link that's supposed to always have exactly one).
- The real US HaLow channel-plan (`app/halow_channel_plan.py`) is sourced
  directly from `/usr/share/morse-regdb/channels.csv` on the device
  (package `morse-regdb`, confirmed live) - not guessed, not derived from
  public docs. Channel numbering is bandwidth-dependent and the valid
  channel sets per bandwidth don't overlap (e.g. channel 12 is only valid
  at 8MHz; at 4MHz the valid set is 8/16/24/32/40/48) - this is exactly
  what caused the silent-failure bug above.

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
  Blink/Shelly load on the bench to tune against yet. Revisit once real
  usage data exists. The channel picked for the new bandwidth is also the
  simplest possible choice (first valid channel), not frequency-proximity
  aware.
- TX power is intentionally not a lever (no battery/power constraint on
  this particular system — remove this line if that doesn't apply to you).
