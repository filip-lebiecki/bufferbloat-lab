#!/usr/bin/env bash
# Latency-under-load test through the bottleneck (run with sudo).
# Starts an iperf3 upload client->server, pings while it runs, and prints
# idle RTT, loaded RTT and achieved throughput.
# Usage: sudo ./measure.sh [seconds]
set -euo pipefail

DUR=${1:-10}
SRV=10.0.2.2

# iperf3 server (idempotent)
pkill -f 'iperf3 -s' 2>/dev/null || true
ip netns exec server iperf3 -s -D
sleep 0.3

echo "== idle RTT =="
ip netns exec client ping -q -c 5 -i 0.2 $SRV | tail -1

echo
echo "== ${DUR}s TCP upload + ping under load =="
ip netns exec client iperf3 -c $SRV -t "$DUR" -O 1 --logfile /tmp/iperf.log --forceflush &
IPERF=$!
sleep 1.5   # let TCP fill the queue
ip netns exec client ping -q -c $((DUR*4 - 10)) -i 0.25 $SRV | tail -1
wait $IPERF
grep -E 'sender|receiver' /tmp/iperf.log | sed 's/^/   /'
rm -f /tmp/iperf.log

pkill -f 'iperf3 -s' 2>/dev/null || true
