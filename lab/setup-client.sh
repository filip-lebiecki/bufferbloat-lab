#!/usr/bin/env bash
# Client .32 — the laptop in the story. Run with sudo.
#
# The address (192.168.80.32/23) and the default route via the Linux router
# come from systemd-networkd and survive a reboot. What does not survive:
# IPv6 being off, offloads being off, and the second address chapter 7 needs.
#
# IPv6 matters more than it looks: the client's v6 default route is learned by
# RA from the RB5009 on the shared LAN, and test.libreqos.com is dual-stack.
# Left on, a browser test runs around everything we configure.
set -euo pipefail

DEV=eth0
ROUTER=192.168.80.31
HOST_B=192.168.80.42/23     # chapter 7: "host B" behind the same NAT
SERVER=192.168.12.120

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

echo "== IPv6 off"
sysctl -wq net.ipv6.conf.$DEV.accept_ra=0
sysctl -wq net.ipv6.conf.all.disable_ipv6=1
sysctl -wq net.ipv6.conf.$DEV.disable_ipv6=1
ip -6 route flush dev $DEV 2>/dev/null || true

echo "== offloads off ($DEV)"
ethtool -K $DEV gro off gso off tso off

echo "== clamp the send buffer (deliberate, cosmetic)"
# Invisible until it wrecks a take. Kernel 7.0 defaults tcp_wmem's ceiling to
# 32 MB; against a bloated FIFO, TCP autotunes a send buffer of many MB, and
# iperf3's per-second accounting quantises badly — write() only returns once
# ~half the send buffer drains, so the SENDER's counter moves in gulps of
# sndbuf/2 while the wire is perfectly steady. At 32 MB you get
# "25.2 / 25.2 / 25.2 / 0.00 Bytes"; at 4 MB and a 20 mbit shaper you got a
# 2:1 sawtooth, "23 / 23 / 11".
#
# So this is a DEVIATION FROM THE KERNEL DEFAULT, not a restoration of one.
# 4 MB is what kernel 6.8 shipped (the server still has it), and 4 MB is also
# the measured sweet spot: raising it to 8 MB makes the sender readout WORSE,
# not better. It changes what the numbers look like, not what they are — the
# receiver reads flat throughout either way. See lab/README.md.
#
# At 50 mbit the sawtooth is gone: the sndbuf/2 gulp is ~0.2 s of data instead
# of ~0.6 s, so it no longer aliases against iperf3's 1-second intervals.
# Verified 2026-08-21 in a netns rebuild of this lab.
for kv in "net.ipv4.tcp_wmem=4096 16384 4194304" \
          "net.ipv4.tcp_rmem=4096 131072 6291456"; do
    key=${kv%%=*}; want=${kv#*=}
    have=$(sysctl -n "$key" | tr -s '\t' ' ')
    if [ "$have" != "$want" ]; then
        echo "  $key: $have -> $want (clamped for legible iperf3 output)"
        sysctl -wq "$key=$want"
    fi
done
echo "  stock: $(sysctl -n net.ipv4.tcp_wmem | tr -s '\t' ' ')"

echo "== addresses"
ip addr replace $HOST_B dev $DEV        # secondary; .32 stays primary

echo "== default route via the Linux router"
# Chapter 8 points this at the MikroTik (.40) and leaves it there. Always put
# it back, or the cold open films the MikroTik's cake and the ping never moves.
ip route replace default via $ROUTER dev $DEV

echo
echo "== verify"
ip -br a show $DEV
ip route get $SERVER

echo
ok=1
ip -br a show $DEV | grep -q 192.168.80.32/23 || { echo "MISSING .32" >&2; ok=0; }
ip -br a show $DEV | grep -q 192.168.80.42/23 || { echo "MISSING .42" >&2; ok=0; }
ip route get $SERVER | grep -q "via $ROUTER" || { echo "NOT ROUTING VIA $ROUTER" >&2; ok=0; }
if ping -c 3 -i 0.2 -q $SERVER 2>&1 | tail -1; then :; fi
[ "$ok" = 1 ] && echo "CLIENT READY — expect ~20 ms idle to $SERVER." || { echo "CLIENT NOT READY" >&2; exit 1; }
