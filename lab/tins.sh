#!/usr/bin/env bash
# Router .31 — the cake tin table and nothing else. No root needed.
#
#   ./tins.sh              eth0, the upload queue
#   ./tins.sh ifb0         the download queue
#   watch -n1 ./tins.sh    the live-counter view
#
# `tc -s qdisc show` prints one column per tin — Bulk, Best Effort, Voice with
# the default diffserv3. This keeps only the rows worth reading (thresholds,
# the three delays, backlog, counters, drops and the flow gauges) so the table
# fits next to an iperf3 window.
tc -s qdisc show dev "${1:-eth0}" | awk '
/^ +Bulk/ && !hdr { print; hdr = 1 }
/^  (thresh|target|interval|pk_delay|av_delay|sp_delay|backlog|pkts|bytes|drops|marks|ack_drop|sp_flows|bk_flows|way_cols)/ {
    printf "%-10s", $1
    for (i = 2; i <= NF; i++) printf " %14s", $i
    printf "\n" }'
