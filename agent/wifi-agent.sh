#!/bin/sh
# wifi-agent.sh - one-shot device-side helper for Heltec HT-HD01-V2,
# invoked over SSH by the server (see app/device_client.py) rather than
# running as a continuous background process. There is no loop, no state
# kept between invocations, and no server URL/token to configure - the
# server initiates every connection (VLAN1 -> VLAN2 is reachable; the
# reverse is not, which is the whole reason this flipped from a
# push/poll-over-HTTP agent to this stateless pull-over-SSH design). See
# README's "Reaching the devices over SSH" for the full rationale.
#
# Deploy under /usr/bin/wifi-agent.sh. The only thing that still needs to
# run automatically on-device (independent of the server ever reaching it)
# is the boot-time radio recovery check - see wifi-agent-boot.init and the
# `recover-radio` subcommand below.
#
# Usage: wifi-agent.sh <collect|apply|verify-confirm|backup|recover-radio> [args...]
#
# Every command/field below was verified live via SSH on the actual AP
# (192.168.2.2) and STA (192.168.2.3), OpenWrt 23.05.5 / firmware
# 2.8.5-20250924:
#   - HaLow radio interface is `wlan0` on both AP and STA (phy1, Morse
#     Micro MM6108A1). The 2.4GHz radio interface is `phy0-ap0` on the STA
#     (phy0, MediaTek MT7628) - looked up dynamically below rather than
#     hardcoded, since naming isn't guaranteed to be identical on the AP if
#     its 2.4GHz radio is ever enabled.
#   - `iwinfo <iface> info` / `iwinfo <iface> assoclist` via ubus return
#     clean JSON (channel, signal, noise, per-client tx/rx incl. mcs,
#     retries, packets, rate in units of 0.001 Mbit/s).
#   - `ubus call rangetest morse_cli_channel` gives exact HaLow bandwidth
#     (bw_mhz) alongside the channel.
#   - HaLow bandwidth is NOT a separate uci option - LuCI's "Width" field is
#     UI sugar over a custom widget (widgets.WifiFrequencyValue) that
#     ultimately just picks a channel index from a bandwidth-aware channel
#     plan. There is no `wireless.radio1.width` to set - only `channel`.
#     Translating "I want N MHz" into the right channel index is the
#     server-side optimizer's job, not this script's.
#   - `iwinfo scan` on the HaLow device returned an empty result set live -
#     confirms real channel-scan telemetry isn't trivially available here,
#     consistent with the optimizer not yet doing scan-based selection.
#   - The device's own retry/packet/byte counters are CUMULATIVE since
#     boot, not an instantaneous rate. Turning that into a per-poll rate
#     used to happen here (a delta against /tmp state from the previous
#     poll) - but a one-shot SSH invocation has nowhere to keep that state
#     between polls, so `collect` now just reports the raw cumulative
#     counters and the server computes the delta itself against the
#     previous poll's counters (see main.py's _upsert_radio_counters and
#     migration 009).
#   - `ubus call uci apply '{"rollback":true,"timeout":N}'` + `uci confirm`/
#     `uci rollback` is OpenWrt's native safe-apply mechanism (same one
#     LuCI's own "Save & Apply" countdown uses) - used here instead of a
#     hand-rolled config snapshot, since it's a tested, built-in feature
#     that survives more failure modes (e.g. a crash mid-window) than a
#     shell-script-level snapshot would. Crucially, this timer runs
#     on-device via procd/rpcd, independent of network reachability - it
#     still protects against a bad change even if the server can no longer
#     SSH back in to confirm or roll back explicitly.
#   - IMPORTANT, confirmed live: `uci apply` alone does NOT reliably push a
#     wireless config change to the radio - an explicit
#     `ubus call network.wireless reconf` is still required afterward.
#   - ALSO confirmed live: an invalid channel/bandwidth combination (HaLow
#     channel numbering is bandwidth-dependent, and the valid combinations
#     aren't fully mapped yet - see Task "Research HaLow channel-plan")
#     fails SILENTLY - the uci config commits fine and reconf returns no
#     error, but the radio just keeps running its last-good state. This is
#     why verify-confirm reads the live value back and compares it to the
#     target before ever calling `uci confirm` - reachability alone (the
#     AP staying up over Ethernet) proves nothing about whether the HaLow
#     radio itself actually changed, since that path doesn't go through
#     HaLow at all.

MAC="$(cat /sys/class/net/eth0/address)"
DEVICE_HOSTNAME="$(uci get system.@system[0].hostname 2>/dev/null)"

# Emit a JSON-safe value: the value itself, or the literal `null`.
jnum() {
    [ -n "$1" ] && echo "$1" || echo null
}

# Mutex around anything that touches the radio module/interface state
# (chip reset, `wifi down/up`, a full network reload) - added 2026-08-11
# after review found nothing stopped the cron-driven recover-radio path
# from running concurrently with a server-driven apply, or two retried
# applies from overlapping if a reload hangs past the server's SSH
# timeout. `mkdir` is used as the atomic primitive (not `flock`, which
# isn't guaranteed present in every busybox build) - a directory create
# either succeeds or fails atomically, no extra tooling required. Age is
# tracked in a file inside the lock dir (rather than relying on `stat`/
# `date -r`, whose flags vary across busybox builds) so a lock left behind
# by a killed/crashed process doesn't wedge every future radio operation
# forever.
RADIO_LOCK_DIR="/tmp/wifi-agent-radio.lock"
RADIO_LOCK_MAX_AGE_SECONDS=300

acquire_radio_lock() {
    attempt=0
    while ! mkdir "$RADIO_LOCK_DIR" 2>/dev/null; do
        lock_started="$(cat "$RADIO_LOCK_DIR/started_at" 2>/dev/null)"
        now="$(date +%s)"
        if [ -n "$lock_started" ] && [ $((now - lock_started)) -gt "$RADIO_LOCK_MAX_AGE_SECONDS" ]; then
            logger -t wifi-agent "radio lock is >${RADIO_LOCK_MAX_AGE_SECONDS}s old - assuming its owner died without releasing it, reclaiming"
            rm -rf "$RADIO_LOCK_DIR"
            continue
        fi
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 30 ]; then
            logger -t wifi-agent "could not acquire radio lock after 60s - another radio operation is in progress, aborting rather than risk interleaving with it"
            return 1
        fi
        sleep 2
    done
    date +%s > "$RADIO_LOCK_DIR/started_at" 2>/dev/null
    trap 'release_radio_lock' EXIT
    return 0
}

release_radio_lock() {
    rm -rf "$RADIO_LOCK_DIR" 2>/dev/null
}

# Unloads and reloads the morse kernel module with modules.d's own
# per-device parameters, actually verifying each step rather than trusting
# it - added 2026-08-11 after confirming live that the previous version
# (bare `rmmod ... 2>/dev/null; modprobe ... 2>/dev/null`, neither checked)
# could fail completely silently: if rmmod can't unload the module because
# something still references it right after `wifi down` (the interface
# not fully released yet), the modprobe that follows is a total no-op - a
# kernel does not reload an already-loaded module's parameters just
# because modprobe is invoked again. The result was the radio running
# indefinitely on the chip's compiled-in defaults (country reverting to
# its factory AU, macaddr_suffix and bcf blank) while every log line
# upstream still claimed the reload had happened. Returns 1 if the
# parameters demonstrably did not take, so the caller can tell a real
# reload apart from one that silently no-opped.
reload_morse_module() {
    unload_attempt=1
    while lsmod | grep -q '^morse '; do
        if [ "$unload_attempt" -gt 3 ]; then
            logger -t wifi-agent "reload_morse_module: morse module still loaded after 3 plain rmmod attempts - force-unloading"
            rmmod -f morse dot11ah 2>/dev/null
            break
        fi
        rmmod morse dot11ah 2>/dev/null
        sleep 1
        unload_attempt=$((unload_attempt + 1))
    done

    morse_params="$(sed -n 's/^morse //p' /etc/modules.d/morse)"
    # shellcheck disable=SC2086
    modprobe morse $morse_params 2>/dev/null
    sleep 1

    expected_country="$(echo "$morse_params" | sed -n 's/.*country=\([^ ]*\).*/\1/p')"
    live_country="$(cat /sys/module/morse/parameters/country 2>/dev/null)"
    if [ -n "$expected_country" ] && [ "$live_country" != "$expected_country" ]; then
        logger -t wifi-agent "reload_morse_module: module loaded but country=${live_country:-unknown}, expected $expected_country - parameters did not take"
        return 1
    fi
    return 0
}

halow_ifname() {
    ubus call network.wireless status 2>/dev/null | jsonfilter -e '@.radio1.interfaces[0].ifname' 2>/dev/null
}

# True only when the radio driver itself is actually up - distinct from
# whether an ifname/channel/association exists, since a wedged chip (see
# verify_and_recover_radio) can leave radio1 reporting up=false with
# retry_setup_failed=true while SSH/the rest of the device is fine. This is
# what lets the server tell "device unreachable" apart from "device is
# fine, radio is not" - see cmd_collect and main.py's poll_telemetry.
halow_radio_up() {
    status="$(ubus call network.wireless status 2>/dev/null)"
    up="$(echo "$status" | jsonfilter -e '@.radio1.up' 2>/dev/null)"
    retry_failed="$(echo "$status" | jsonfilter -e '@.radio1.retry_setup_failed' 2>/dev/null)"
    if [ "$up" = "true" ] && [ "$retry_failed" != "true" ]; then
        echo "true"
    else
        echo "false"
    fi
}

wifi24_ifname() {
    ubus call network.wireless status 2>/dev/null | jsonfilter -e '@.radio0.interfaces[0].ifname' 2>/dev/null
}

# Confirmed live (2026-07): after a cold boot or a full `wifi down`/`wifi up`
# restart, this specific HaLow radio can come up on a fallback 1MHz channel
# (seen: 11, then 7) instead of the channel configured in uci - hostapd logs
# `Command 'morse_cli ... channel ...' failed with error code -1` /
# `morse_cmd_vendor_set_channel ... failed with rc -1` at the kernel/SDIO
# level. A LIVE reconf (while already up) reliably applies a channel change,
# but cold bring-up does not reliably reach the configured one. Recovery
# that worked live: `wifi down radio1`, a hard chip reset via the vendor's
# own MM_RESET GPIO script (this actually power-cycles the Morse Micro
# module, not just the kernel driver - a plain reboot alone was NOT
# sufficient, confirmed across multiple attempts), then `wifi up radio1`
# followed by a live channel reconf. Run once at device boot (see
# wifi-agent-boot.init) so a power interruption in the field - expected on
# solar/battery - doesn't strand the link on a channel the peer isn't
# listening on.
verify_and_recover_radio() {
    # Serialize against any other radio-touching operation (a server-driven
    # apply, or another overlapping invocation of this same function) -
    # see acquire_radio_lock's comment. Bailing out here (rather than
    # blocking indefinitely) is deliberate: if something else is already
    # mid-operation, piling this on top of it is how today's corruption
    # happened, not a risk worth taking to avoid skipping one 15-minute
    # cron tick.
    acquire_radio_lock || return 1

    # Survives across reboots (unlike /tmp, which is tmpfs) so the escalation
    # below can tell "first time trying a reboot" apart from "already tried
    # rebooting and it didn't help" - caps how many times this will reboot
    # the device on its own so a genuinely persistent hardware fault doesn't
    # turn into an infinite reboot loop. Cleared the moment the radio is
    # next confirmed healthy, by any invocation (boot or the periodic cron -
    # see wifi-agent-boot.init).
    reboot_count_file="/etc/wifi-agent-reboot-count"

    # At boot, radio1's interface may not have registered yet - wait for it
    # rather than silently skipping the check (confirmed live: skipping
    # here means a genuinely wrong channel could go completely unnoticed
    # and unrecovered).
    wait_attempt=1
    ifname=""
    while [ "$wait_attempt" -le 6 ]; do
        ifname="$(halow_ifname)"
        [ -n "$ifname" ] && break
        sleep 5
        wait_attempt=$((wait_attempt + 1))
    done
    if [ -z "$ifname" ]; then
        logger -t wifi-agent "verify_and_recover_radio: HaLow ifname never appeared after 30s, giving up"
        return 1
    fi
    expected_channel="$(uci get wireless.radio1.channel 2>/dev/null)"
    [ -z "$expected_channel" ] && { echo "verify_and_recover_radio: no configured channel in uci, skipping" >&2; return 1; }

    attempt=1
    while [ "$attempt" -le 3 ]; do
        live_channel="$(ubus call iwinfo info "{\"device\":\"$ifname\"}" 2>/dev/null | jsonfilter -e '@.channel' 2>/dev/null)"
        if [ "$live_channel" = "$expected_channel" ]; then
            logger -t wifi-agent "HaLow radio confirmed on configured channel $expected_channel"
            rm -f "$reboot_count_file"
            return 0
        fi
        logger -t wifi-agent "HaLow radio on channel ${live_channel:-none}, expected $expected_channel (recovery attempt $attempt/3) - hard-resetting chip"
        wifi down radio1 2>/dev/null
        sleep 2
        [ -x /morse/scripts/chipreset.sh ] && sh /morse/scripts/chipreset.sh 2>/dev/null
        sleep 3
        # Confirmed live (2026-08-03): chipreset.sh's SDIO unbind/bind can
        # trigger the kernel's own module auto-load before this script
        # regains control, using the morse driver's compiled-in defaults -
        # /etc/modules.d/morse (and any parameter overrides in it, e.g.
        # the enable_auto_duty_cycle=N experiment tracking the AP's
        # spontaneous ECSA channel-switch issue) is only consulted by
        # kmodloader at actual system boot, not on a udev-triggered SDIO
        # bind. Force an explicit reload here with modules.d's own
        # per-device params (read fresh rather than duplicated here -
        # bcf/macaddr_suffix differ between the AP and STA) so a
        # mid-operation recovery cycle can't silently revert an override
        # back to the compiled-in default.
        #
        # CRITICAL, added 2026-08-11 after review: confirmed live that
        # this reload can fail *silently* and still leave the radio
        # running on compiled-in defaults (country reverting to the
        # chip's factory AU, macaddr_suffix and bcf blank) - the original
        # version below neither checked rmmod's exit status nor verified
        # modprobe's params actually took, so a busy module (still
        # referenced right after `wifi down`, before it's actually
        # released) made rmmod fail, which made the modprobe that
        # followed it a complete no-op - a kernel won't reload an
        # already-loaded module's parameters just because modprobe is
        # called again. reload_morse_module() below verifies each step
        # instead of trusting it.
        if reload_morse_module; then
            logger -t wifi-agent "reload_morse_module: module reloaded with modules.d parameters confirmed"
        else
            logger -t wifi-agent "reload_morse_module: parameters did not take (see prior log line) - continuing anyway, next attempt or the reboot escalation below may still recover it"
        fi
        wifi up radio1 2>/dev/null
        sleep 8
        # Confirmed live (2026-08-01): a full netifd device-handler
        # reload (what LuCI's own "Save & Apply" actually triggers via
        # ucitrack, not a plain wireless-scoped reconf) is what reliably
        # gets a channel/bandwidth change to actually stick - see
        # cmd_apply's comment. Layered on top of the hard chip reset
        # above rather than replacing it: this cold-bringup path was
        # originally written for a lower-level SDIO/kernel failure mode
        # (hostapd logging `morse_cli ... failed with error code -1`)
        # that hasn't specifically been retested against the reload
        # mechanism alone, so keep both rather than assume one subsumes
        # the other.
        ubus call network reload 2>/dev/null
        /sbin/wifi reload_legacy 2>/dev/null
        sleep 8
        ifname="$(halow_ifname)"
        attempt=$((attempt + 1))
    done

    # Chip reset alone didn't recover it. Escalate to a full reboot -
    # confirmed live (2026-08-01) to clear an SDIO-level probe timeout
    # (`morse_sdio: probe of mmc0:0001:x failed with error -145`, i.e.
    # ETIMEDOUT - the chip not responding on the bus at all) that 3
    # chip-reset attempts alone did not clear. Capped at 2 auto-reboots
    # across invocations so a genuinely persistent hardware fault doesn't
    # reboot-loop the device forever - past that, give up and wait for a
    # human instead.
    reboot_count="$(cat "$reboot_count_file" 2>/dev/null || echo 0)"
    if [ "$reboot_count" -ge 2 ]; then
        logger -t wifi-agent "HaLow radio FAILED to reach configured channel $expected_channel after 3 recovery attempts AND $reboot_count prior auto-reboots - giving up, manual intervention needed"
        return 1
    fi
    echo "$((reboot_count + 1))" > "$reboot_count_file"
    logger -t wifi-agent "HaLow radio still not on channel $expected_channel after 3 chip-reset attempts (prior auto-reboots: $reboot_count) - rebooting as a last resort"
    reboot
    # reboot is asynchronous on OpenWrt (returns before the device actually
    # goes down) - block here rather than falling through, which would log
    # a misleading "manual intervention needed" while a reboot is already
    # in flight.
    sleep 60
    return 1
}

collect_halow() {
    ifname="$(halow_ifname)"
    driver_up="$(halow_radio_up)"
    [ -z "$ifname" ] && { printf '{"radio":"halow","radio_up":%s}\n' "$driver_up"; return; }

    info="$(ubus call iwinfo info "{\"device\":\"$ifname\"}" 2>/dev/null)"
    channel="$(echo "$info" | jsonfilter -e '@.channel' 2>/dev/null)"
    signal="$(echo "$info" | jsonfilter -e '@.signal' 2>/dev/null)"
    noise="$(echo "$info" | jsonfilter -e '@.noise' 2>/dev/null)"

    bw_mhz="$(ubus call rangetest morse_cli_channel '{}' 2>/dev/null | jsonfilter -e '@.bw_mhz' 2>/dev/null)"

    assoc="$(ubus call iwinfo assoclist "{\"device\":\"$ifname\"}" 2>/dev/null)"
    peer_mac="$(echo "$assoc" | jsonfilter -e '@.results[0].mac' 2>/dev/null)"
    mcs="$(echo "$assoc" | jsonfilter -e '@.results[0].tx.mcs' 2>/dev/null)"
    rate_raw="$(echo "$assoc" | jsonfilter -e '@.results[0].tx.rate' 2>/dev/null)"
    retries_cum="$(echo "$assoc" | jsonfilter -e '@.results[0].tx.retries' 2>/dev/null)"
    packets_cum="$(echo "$assoc" | jsonfilter -e '@.results[0].tx.packets' 2>/dev/null)"
    tx_bytes_cum="$(echo "$assoc" | jsonfilter -e '@.results[0].tx.bytes' 2>/dev/null)"
    rx_bytes_cum="$(echo "$assoc" | jsonfilter -e '@.results[0].rx.bytes' 2>/dev/null)"

    rate_mbps=""
    [ -n "$rate_raw" ] && rate_mbps="$(awk -v r="$rate_raw" 'BEGIN{printf "%.2f", r/1000}')"

    clients="[]"
    [ -n "$peer_mac" ] && clients="[{\"mac\":\"$peer_mac\",\"rssi\":$(jnum "$signal"),\"rate_mbps\":$(jnum "$rate_mbps"),\"retries_cum\":$(jnum "$retries_cum"),\"packets_cum\":$(jnum "$packets_cum")}]"

    # radio_up means "the link is actually usable", not just "the driver
    # initialized" - confirmed live (2026-08-03): a bandwidth-change apply
    # landed the AP's radio cleanly on the target channel with
    # retry_setup_failed=false, but the STA never followed. A driver-only
    # check would have reported that as healthy. This is the same peer
    # check cmd_verify_confirm already does for the halow radio - a P2P
    # bridge with no peer isn't up, whatever the driver itself thinks.
    if [ "$driver_up" = "true" ] && [ -n "$peer_mac" ]; then
        radio_up="true"
    else
        radio_up="false"
    fi

    printf '{"radio":"halow","radio_up":%s,"rssi":%s,"noise":%s,"mcs":%s,"rate_mbps":%s,"channel":%s,"bandwidth_mhz":%s,"retries_cum":%s,"packets_cum":%s,"tx_bytes_cum":%s,"rx_bytes_cum":%s,"clients":%s}\n' \
        "$radio_up" "$(jnum "$signal")" "$(jnum "$noise")" "$(jnum "$mcs")" "$(jnum "$rate_mbps")" \
        "$(jnum "$channel")" "$(jnum "$bw_mhz")" "$(jnum "$retries_cum")" "$(jnum "$packets_cum")" \
        "$(jnum "$tx_bytes_cum")" "$(jnum "$rx_bytes_cum")" "$clients"
}

collect_wifi24() {
    ifname="$(wifi24_ifname)"
    [ -z "$ifname" ] && { echo '{"radio":"wifi24"}'; return; }

    info="$(ubus call iwinfo info "{\"device\":\"$ifname\"}" 2>/dev/null)"
    channel="$(echo "$info" | jsonfilter -e '@.channel' 2>/dev/null)"

    assoc="$(ubus call iwinfo assoclist "{\"device\":\"$ifname\"}" 2>/dev/null)"

    clients="["
    first=1
    i=0
    while [ "$i" -lt 16 ]; do
        mac="$(echo "$assoc" | jsonfilter -e "@.results[$i].mac" 2>/dev/null)"
        [ -z "$mac" ] && break
        signal="$(echo "$assoc" | jsonfilter -e "@.results[$i].signal" 2>/dev/null)"
        rate_raw="$(echo "$assoc" | jsonfilter -e "@.results[$i].tx.rate" 2>/dev/null)"
        retries_cum="$(echo "$assoc" | jsonfilter -e "@.results[$i].tx.retries" 2>/dev/null)"
        packets_cum="$(echo "$assoc" | jsonfilter -e "@.results[$i].tx.packets" 2>/dev/null)"
        rate_mbps=""
        [ -n "$rate_raw" ] && rate_mbps="$(awk -v r="$rate_raw" 'BEGIN{printf "%.2f", r/1000}')"
        [ "$first" -eq 0 ] && clients="$clients,"
        clients="$clients{\"mac\":\"$mac\",\"rssi\":$(jnum "$signal"),\"rate_mbps\":$(jnum "$rate_mbps"),\"retries_cum\":$(jnum "$retries_cum"),\"packets_cum\":$(jnum "$packets_cum")}"
        first=0
        i=$((i + 1))
    done
    clients="$clients]"

    printf '{"radio":"wifi24","channel":%s,"clients":%s}\n' "$(jnum "$channel")" "$clients"
}

# Combined single-shot snapshot of both radios - what the server pulls
# over SSH every SSH_POLL_INTERVAL_SECONDS (see app/device_client.py's
# collect() and main.py's poll_telemetry).
cmd_collect() {
    halow_json="$(collect_halow)"
    wifi24_json="$(collect_wifi24)"
    printf '{"device_mac":"%s","hostname":"%s","radios":[%s,%s]}\n' \
        "$MAC" "$DEVICE_HOSTNAME" "$halow_json" "$wifi24_json"
}

# Rejects anything that isn't a plausible-looking channel number before it
# ever reaches `uci set` - added 2026-08-11. The server (halow_channel_plan.py)
# is the real source of truth for which channels are valid, and this can't
# duplicate that logic without the two drifting out of sync - but a bare
# minimum sanity check (positive integer, sane upper bound) costs nothing
# and means a malformed value from a future bug on the server side fails
# loudly here instead of getting silently `uci set` onto the radio.
is_plausible_channel() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$1" -ge 1 ] && [ "$1" -le 200 ]
}

apply_halow_operating_freq() {
    # $1 = JSON target_value, e.g. {"channel":44}
    # See header note: bandwidth is implied by the channel index itself,
    # there is no separate width uci option to set.
    #
    # Deliberately does NOT `uci commit` here - see cmd_apply's comment.
    # Committing immediately would flush this to disk before `uci apply
    # --rollback` ever gets a chance to track it as a revertible staged
    # change, which - confirmed live, 2026-08-03 - makes the rollback
    # safety net a complete no-op: `ubus call uci changes` came back
    # empty and a failed apply sat stuck on the bad channel for over 10
    # minutes (well past the 120s timeout) with nothing reverting it.
    channel="$(echo "$1" | jsonfilter -e '@.channel' 2>/dev/null)"
    [ -z "$channel" ] && { echo "apply_halow_operating_freq: missing channel in $1" >&2; return 1; }
    is_plausible_channel "$channel" || { echo "apply_halow_operating_freq: implausible channel value '$channel', refusing" >&2; return 1; }
    uci set wireless.radio1.channel="$channel"
}

apply_wifi24_channel() {
    # $1 = JSON target_value, e.g. {"channel":6}
    # No `uci commit` here either - same reasoning as
    # apply_halow_operating_freq above.
    channel="$(echo "$1" | jsonfilter -e '@.channel' 2>/dev/null)"
    [ -z "$channel" ] && { echo "apply_wifi24_channel: missing channel in $1" >&2; return 1; }
    is_plausible_channel "$channel" || { echo "apply_wifi24_channel: implausible channel value '$channel', refusing" >&2; return 1; }
    uci set wireless.radio0.channel="$channel"
}

# $1 = param, $2 = target_value JSON, $3 = ttl_seconds. Stages the config
# change and starts OpenWrt's own rollback timer, then returns immediately
# - this SSH session doesn't stay open to babysit the change (that's what
# `verify-confirm`, called again a bit later, is for). If nothing ever
# confirms it (the server never manages to reconnect at all), the
# on-device timer reverts it on its own regardless.
cmd_apply() {
    param="$1"; target_value="$2"; ttl_seconds="${3:-120}"

    # Serialize against recover-radio's chip-reset/reboot path and against
    # a second overlapping apply (e.g. a retried SSH call while a prior
    # reload is still running remotely) - see acquire_radio_lock's comment.
    acquire_radio_lock || { echo "cmd_apply: could not acquire radio lock, another radio operation is in progress" >&2; exit 1; }

    case "$param" in
        halow_operating_freq) apply_halow_operating_freq "$target_value" || exit 1 ;;
        wifi24_channel) apply_wifi24_channel "$target_value" || exit 1 ;;
        *) echo "unknown param $param" >&2; exit 1 ;;
    esac

    # Native OpenWrt safe-apply: THIS is what actually commits the `uci
    # set` above to disk - the same mechanism LuCI's own "Save & Apply"
    # countdown uses - with an automatic rollback if not confirmed within
    # ttl_seconds. The persistent-config safety net (reverts uci on its
    # own even if the server can never reconnect to confirm), independent
    # of, and in addition to, the reload below. Only works because the
    # apply_*() functions above stage the change with plain `uci set` and
    # deliberately don't commit it themselves first.
    #
    # CRITICAL, added 2026-08-11 after review: this call's exit status
    # used to be completely ignored. Confirmed live (2026-08-03) that it
    # can fail outright ("Invalid argument") while the staged `uci set`
    # above is still sitting there uncommitted - if the script had barreled
    # on into the reload below anyway (as it used to), the change would
    # go live with *no* safety net at all: no rollback timer, nothing to
    # revert it if the change is bad and the server can never reconnect to
    # confirm it. That combination is the single biggest contributor to
    # this session's damage. Now: if the safety net itself doesn't engage,
    # the staged change is explicitly reverted and the whole apply is
    # aborted before it ever touches the live radio - the server's own
    # retry logic (apply_pending_commands) will try again next tick, by
    # which point whatever made ubus/rpcd unhappy may have cleared.
    if ! ubus call uci apply "{\"rollback\":true,\"timeout\":$ttl_seconds}"; then
        echo "cmd_apply: 'uci apply --rollback' itself failed - no safety net available, reverting the staged change and aborting without touching the live radio" >&2
        uci revert wireless
        exit 1
    fi

    if [ "$param" = "halow_operating_freq" ]; then
        # Confirmed live (2026-08-01): a plain `network.wireless reconf`
        # does NOT reliably push a channel/bandwidth change to this HaLow
        # radio - several channel-only targets failed via it, and one
        # bandwidth-changing target (4MHz -> 2MHz) crashed the AP hard
        # enough to need a physical reboot. Traced the real mechanism by
        # diffing LuCI's own logged behavior against ours: LuCI's "Save &
        # Apply" doesn't call network.wireless reconf directly - it goes
        # through the standard OpenWrt ucitrack "wireless affects
        # network" mapping (see /etc/config/ucitrack), which triggers
        # `/etc/init.d/network reload_service()`:
        #   ubus call network reload
        #   /sbin/wifi reload_legacy
        # That's a full netifd device-handler reload (logged as "Adding
        # device handler type: morse" / "Configuring radio1" / "Full
        # Channel Information"), not just a wireless-scoped reconf - and
        # unlike our earlier `wifi down/up radio1` + chip-reset
        # workaround, it's confirmed live to apply BOTH same-bandwidth
        # channel changes AND bandwidth changes (4MHz<->2MHz) cleanly,
        # with the STA reassociating automatically either way. This is
        # what LuCI has been doing under the hood all along - replicating
        # it exactly here, rather than the narrower reconf call or the
        # chip-reset workaround, is what actually fixes this.
        ubus call network reload
        /sbin/wifi reload_legacy
    else
        ubus call network.wireless reconf
    fi
}

# Reads the live radio state back and compares it to what the command
# actually asked for. uci committing successfully and `reconf` returning
# without error prove nothing on their own - confirmed live, an invalid
# channel/bandwidth combination is silently ignored by the driver. Only on
# a confirmed match does this call `uci confirm` to cancel the pending
# rollback; otherwise it leaves the rollback timer alone to revert on its
# own. $1 = param, $2 = target_value JSON.
cmd_verify_confirm() {
    param="$1"; target_value="$2"
    case "$param" in
        halow_operating_freq) ifname="$(halow_ifname)" ;;
        wifi24_channel) ifname="$(wifi24_ifname)" ;;
        *) echo "failed"; exit 1 ;;
    esac
    if [ -z "$ifname" ]; then
        echo "failed"; exit 1
    fi
    target_channel="$(echo "$target_value" | jsonfilter -e '@.channel' 2>/dev/null)"
    live_channel="$(ubus call iwinfo info "{\"device\":\"$ifname\"}" 2>/dev/null | jsonfilter -e '@.channel' 2>/dev/null)"
    if [ -z "$live_channel" ] || [ "$live_channel" != "$target_channel" ]; then
        echo "failed"; exit 1
    fi

    if [ "$param" = "halow_operating_freq" ]; then
        # Our own radio reaching the target channel proves nothing about
        # whether the PEER followed - this is a P2P bridge, so a healthy
        # change means the STA reassociated, not just that our own radio
        # moved. This matters far more once this isn't sitting on a bench
        # with a huge signal margin: a bad change could leave our own
        # radio looking perfectly fine while the far end never comes back.
        peer_mac="$(ubus call iwinfo assoclist "{\"device\":\"$ifname\"}" 2>/dev/null | jsonfilter -e '@.results[0].mac' 2>/dev/null)"
        if [ -z "$peer_mac" ]; then
            echo "failed"; exit 1
        fi
    fi
    # wifi24_channel has no equivalent check: downstream 2.4GHz clients are
    # transient (can legitimately be idle/off), so "zero clients right
    # now" doesn't mean the change failed the way "zero HaLow peers" does
    # on a link that's supposed to always have exactly one.

    ubus call uci confirm '{}'
    echo "confirmed"
}

# Config backup: tars up the same UCI config this device's LuCI "Backup"
# button would, plus the agent's own script, and writes the raw archive
# bytes to stdout for the server to pull over SSH (see
# app/device_client.py's fetch_backup and main.py's poll_backups) - the
# same files hobo_cams' backup.sh pulls over SSH, just on a schedule the
# server drives instead of requiring someone to run it manually. Dedupe by
# sha256 happens server-side (poll_backups), so calling this regularly is
# cheap - most calls are a no-op once the config stabilizes.
cmd_backup() {
    command -v tar >/dev/null 2>&1 || { echo "backup: tar not available" >&2; exit 1; }

    tar_paths="/etc/config"
    for f in /usr/bin/wifi-agent.sh /etc/init.d/wifi-agent-boot /etc/crontabs/root; do
        [ -e "$f" ] && tar_paths="$tar_paths $f"
    done

    tarball="$(mktemp)"
    tar -czf "$tarball" $tar_paths 2>/dev/null
    if [ ! -s "$tarball" ]; then
        echo "backup: tar produced no archive" >&2
        rm -f "$tarball"
        exit 1
    fi
    cat "$tarball"
    rm -f "$tarball"
}

case "$1" in
    collect) cmd_collect ;;
    apply) shift; cmd_apply "$@" ;;
    verify-confirm) shift; cmd_verify_confirm "$@" ;;
    backup) cmd_backup ;;
    recover-radio) verify_and_recover_radio ;;
    *)
        echo "usage: wifi-agent.sh <collect|apply|verify-confirm|backup|recover-radio> [args...]" >&2
        exit 1
        ;;
esac
