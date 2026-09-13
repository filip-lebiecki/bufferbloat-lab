#!/usr/bin/env bash
# HTB borrowing demo (run with sudo, after set-qdisc.sh htb-demo):
# phase 1 - the 10 mbit "bulk" class runs alone and borrows up to ceil (50M);
# phase 2 - the 40 mbit "priority" class wakes up and reclaims its guarantee.
# Usage: sudo ./htb-borrow.sh
set -euo pipefail

SRV=10.0.2.2

pkill -x iperf3 2>/dev/null || true
sleep 0.2
ip netns exec server iperf3 -s -p 5201 -D
ip netns exec server iperf3 -s -p 5202 -D
sleep 0.3

echo "== phase 1: bulk class (guaranteed 10M) alone for 8s =="
ip netns exec client iperf3 -c $SRV -p 5201 -t 16 -i 2 --logfile /tmp/bulk.log &
BULK=$!
sleep 8
echo "== phase 2: priority class (guaranteed 40M) joins =="
ip netns exec client iperf3 -c $SRV -p 5202 -t 8 -i 2 --logfile /tmp/prio.log &
wait $BULK

echo
echo "bulk flow, 2s intervals (watch it drop when priority wakes up):"
grep -E 'sec.*Mbits' /tmp/bulk.log | grep -v receiver | grep -v sender | sed 's/^/   /'
echo "priority flow:"
grep -E 'receiver' /tmp/prio.log | sed 's/^/   /'
rm -f /tmp/bulk.log /tmp/prio.log
pkill -x iperf3 2>/dev/null || true
