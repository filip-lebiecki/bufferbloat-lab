#!/usr/bin/env bash
# The "web feels snappy" test (run with sudo): how long does a SMALL transfer
# (256 KB, like a web page) take while a bulk upload hogs the link?
# Under pfifo the new flow's handshake and slow-start crawl through the full
# queue; under fq_codel new/sparse flows get priority and finish fast.
# Run once with set-qdisc.sh pfifo, once with fq_codel (or cake).
# Usage: sudo ./sparse-flow.sh
set -euo pipefail

SRV=10.0.2.2

pkill -x iperf3 2>/dev/null || true
sleep 0.2
ip netns exec server iperf3 -s -p 5201 -D
ip netns exec server iperf3 -s -p 5202 -D
sleep 0.3

small_transfer() {
    local label=$1 i T0 T1
    for i in 1 2 3; do
        T0=$(date +%s%N)
        ip netns exec client iperf3 -c $SRV -p 5202 -n 256K >/dev/null
        T1=$(date +%s%N)
        printf '   %s try %d: %d ms\n' "$label" "$i" $(( (T1 - T0) / 1000000 ))
    done
}

echo "== 256 KB transfer, idle link =="
small_transfer idle

ip netns exec client iperf3 -c $SRV -p 5201 -t 30 --logfile /dev/null &
BULK=$!
sleep 3   # let the bulk flow fill the queue

echo "== 256 KB transfer while a bulk upload hogs the link =="
small_transfer loaded

kill $BULK 2>/dev/null || true
wait 2>/dev/null || true
pkill -x iperf3 2>/dev/null || true
