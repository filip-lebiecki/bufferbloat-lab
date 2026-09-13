#!/usr/bin/env bash
# The "roommate with 8 torrents" test (run with sudo): host A opens 8
# parallel flows, host B opens 1. Per-FLOW fairness (fq_codel) gives A
# 8/9 of the link; cake's per-HOST isolation splits it 50/50.
# Run once with set-qdisc.sh fq_codel, once with cake.
# Usage: sudo ./host-isolation.sh [seconds]
set -euo pipefail

DUR=${1:-10}
SRV=10.0.2.2

# host B = second IP on the client veth (idempotent)
ip netns exec client ip addr add 10.0.1.3/24 dev c-eth 2>/dev/null || true

pkill -x iperf3 2>/dev/null || true
sleep 0.2
ip netns exec server iperf3 -s -p 5201 -D
ip netns exec server iperf3 -s -p 5202 -D
sleep 0.3

ip netns exec client iperf3 -c $SRV -p 5201 -B 10.0.1.2 -P 8 -t "$DUR" \
    --logfile /tmp/roommate.log &
ip netns exec client iperf3 -c $SRV -p 5202 -B 10.0.1.3 -t "$DUR" \
    --logfile /tmp/you.log &
wait

echo "roommate (10.0.1.2, 8 flows):"
grep -E 'SUM.*receiver' /tmp/roommate.log | sed 's/^/   /'
echo "you      (10.0.1.3, 1 flow):"
grep -E 'receiver' /tmp/you.log | sed 's/^/   /'
rm -f /tmp/roommate.log /tmp/you.log
pkill -x iperf3 2>/dev/null || true
