#!/usr/bin/env bash
# Server .33 (192.168.12.120) — plays "the internet" and "the ISP's modem".
# Run with sudo.
#
# Two jobs:
#   1. iperf3 listeners on 5201 (all chapters) and 5202 (chapter 7 host B),
#      plus 5203-5204 and an irtt server for the CAKE video, which runs up to
#      four iperf3 tests side by side (one listener serves one test at a time)
#   2. the emulated last mile on eth0 egress: 20 ms of internet distance,
#      then a 100 mbit pipe with a dumb 1.5 MB buffer.
#
# Order matters and is physically honest: distance first (the far internet),
# then the bottleneck (your slow last mile).
set -euo pipefail

WAN=eth0        # 192.168.12.120, faces the router. eth1 is mgmt only.

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

echo "== offloads off ($WAN)"
ethtool -K $WAN gro off gso off tso off

echo "== iperf3 listeners"
pkill -x iperf3 2>/dev/null || true
sleep 0.3
iperf3 -s -D            # 5201
iperf3 -s -p 5202 -D    # chapter 7: host B's server
iperf3 -s -p 5203 -D    # CAKE video: the LE "torrent"
iperf3 -s -p 5204 -D    # CAKE video: spare, for a fourth concurrent stream
sleep 0.3

echo "== irtt server (the simulated voice call)"
# -i 0: no minimum send interval. irtt's default floor is 10 ms, and the
# sparse-flow demo sends every 1 ms. setsid --fork, not nohup &: the latter
# dies with the ssh session.
if command -v irtt >/dev/null; then
    pkill -x irtt 2>/dev/null || true
    setsid --fork irtt server -i 0 >/dev/null 2>&1 </dev/null
else
    echo "   irtt not installed (apt install irtt) — the call demos need it"
fi

echo "== emulated ISP modem on $WAN"
tc qdisc del dev $WAN root 2>/dev/null || true
tc qdisc add dev $WAN root handle 1: netem delay 20ms limit 100000
tc qdisc add dev $WAN parent 1: handle 2: tbf rate 100mbit burst 15k limit 1500000

echo "== the internet has no route back to a private address"
# Chapter 3's premise, made true. This box has a second leg on the client's
# LAN (eth1, 192.168.80.33/23), so without these routes it answers an
# unmasqueraded client *directly* — in 0.4 ms, on a path that skips the router
# under test entirely. The client then appears to have working internet with
# no NAT at all, which is impossible to explain on camera in one sentence.
# Blackholing the lab client's two addresses makes the unmasqueraded case fail
# cleanly, the way the real internet fails for an RFC1918 source.
ip route replace blackhole 192.168.80.32
ip route replace blackhole 192.168.80.42

echo
echo "== verify"
tc qdisc show dev $WAN
pgrep -a iperf3 || true
pgrep -a irtt || true

echo
if tc qdisc show dev $WAN | grep -q netem && [ "$(pgrep -xc iperf3)" -ge 4 ] \
   && ip route show | grep -q "blackhole 192.168.80.32"; then
    echo "SERVER READY — 20 ms + 100 mbit, iperf3 on 5201-5204,"
    echo "               no route back to an unmasqueraded client."
else
    echo "SERVER NOT READY" >&2
    exit 1
fi
