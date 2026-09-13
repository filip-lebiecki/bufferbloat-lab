#!/usr/bin/env bash
# Router .31 — bring the lab up from a cold boot. Run with sudo.
#
# Everything tc and nft does here is runtime-only: a reboot wipes it, which
# is why this script exists. Addresses and ip_forward come from
# systemd-networkd and sysctl.d and are already correct at boot.
#
# Leaves eth0 staged with the COLD OPEN bottleneck: a 50 mbit HTB class with
# a dumb 1000-packet FIFO under it. That is the villain the cold open films.
#
#   SKIP_NAT=1 sudo ./setup-router.sh
#       skips the masquerade rule so chapter 3 can show `lsmod | grep -c
#       nf_conntrack` going 0 -> 3 on camera. That beat only works on a
#       router that has never had NAT since boot.
set -euo pipefail

WAN=eth0
LAN=eth1
RATE=50mbit

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

echo "== forwarding"
sysctl -wq net.ipv4.ip_forward=1

echo "== offloads off ($WAN, $LAN)"
# reproducible packet counts on the qdisc: no 64 KB superpackets
ethtool -K $WAN gro off gso off tso off
ethtool -K $LAN gro off gso off tso off

echo "== masquerade"
if [ "${SKIP_NAT:-0}" = 1 ]; then
    nft list table ip nat >/dev/null 2>&1 && nft delete table ip nat
    echo "   skipped (SKIP_NAT=1) — conntrack beat is armed:"
    echo "   lsmod | grep -c nf_conntrack   should print 0"
else
    # idempotent: add table/chain unconditionally (nft add is a no-op if they
    # exist), then add the rule only if it isn't already there, so re-running
    # doesn't stack duplicates.
    nft add table ip nat
    nft 'add chain ip nat postrouting { type nat hook postrouting priority srcnat ; }'
    if ! nft list chain ip nat postrouting | grep -q "oifname \"$WAN\" masquerade"; then
        nft add rule ip nat postrouting oifname $WAN masquerade
    fi
fi

echo "== clearing any qdisc left from a previous session"
tc qdisc del dev $WAN root    2>/dev/null || true
tc qdisc del dev $WAN ingress 2>/dev/null || true
tc qdisc del dev ifb0 root    2>/dev/null || true
ip link del ifb0              2>/dev/null || true
# ifb0 is chapter 6's; it gets built on camera there, not here.

echo "== staging the cold-open bottleneck on $WAN"
# Lines 1-2 are scaffolding — they make a 10G card behave like a 50 mbit
# uplink. Line 3 is the villain: 1000 packets of dumb FIFO = 242 ms of
# standing queue at 50 mbit.
tc qdisc add dev $WAN root handle 1: htb default 10
tc class add dev $WAN parent 1: classid 1:10 htb rate $RATE ceil $RATE
tc qdisc add dev $WAN parent 1:10 handle 10: pfifo limit 1000

echo
echo "== verify"
tc qdisc show dev $WAN
nft list table ip nat 2>/dev/null | sed -n '/postrouting/,$p' || echo "  (no nat table — SKIP_NAT)"
echo "nf_conntrack modules loaded: $(lsmod | grep -c nf_conntrack)"

echo
if tc qdisc show dev $WAN | grep -q 'pfifo 10:'; then
    echo "ROUTER READY — eth0 is a 50 mbit uplink with a dumb FIFO."
    echo "Tear this down before shooting chapter 4:  sudo tc qdisc del dev eth0 root"
else
    echo "ROUTER NOT READY — no pfifo on $WAN" >&2
    exit 1
fi
