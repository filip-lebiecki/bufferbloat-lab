#!/usr/bin/env bash
# Verifies the htb-demo class split (run with sudo): two greedy TCP flows,
# port 5202 is in the 40 mbit class, everything else in the 10 mbit class.
# Usage: sudo ./htb-test.sh [seconds]
set -euo pipefail

DUR=${1:-10}
SRV=10.0.2.2

pkill -x iperf3 2>/dev/null || true
sleep 0.2
ip netns exec server iperf3 -s -p 5201 -D
ip netns exec server iperf3 -s -p 5202 -D
sleep 0.3

ip netns exec client iperf3 -c $SRV -p 5201 -t "$DUR" --logfile /tmp/bulk.log &
ip netns exec client iperf3 -c $SRV -p 5202 -t "$DUR" --logfile /tmp/prio.log &
wait

echo "bulk flow     (port 5201 -> class 1:20, guaranteed 10M):"
grep receiver /tmp/bulk.log | sed 's/^/   /'
echo "priority flow (port 5202 -> class 1:10, guaranteed 40M):"
grep receiver /tmp/prio.log | sed 's/^/   /'
rm -f /tmp/bulk.log /tmp/prio.log
pkill -x iperf3 2>/dev/null || true
