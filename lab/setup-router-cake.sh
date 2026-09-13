#!/usr/bin/env bash
# Router .31 — chapter 17, "Proof, on the real internet": the cake half.
# Run with sudo.
#
# Cake in both directions on a real WAN, at the rates that match the lab:
# 100 down / 50 up. This is the "Linux cake 100 / 50" column of the table.
#
#   sudo ./setup-router-cake.sh
#       apply.
#
#   sudo ./setup-router-cake.sh off
#       tear it all down, leave the WAN bare. Cleanup only — the baseline
#       for the table is setup-router-org.sh, not a bare line.
#
#   DOWN=270mbit UP=40mbit sudo ./setup-router-cake.sh
#       your own line. Measure it first, then shape to ~90% of what it
#       actually delivers, not what the ISP sells you.
#
# The masquerade block and the ifb plumbing below are duplicated verbatim in
# setup-router-org.sh, on purpose: every script in this directory has to run
# standalone on a box that only has that one file. If you change the plumbing
# here, change it there too.
#
# Everything here is runtime-only — a reboot wipes it. Addresses, routes and
# ip_forward come from systemd-networkd and sysctl.d and are already right at
# boot.
#
# Offloads are deliberately left alone. The lab script turns GRO/GSO/TSO off
# for reproducible packet counts on the qdisc; here we want honest throughput
# numbers off a real line.
set -euo pipefail

WAN=${WAN:-eth0}
DOWN=${DOWN:-100mbit}
UP=${UP:-50mbit}

MODE=${1:-on}
case "$MODE" in on|off) ;; *) echo "usage: $0 [off]" >&2; exit 1 ;; esac

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
ip link show "$WAN" >/dev/null 2>&1 || { echo "no such interface: $WAN" >&2; exit 1; }

echo "== clearing anything left from a previous session"
tc qdisc del dev "$WAN" root    2>/dev/null || true
tc qdisc del dev "$WAN" ingress 2>/dev/null || true
tc qdisc del dev ifb0 root      2>/dev/null || true
ip link del ifb0                2>/dev/null || true

if [ "$MODE" = off ]; then
    echo
    tc qdisc show dev "$WAN"
    echo
    echo "ROUTER BARE — no shaper on $WAN."
    echo "For the chapter 17 baseline:  sudo ./setup-router-org.sh"
    exit 0
fi

echo "== forwarding"
sysctl -wq net.ipv4.ip_forward=1

echo "== masquerade on $WAN"
# idempotent: add table/chain unconditionally (nft add is a no-op if they
# already exist), then add the rule only if it isn't there, so re-running
# doesn't stack duplicates.
nft add table ip nat
nft 'add chain ip nat postrouting { type nat hook postrouting priority srcnat ; }'
if ! nft list chain ip nat postrouting | grep -q "oifname \"$WAN\" masquerade"; then
    nft add rule ip nat postrouting oifname "$WAN" masquerade
fi

echo "== upload: cake $UP on $WAN"
# nat dual-srchost — fair share per LAN host, seen through the masquerade
# ethernet         — account for the 38 bytes of framing cake would otherwise
#                    ignore, so $UP means $UP on the wire
# ack-filter       — drop redundant acks on the narrow direction
tc qdisc replace dev "$WAN" root cake bandwidth "$UP" nat dual-srchost ethernet ack-filter

echo "== ifb plumbing: turning the download into an upload"
# There is no such thing as a download queue. Redirect everything arriving on
# the WAN onto a virtual card, where it counts as leaving, and queue it there.
ip link add ifb0 type ifb
ip link set ifb0 up
tc qdisc add dev "$WAN" handle ffff: ingress
tc filter add dev "$WAN" parent ffff: matchall action mirred egress redirect dev ifb0

echo "== download: cake $DOWN on ifb0"
# dual-dsthost, not dual-srchost: coming in, the host that matters is the one
# the packet is *for*.
tc qdisc replace dev ifb0 root cake bandwidth "$DOWN" nat dual-dsthost ethernet ingress

echo
echo "== verify"
tc qdisc show dev "$WAN"
tc qdisc show dev ifb0
tc filter show dev "$WAN" parent ffff:

echo
if tc qdisc show dev "$WAN" | grep -q cake && tc qdisc show dev ifb0 | grep -q cake; then
    echo "ROUTER SHAPED — $DOWN down / $UP up, cake both directions."
    echo "Run the test from the client:  libreqos-test --no-submit"
    echo "Baseline for comparison:       sudo ./setup-router-org.sh"
else
    echo "ROUTER NOT READY — no cake on $WAN or ifb0" >&2
    exit 1
fi
