#!/usr/bin/env bash
# Router .31 — measure which cake tin each DSCP code point actually lands in,
# on YOUR kernel. Needs cake (diffserv3, the default) on $DEV.
#
#   ./dscpmap.sh
#   DEV=eth0 TARGET=1.1.1.1 ./dscpmap.sh     any host reachable out of $DEV
#
# For each code point it sends 12 pings with that ToS byte from the router
# itself, then reports which tin's packet counter moved the most. Run it on a
# quiet link — other traffic moving the counters muddies the result.
#
# Why measure instead of reading a table: the map is a kernel detail and it
# has changed between versions. Measured on the video's lab (diffserv3):
#   Bulk         LE (1), CS1 (8)
#   Voice        EF (46), CS6 (48), CS7 (56)
#   Best Effort  everything else — including AF41 and CS5, the values
#                enterprise gear uses for video and voice.
set -uo pipefail

DEV=${DEV:-eth0}
TARGET=${TARGET:-192.168.12.120}

tc qdisc show dev "$DEV" | grep -q '^qdisc cake' \
    || { echo "no cake on $DEV — try: sudo ./set-modem.sh cake" >&2; exit 1; }
tc qdisc show dev "$DEV" | grep -q diffserv3 \
    || echo "warning: $DEV is not running diffserv3; tin names below assume it" >&2

# per-tin packet counters, in column order: Bulk, Best Effort, Voice
get() { tc -s qdisc show dev "$DEV" | awk '/^  pkts/ { print $2, $3, $4 }'; }

printf "%-6s %-5s %-8s -> %s\n" ToS DSCP name tin
for e in "0x00 0 CS0/BE" "0x04 1 LE"    "0x10 4 DSCP4"  "0x18 6 DSCP6" \
         "0x20 8 CS1"    "0x28 10 AF11" "0x40 16 CS2"   "0x48 18 AF21" \
         "0x60 24 CS3"   "0x68 26 AF31" "0x80 32 CS4"   "0x88 34 AF41" \
         "0xa0 40 CS5"   "0xb0 44 VA"   "0xb8 46 EF"    "0xc0 48 CS6" \
         "0xe0 56 CS7"; do
    set -- $e; tos=$1; dscp=$2; name=$3
    before=($(get))
    ping -Q "$tos" -c 12 -i 0.05 -W 1 "$TARGET" >/dev/null 2>&1
    sleep 0.4
    after=($(get))
    best=""; bestd=0
    for i in 0 1 2; do
        d=$(( ${after[$i]} - ${before[$i]} ))
        [ "$d" -gt "$bestd" ] && { bestd=$d; best=$i; }
    done
    case "$best" in 0) t="Bulk" ;; 1) t="Best Effort" ;; 2) t="Voice" ;; *) t="(none seen)" ;; esac
    printf "%-6s %-5s %-8s -> %s (+%d pkts)\n" "$tos" "$dscp" "$name" "$t" "$bestd"
done
