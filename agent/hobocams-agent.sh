#!/bin/sh
# hobocams-agent.sh - telemetry + command agent for Heltec HT-HD01-V2
#
# Deploy under /usr/bin/hobocams-agent.sh, run via the hobocams-agent init
# script (procd), configured per-device by /etc/hobocams-agent.conf.
#
# Every command/field below was verified live via SSH on the actual AP
# (192.168.2.2) and STA (192.168.2.3), OpenWrt 23.05.5 / firmware
# 2.8.5-20250924:
#   - HaLow radio interface is `wlan0` on both AP and STA (phy1, Morse
#     Micro MM6108A1). The 2.4GHz radio interface is `phy0-ap0` on the STA
#     (phy0, MediaTek MT7628) - looked up dynamically below rather than
#     hardcoded, since naming isn't guaranteed to be identical on the AP if
#     its 2.4GHz radio is ever enabled.
#   - No `curl` on this firmware - only busybox `wget`, which does support
#     `--post-data` for POST but has no `--header` flag, so no
#     Content-Type is sent (FastAPI parses the body as JSON regardless).
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
#   - The device's own retry/packet counters are CUMULATIVE since boot, not
#     an instantaneous rate - this script computes a delta between polls
#     (see delta_rate()) to get an actual retry fraction.
#   - `ubus call uci apply '{"rollback":true,"timeout":N}'` + `uci confirm`/
#     `uci rollback` is OpenWrt's native safe-apply mechanism (same one
#     LuCI's own "Save & Apply" countdown uses) - used here instead of a
#     hand-rolled config snapshot, since it's a tested, built-in feature
#     that survives more failure modes (e.g. a crash mid-window) than a
#     shell-script-level snapshot would.
#   - IMPORTANT, confirmed live: `uci apply` alone does NOT reliably push a
#     wireless config change to the radio - an explicit
#     `ubus call network.wireless reconf` is still required afterward.
#   - ALSO confirmed live: an invalid channel/bandwidth combination (HaLow
#     channel numbering is bandwidth-dependent, and the valid combinations
#     aren't fully mapped yet - see Task "Research HaLow channel-plan")
#     fails SILENTLY - the uci config commits fine and reconf returns no
#     error, but the radio just keeps running its last-good state. This is
#     why poll_and_apply_command reads the live value back and compares it
#     to the target before ever calling `uci confirm` - reachability alone
#     (the AP staying up over Ethernet) proves nothing about whether the
#     HaLow radio itself actually changed, since that path doesn't go
#     through HaLow at all.

: "${HOBOCAMS_SERVER_URL:?set HOBOCAMS_SERVER_URL, e.g. http://masha.lan:8080}"
: "${HOBOCAMS_ROLE:?set HOBOCAMS_ROLE=AP or STA}"
: "${HOBOCAMS_API_TOKEN:?set HOBOCAMS_API_TOKEN to the shared secret}"

MAC="$(cat /sys/class/net/eth0/address)"
DEVICE_HOSTNAME="$(uci get system.@system[0].hostname 2>/dev/null)"
STATE_DIR=/tmp/hobocams
mkdir -p "$STATE_DIR"

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
# followed by a live channel reconf. Run once at agent startup (i.e. once
# per device boot, or whenever the agent process itself restarts) so a
# power interruption in the field - expected on solar/battery - doesn't
# strand the link on a channel the peer isn't listening on.
verify_and_recover_radio() {
    ifname="$(halow_ifname)"
    [ -z "$ifname" ] && { echo "verify_and_recover_radio: no HaLow ifname yet, skipping" >&2; return 1; }
    expected_channel="$(uci get wireless.radio1.channel 2>/dev/null)"
    [ -z "$expected_channel" ] && { echo "verify_and_recover_radio: no configured channel in uci, skipping" >&2; return 1; }

    attempt=1
    while [ "$attempt" -le 3 ]; do
        live_channel="$(ubus call iwinfo info "{\"device\":\"$ifname\"}" 2>/dev/null | jsonfilter -e '@.channel' 2>/dev/null)"
        if [ "$live_channel" = "$expected_channel" ]; then
            logger -t hobocams-agent "HaLow radio confirmed on configured channel $expected_channel"
            return 0
        fi
        logger -t hobocams-agent "HaLow radio on channel ${live_channel:-none}, expected $expected_channel (recovery attempt $attempt/3) - hard-resetting chip"
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

    logger -t hobocams-agent "HaLow radio FAILED to reach configured channel $expected_channel after 3 recovery attempts - manual intervention likely needed"
    return 1
}

# Fraction of (cur_num - prev_num) over (cur_den - prev_den) since the last
# call with this state key, e.g. retries-over-packets since the last poll.
# Cumulative counters reset (reboot, counter wrap) are treated as "no data
# this interval" (0) rather than producing a nonsense negative/huge value.
delta_rate() {
    key="$1"; cur_num="$2"; cur_den="$3"
    f="$STATE_DIR/$key"
    prev_num=0; prev_den=0
    if [ -f "$f" ]; then
        prev_num="$(cut -d' ' -f1 "$f" 2>/dev/null)"
        prev_den="$(cut -d' ' -f2 "$f" 2>/dev/null)"
        [ -z "$prev_num" ] && prev_num=0
        [ -z "$prev_den" ] && prev_den=0
    fi
    echo "$cur_num $cur_den" > "$f"
    d_num=$((cur_num - prev_num))
    d_den=$((cur_den - prev_den))
    if [ "$d_num" -lt 0 ] || [ "$d_den" -le 0 ]; then
        echo 0
    else
        awk -v n="$d_num" -v d="$d_den" 'BEGIN{printf "%.4f", n/d}'
    fi
}

# Actual data throughput in Mbit/s from a cumulative byte counter. Unlike
# delta_rate (a request-count ratio), a byte count needs a real wall-clock
# time base to become a rate, so this tracks elapsed seconds between polls
# too rather than assuming a fixed interval.
delta_throughput_mbps() {
    key="$1"; cur_bytes="$2"
    f="$STATE_DIR/$key"
    now_ts="$(date +%s)"
    prev_bytes=0; prev_ts=0
    if [ -f "$f" ]; then
        prev_bytes="$(cut -d' ' -f1 "$f" 2>/dev/null)"
        prev_ts="$(cut -d' ' -f2 "$f" 2>/dev/null)"
        [ -z "$prev_bytes" ] && prev_bytes=0
        [ -z "$prev_ts" ] && prev_ts=0
    fi
    echo "$cur_bytes $now_ts" > "$f"
    d_bytes=$((cur_bytes - prev_bytes))
    d_secs=$((now_ts - prev_ts))
    if [ "$d_bytes" -lt 0 ] || [ "$d_secs" -le 0 ]; then
        echo 0
    else
        awk -v b="$d_bytes" -v s="$d_secs" 'BEGIN{printf "%.4f", (b*8)/(s*1000000)}'
    fi
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

    retry_rate=""
    if [ -n "$retries_cum" ] && [ -n "$packets_cum" ]; then
        retry_rate="$(delta_rate "halow-retries" "$retries_cum" "$packets_cum")"
    fi

    # Actual data throughput (both directions combined), as opposed to
    # rate_mbps above which is just the negotiated PHY link rate - the
    # optimizer's bandwidth widen/narrow decision needs to compare real
    # demand against capacity, not just know what the radio is capable of.
    throughput_mbps=""
    if [ -n "$tx_bytes_cum" ] && [ -n "$rx_bytes_cum" ]; then
        total_bytes_cum=$((tx_bytes_cum + rx_bytes_cum))
        throughput_mbps="$(delta_throughput_mbps "halow-throughput" "$total_bytes_cum")"
    fi

    clients="[]"
    [ -n "$peer_mac" ] && clients="[{\"mac\":\"$peer_mac\",\"rssi\":$(jnum "$signal"),\"rate_mbps\":$(jnum "$rate_mbps")}]"

    printf '{"radio":"halow","rssi":%s,"noise":%s,"mcs":%s,"rate_mbps":%s,"retries":%s,"channel":%s,"bandwidth_mhz":%s,"throughput_mbps":%s,"clients":%s}\n' \
        "$(jnum "$signal")" "$(jnum "$noise")" "$(jnum "$mcs")" "$(jnum "$rate_mbps")" \
        "$(jnum "$retry_rate")" "$(jnum "$channel")" "$(jnum "$bw_mhz")" "$(jnum "$throughput_mbps")" "$clients"
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
        retry_rate=""
        if [ -n "$retries_cum" ] && [ -n "$packets_cum" ]; then
            retry_rate="$(delta_rate "wifi24-retries-$mac" "$retries_cum" "$packets_cum")"
        fi
        [ "$first" -eq 0 ] && clients="$clients,"
        clients="$clients{\"mac\":\"$mac\",\"rssi\":$(jnum "$signal"),\"rate_mbps\":$(jnum "$rate_mbps"),\"retries\":$(jnum "$retry_rate")}"
        first=0
        i=$((i + 1))
    done
    clients="$clients]"

    printf '{"radio":"wifi24","channel":%s,"clients":%s}\n' "$(jnum "$channel")" "$clients"
}

post_telemetry() {
    halow_json="$(collect_halow)"
    wifi24_json="$(collect_wifi24)"
    payload="{\"device_mac\":\"$MAC\",\"hostname\":\"$DEVICE_HOSTNAME\",\"role\":\"$HOBOCAMS_ROLE\",\"radios\":[$halow_json,$wifi24_json]}"
    wget -q -O /dev/null --post-data="$payload" "$HOBOCAMS_SERVER_URL/telemetry?token=$HOBOCAMS_API_TOKEN" 2>/dev/null
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

# Reads the live radio state back and compares it to what the command
# actually asked for. uci committing successfully and `reconf` returning
# without error prove nothing on their own - confirmed live, an invalid
# channel/bandwidth combination is silently ignored by the driver.
verify_command_applied() {
    # $1 = param, $2 = target_value JSON
    case "$1" in
        halow_operating_freq) ifname="$(halow_ifname)" ;;
        wifi24_channel) ifname="$(wifi24_ifname)" ;;
        *) return 1 ;;
    esac
    [ -z "$ifname" ] && return 1
    target_channel="$(echo "$2" | jsonfilter -e '@.channel' 2>/dev/null)"
    live_channel="$(ubus call iwinfo info "{\"device\":\"$ifname\"}" 2>/dev/null | jsonfilter -e '@.channel' 2>/dev/null)"
    [ -z "$live_channel" ] || [ "$live_channel" != "$target_channel" ] && return 1

    if [ "$1" = "halow_operating_freq" ]; then
        # Our own radio reaching the target channel proves nothing about
        # whether the PEER followed - this is a P2P bridge, so a healthy
        # change means the STA reassociated, not just that our own radio
        # moved. This matters far more once this isn't sitting on a bench
        # with a huge signal margin: a bad change could leave our own
        # radio looking perfectly fine while the far end never comes back.
        peer_mac="$(ubus call iwinfo assoclist "{\"device\":\"$ifname\"}" 2>/dev/null | jsonfilter -e '@.results[0].mac' 2>/dev/null)"
        [ -z "$peer_mac" ] && return 1
    fi
    # wifi24_channel has no equivalent check: Blink/Shelly clients are
    # transient (can legitimately be idle/off), so "zero clients right
    # now" doesn't mean the change failed the way "zero HaLow peers" does
    # on a link that's supposed to always have exactly one.

    return 0
}

poll_and_apply_command() {
    body="$(wget -q -O - "$HOBOCAMS_SERVER_URL/commands/$MAC?token=$HOBOCAMS_API_TOKEN" 2>/dev/null)"
    [ -z "$body" ] && return 0

    command_id="$(echo "$body" | jsonfilter -e '@.command_id' 2>/dev/null)"
    param="$(echo "$body" | jsonfilter -e '@.param' 2>/dev/null)"
    target_value="$(echo "$body" | jsonfilter -e '@.target_value' 2>/dev/null)"
    ttl_seconds="$(echo "$body" | jsonfilter -e '@.ttl_seconds' 2>/dev/null)"
    [ -z "$command_id" ] && return 0

    # reboot has none of the uci apply/rollback/verify machinery below - the
    # device is about to disappear, so there's nothing to roll back and no
    # live state left to verify against. Ack first (so the server has
    # confirmation before we lose the connection), then reboot after a
    # short delay so the ack request has time to actually complete.
    if [ "$param" = "reboot" ]; then
        wget -q -O /dev/null --post-data='{"status":"acked"}' \
            "$HOBOCAMS_SERVER_URL/commands/$command_id/report?token=$HOBOCAMS_API_TOKEN" 2>/dev/null
        ( sleep 2; reboot ) &
        return 0
    fi

    case "$param" in
        halow_operating_freq) apply_halow_operating_freq "$target_value" ;;
        wifi24_channel) apply_wifi24_channel "$target_value" ;;
        *) echo "unknown param $param" >&2; return 1 ;;
    esac

    rm -f "$STATE_DIR/cmd-$command_id.reported"

    # Native OpenWrt safe-apply: stages the uci commit above for real, with
    # an automatic rollback if not confirmed within ttl_seconds - the same
    # mechanism LuCI's own "Save & Apply" countdown uses. Confirmed live
    # that this alone does not push the change to the radio - reconf is
    # still required (see header note).
    ubus call uci apply "{\"rollback\":true,\"timeout\":$ttl_seconds}"
    ubus call network.wireless reconf

    (
        sleep 25
        if verify_command_applied "$param" "$target_value"; then
            if [ -n "$(wget -q -O - "$HOBOCAMS_SERVER_URL/health" 2>/dev/null)" ]; then
                ubus call uci confirm '{}'
                touch "$STATE_DIR/cmd-$command_id.reported"
                wget -q -O /dev/null --post-data='{"status":"acked"}' \
                    "$HOBOCAMS_SERVER_URL/commands/$command_id/report?token=$HOBOCAMS_API_TOKEN" 2>/dev/null
            fi
        else
            # Config committed but the radio never actually reached the
            # target state - not safe to confirm. Let uci's own rollback
            # timer revert it, and report the real reason now rather than
            # waiting out the full ttl_seconds and reporting a generic
            # timeout.
            touch "$STATE_DIR/cmd-$command_id.reported"
            wget -q -O /dev/null --post-data='{"status":"reverted","reason":"target value not reached after apply"}' \
                "$HOBOCAMS_SERVER_URL/commands/$command_id/report?token=$HOBOCAMS_API_TOKEN" 2>/dev/null
        fi
    ) &

    (
        sleep "$((ttl_seconds + 15))"
        if [ ! -f "$STATE_DIR/cmd-$command_id.reported" ]; then
            wget -q -O /dev/null --post-data='{"status":"reverted","reason":"no ack within ttl_seconds"}' \
                "$HOBOCAMS_SERVER_URL/commands/$command_id/report?token=$HOBOCAMS_API_TOKEN" 2>/dev/null
        fi
    ) &
}

# Once per agent start (device boot, or a manual agent restart) - see
# verify_and_recover_radio()'s comment for why this matters on solar/battery
# power specifically.
verify_and_recover_radio

while true; do
    post_telemetry
    poll_and_apply_command
    sleep 30
done
