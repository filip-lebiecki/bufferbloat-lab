#!/usr/bin/env bash
# Creates the demo topology inside the Linux VM (run with sudo):
#
#   [client]  10.0.1.2                      10.0.2.2  [server]
#      c-eth ---------- [router] ---------- s-eth
#              r-c  10.0.1.1  10.0.2.1  r-s
#                              ^^^ bottleneck qdisc goes on r-s
#
# The return path (server -> client) gets 20 ms of netem delay so the
# baseline RTT looks like a real internet path instead of 0.05 ms.
set -euo pipefail

for ns in client router server; do
    ip netns del "$ns" 2>/dev/null || true
done

ip netns add client
ip netns add router
ip netns add server

ip link add c-eth type veth peer name r-c
ip link add s-eth type veth peer name r-s

ip link set c-eth netns client
ip link set r-c   netns router
ip link set r-s   netns router
ip link set s-eth netns server

ip netns exec client ip addr add 10.0.1.2/24 dev c-eth
ip netns exec router ip addr add 10.0.1.1/24 dev r-c
ip netns exec router ip addr add 10.0.2.1/24 dev r-s
ip netns exec server ip addr add 10.0.2.2/24 dev s-eth

for ns in client router server; do
    ip netns exec "$ns" ip link set lo up
done
ip netns exec client ip link set c-eth up
ip netns exec router ip link set r-c up
ip netns exec router ip link set r-s up
ip netns exec server ip link set s-eth up

ip netns exec client ip route add default via 10.0.1.1
ip netns exec server ip route add default via 10.0.2.1
ip netns exec router sysctl -qw net.ipv4.ip_forward=1

# veth offloads (TSO/GSO/GRO) let the kernel move 64 KB super-packets,
# which makes shaping at tens-of-mbit meaningless. Turn them off so the
# link behaves like real ethernet.
ip netns exec client ethtool -K c-eth tso off gso off gro off >/dev/null
ip netns exec router ethtool -K r-c   tso off gso off gro off >/dev/null
ip netns exec router ethtool -K r-s   tso off gso off gro off >/dev/null
ip netns exec server ethtool -K s-eth tso off gso off gro off >/dev/null

# 20 ms base RTT, applied on the return path so it never interacts
# with the bottleneck qdisc we are demoing on r-s.
ip netns exec server tc qdisc add dev s-eth root netem delay 20ms limit 10000

echo "Testbed up. Try: sudo ip netns exec client ping -c 3 10.0.2.2"
