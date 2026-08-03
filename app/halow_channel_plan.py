# US HaLow (802.11ah) channel plan for the Morse Micro MM6108A1, sourced
# from /usr/share/morse-regdb/channels.csv on the actual device (package
# morse-regdb v2.2.1, confirmed live 2026-07-15) - NOT derived from public
# docs or guessed. `s1g_chan` in that CSV is exactly the value that goes
# into `wireless.radio1.channel`.
#
# Confirmed live: setting a channel that isn't valid for the CURRENT
# bandwidth fails silently (uci commits fine, the radio just keeps running
# its last-good state) - channel numbering is bandwidth-dependent, there
# is no single global channel space. Always pick from the list matching
# the bandwidth you're actually configuring, never an arbitrary "channel
# we've used before at some other bandwidth."
#
# Only the US table is included, since that's what both real devices are
# configured for (country=US). Re-derive from the same CSV on-device if
# this is ever deployed under a different regulatory domain.
#
# IMPORTANT, confirmed live (2026-07-31 incident): the raw CSV is not the
# full story. LuCI's own "Operating frequency" channel dropdown - cross-
# checked by hand against every bandwidth here - omits the outermost
# (lowest and highest frequency) channel at each end of the 1MHz and 2MHz
# bandwidths, even though they're present in the CSV: bw=1 excludes
# channels 1 and 51 (CSV has them), bw=2 excludes channels 2 and 50 (CSV
# has them). The optimizer's attempt to narrow to channel 2 (2MHz, one of
# the CSV-only edge channels LuCI itself refuses to offer) is what caused
# that incident - the AP's radio ended up in a broken state (channel 0,
# txpower 0) that needed a physical reboot plus manual recovery to fix.
#
# bw=4's list also drops channels 8 and 48, both CSV/LuCI-valid but
# confirmed live to fail repeatedly on this specific radio, independent of
# the edge-channel issue above. 48 has direct kernel-level proof (AP
# dmesg, 2026-08-03): `morse_mac_change_channel: HW does not permit
# channel (f:926000 kHz, bw:4 MHz)` - 926MHz is channel 48's center
# frequency, right at the top edge of the US S1G band, and the Morse
# chip's own firmware rejects it outright, not just "the config didn't
# take." 48 was also the very first channel this optimizer ever tried
# (2026-07-30) and has failed every time since. 8 failed the same way
# repeatedly with no kernel-level root cause pinned down yet, but the
# pattern (CSV/LuCI-valid, silently or firmware-rejected in practice) is
# the same. Hardcoded here rather than left to the commands table's
# reverted-channel tracking (_reverted_channels) alone, since that's
# learned-by-trial and only as durable as the commands table's history -
# this is a known structural fact about the hardware, not something that
# should ever need re-discovering.
#
# 16 also failed once (2026-08-01) but that was before the on-device
# apply mechanism itself was fixed (see wifi-agent.sh's cmd_apply history)
# - not hardcoded here since it hasn't been retried under the corrected
# mechanism. _reverted_channels still excludes it from being picked again
# until it has.

US_CHANNELS_BY_BANDWIDTH_MHZ: dict[int, list[int]] = {
    1: [3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31, 33, 35, 37, 39, 41, 43, 45, 47, 49],
    2: [6, 10, 14, 18, 22, 26, 30, 34, 38, 42, 46],
    4: [16, 24, 32, 40],
    8: [12, 28, 44],
}


def valid_channels(bandwidth_mhz: int) -> list[int]:
    return US_CHANNELS_BY_BANDWIDTH_MHZ.get(bandwidth_mhz, [])


def is_valid(channel: int, bandwidth_mhz: int) -> bool:
    return channel in valid_channels(bandwidth_mhz)
