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
# has them). bw=4 and bw=8 match the CSV exactly - no edge channels to
# drop there. The optimizer's attempt to narrow to channel 2 (2MHz, one
# of the CSV-only edge channels LuCI itself refuses to offer) is what
# caused that incident - the AP's radio ended up in a broken state
# (channel 0, txpower 0) that needed a physical reboot plus manual
# recovery to fix. Lists below match LuCI's actual selectable set, not
# the raw CSV, so the optimizer can never target those edge channels
# again. This does NOT explain two other channels (48, 8 at bw=4) that
# also failed live despite being valid in both the CSV and LuCI's
# dropdown - see README/Gotchas; those stay excluded via the commands
# table's own reverted-channel tracking (_reverted_channels), not by
# removal from this list.

US_CHANNELS_BY_BANDWIDTH_MHZ: dict[int, list[int]] = {
    1: [3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31, 33, 35, 37, 39, 41, 43, 45, 47, 49],
    2: [6, 10, 14, 18, 22, 26, 30, 34, 38, 42, 46],
    4: [8, 16, 24, 32, 40, 48],
    8: [12, 28, 44],
}


def valid_channels(bandwidth_mhz: int) -> list[int]:
    return US_CHANNELS_BY_BANDWIDTH_MHZ.get(bandwidth_mhz, [])


def is_valid(channel: int, bandwidth_mhz: int) -> bool:
    return channel in valid_channels(bandwidth_mhz)
