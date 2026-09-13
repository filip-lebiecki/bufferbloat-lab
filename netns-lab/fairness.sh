#!/usr/bin/env bash
# The "hostile neighbor" test (run with sudo): a 60 mbit UDP flood (more
# than the 50 mbit link!) competes with a normal TCP upload + ping.
# With pfifo the flood owns the queue; with sfq/fq_codel/cake each flow
# gets its fair share and ping barely notices.
# Usage: sudo ./fairness.sh [seconds]
set -euo pipefail

DUR=${1:-10}
SRV=10.0.2.2

pkill -f 'iperf3 -s' 2>/dev/null || true
ip netns exec server iperf3 -s -p 5201 -D
ip netns exec server iperf3 -s -p 5202 -D
sleep 0.3

ip netns exec client iperf3 -c $SRV -p 5201 -u -b 60M -t "$DUR" \
    --logfile /tmp/udp.log --forceflush &
UDP=$!
ip netns exec client iperf3 -c $SRV -p 5202 -t "$DUR" \
    --logfile /tmp/tcp.log --forceflush &
TCP=$!
sleep 1.5
echo "== ping while UDP flood (60M) + TCP upload fight over 50 mbit =="
ip netns exec client ping -q -c $((DUR*4 - 10)) -i 0.25 $SRV | tail -2
wait $UDP $TCP || true

echo
echo "UDP flood got:"; grep -E '0.00-.*receiver' /tmp/udp.log | sed 's/^/   /'
echo "TCP upload got:"; grep -E 'receiver' /tmp/tcp.log | sed 's/^/   /'
rm -f /tmp/udp.log /tmp/tcp.log
pkill -f 'iperf3 -s' 2>/dev/null || true
