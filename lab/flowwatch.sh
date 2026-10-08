#!/usr/bin/env bash
# Router .31 — one line per second: sparse/bulk flow gauges and the Best Effort
# delays. No root needed.
#
#   ./flowwatch.sh            26 samples on eth0
#   ./flowwatch.sh 60 ifb0    60 samples on the download queue
#
# Read the delay pair, not the flow count. sp_flows is an instantaneous gauge:
# a ping that is never dropped leaves the sparse list the moment its packet is
# sent, ~100 µs at 50 Mbit, so a 1 Hz sample essentially never catches it.
# What does show at 1 Hz is the gap between av_delay (the whole tin) and
# sp_delay (its low end — where sparse packets live). For a steady sp_flows 1,
# run a high-rate flow that stays under its fair share instead:
#   iperf3 -c 192.168.12.120 -p 5204 -B 192.168.80.32 -u -b 8M -l 100 -t 3600
N=${1:-26}
DEV=${2:-eth0}
printf "%5s  %-14s %-16s %s\n" t "Bulk sp/bk" "BestEff sp/bk" "BE av_delay / sp_delay"
for i in $(seq 1 "$N"); do
    tc -s qdisc show dev "$DEV" | awk -v t="$i" '
        /^  sp_flows/ { sp1 = $2; sp2 = $3 }
        /^  bk_flows/ { bk1 = $2; bk2 = $3 }
        /^  av_delay/ { av  = $3 }
        /^  sp_delay/ { spd = $3 }
        END { printf "%5s  %-14s %-16s %s / %s\n", t "s", sp1 "/" bk1, sp2 "/" bk2, av, spd }'
    sleep 1
done
