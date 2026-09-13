#!/usr/bin/env bash
# Download-direction bufferbloat + the real-world fix (run with sudo).
#
# The "ISP modem" is the router's egress toward the client: 50 mbit with a
# dumb 1000-packet FIFO you cannot touch (in real life it's the ISP's box).
# The fix runs entirely on the CLIENT: redirect ingress traffic through an
# ifb device and let cake shape it to 45 mbit — just below the ISP rate, so
# the queue forms where cake controls it, not in the modem.
# This is exactly what OpenWrt SQM does for your downloads.
# Usage: sudo ./ingress-demo.sh [seconds]
set -euo pipefail

DUR=${1:-10}
SRV=10.0.2.2

download_test() {
    pkill -x iperf3 2>/dev/null || true
    sleep 0.2
    ip netns exec server iperf3 -s -D
    sleep 0.3
    ip netns exec client iperf3 -c $SRV -R -t "$DUR" \
        --logfile /tmp/dl.log --forceflush &
    local PID=$!
    sleep 1.5
    ip netns exec client ping -q -c $((DUR*4 - 10)) -i 0.25 $SRV | tail -1
    wait $PID
    grep receiver /tmp/dl.log | sed 's/^/   /'
    rm -f /tmp/dl.log
    pkill -x iperf3 2>/dev/null || true
}

# the untouchable "ISP modem" queue
ip netns exec router tc qdisc del dev r-c root 2>/dev/null || true
ip netns exec router tc qdisc add dev r-c root handle 1: htb default 10 r2q 1000
ip netns exec router tc class add dev r-c parent 1: classid 1:10 htb rate 50mbit ceil 50mbit
ip netns exec router tc qdisc add dev r-c parent 1:10 pfifo limit 1000

# start clean on the client
ip netns exec client tc qdisc del dev c-eth ingress 2>/dev/null || true
ip netns exec client ip link del ifb0 2>/dev/null || true

echo "== BEFORE: download through the ISP's dumb FIFO =="
download_test

echo
echo "== applying client-side SQM (ifb + cake ingress @ 45mbit) =="
ip netns exec client ip link add ifb0 type ifb
ip netns exec client ip link set ifb0 up
ip netns exec client tc qdisc add dev c-eth handle ffff: ingress
ip netns exec client tc filter add dev c-eth parent ffff: matchall \
    action mirred egress redirect dev ifb0
ip netns exec client tc qdisc add dev ifb0 root cake bandwidth 45mbit ingress besteffort

echo
echo "== AFTER: same download, queue now lives in cake =="
download_test
