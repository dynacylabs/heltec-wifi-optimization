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

halow_ifname() {
    ubus call network.wireless status 2>/dev/null | jsonfilter -e '@.radio1.interfaces[0].ifname' 2>/dev/null
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
            return 0
        fi
        logger -t wifi-agent "HaLow radio on channel ${live_channel:-none}, expected $expected_channel (recovery attempt $attempt/3) - hard-resetting chip"
        wifi down radio1 2>/dev/null
        sleep 2
        [ -x /morse/scripts/chipreset.sh ] && sh /morse/scripts/chipreset.sh 2>/dev/null
        sleep 3
        wifi up radio1 2>/dev/null
        sleep 8
        # A live reconf on top of the fresh bring-up is what actually got the
        # channel to stick in testing, not the reset/restart alone.
        ubus call network.wireless reconf 2>/dev/null
        sleep 8
        ifname="$(halow_ifname)"
        attempt=$((attempt + 1))
    done

    logger -t wifi-agent "HaLow radio FAILED to reach configured channel $expected_channel after 3 recovery attempts - manual intervention likely needed"
    return 1
}

collect_halow() {
    ifname="$(halow_ifname)"
    [ -z "$ifname" ] && { echo '{"radio":"halow"}'; return; }

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

    printf '{"radio":"halow","rssi":%s,"noise":%s,"mcs":%s,"rate_mbps":%s,"channel":%s,"bandwidth_mhz":%s,"retries_cum":%s,"packets_cum":%s,"tx_bytes_cum":%s,"rx_bytes_cum":%s,"clients":%s}\n' \
        "$(jnum "$signal")" "$(jnum "$noise")" "$(jnum "$mcs")" "$(jnum "$rate_mbps")" \
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

apply_halow_operating_freq() {
    # $1 = JSON target_value, e.g. {"channel":44}
    # See header note: bandwidth is implied by the channel index itself,
    # there is no separate width uci option to set.
    channel="$(echo "$1" | jsonfilter -e '@.channel' 2>/dev/null)"
    [ -z "$channel" ] && { echo "apply_halow_operating_freq: missing channel in $1" >&2; return 1; }
    uci set wireless.radio1.channel="$channel"
    uci commit wireless
}

apply_wifi24_channel() {
    # $1 = JSON target_value, e.g. {"channel":6}
    channel="$(echo "$1" | jsonfilter -e '@.channel' 2>/dev/null)"
    [ -z "$channel" ] && { echo "apply_wifi24_channel: missing channel in $1" >&2; return 1; }
    uci set wireless.radio0.channel="$channel"
    uci commit wireless
}

# $1 = param, $2 = target_value JSON, $3 = ttl_seconds. Stages the config
# change and starts OpenWrt's own rollback timer, then returns immediately
# - this SSH session doesn't stay open to babysit the change (that's what
# `verify-confirm`, called again a bit later, is for). If nothing ever
# confirms it (the server never manages to reconnect at all), the
# on-device timer reverts it on its own regardless.
cmd_apply() {
    param="$1"; target_value="$2"; ttl_seconds="${3:-120}"
    case "$param" in
        halow_operating_freq) apply_halow_operating_freq "$target_value" || exit 1 ;;
        wifi24_channel) apply_wifi24_channel "$target_value" || exit 1 ;;
        *) echo "unknown param $param" >&2; exit 1 ;;
    esac

    # Native OpenWrt safe-apply: stages the uci commit above for real, with
    # an automatic rollback if not confirmed within ttl_seconds - the same
    # mechanism LuCI's own "Save & Apply" countdown uses. Left in place
    # for both params as the persistent-config safety net (reverts uci on
    # its own even if the server can never reconnect to confirm) -
    # independent of, and in addition to, the live-radio fix below.
    ubus call uci apply "{\"rollback\":true,\"timeout\":$ttl_seconds}"

    # Confirmed live (2026-08-01): a plain `network.wireless reconf` does
    # NOT reliably push a *same-bandwidth* channel change to this HaLow
    # radio - four separate channel targets (48, 8, 16, and initially 24)
    # were tried, and only once this same hard chip-reset sequence
    # (already used for cold-bringup recovery - see
    # verify_and_recover_radio/wifi-agent-boot.init) ran ahead of reconf
    # did the live channel actually move. Only for halow_operating_freq -
    # wifi24_channel is plain 2.4GHz Wi-Fi, no evidence plain reconf is
    # insufficient there.
    #
    # IMPORTANT: this is only safe for a channel change that keeps the
    # SAME bandwidth as before. A *bandwidth* change (e.g. 4MHz -> 2MHz)
    # through this exact same sequence crashed the AP hard, twice, with
    # two different valid target channels - confirmed to be a firmware-
    # level issue (traced into hostapd_s1g/chip territory, past anything
    # fixable from uci/netifd), not a channel-validity or apply-method
    # problem. The optimizer's widen/narrow (bandwidth-changing) paths
    # must never be routed through this - see optimizer.py, which keeps
    # halow_channel_optimization_enabled scoped to same-bandwidth cycling
    # only. See README/Gotchas for the full incident writeup.
    if [ "$param" = "halow_operating_freq" ]; then
        wifi down radio1
        sleep 2
        [ -x /morse/scripts/chipreset.sh ] && sh /morse/scripts/chipreset.sh
        sleep 3
        wifi up radio1
        sleep 8
    fi

    ubus call network.wireless reconf
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
