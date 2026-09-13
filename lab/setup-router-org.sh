#!/usr/bin/env bash
# Router .31 — chapter 17, "Proof, on the real internet": the baseline half.
# Run with sudo.
#
# The villain at matched rates. 100 down / 50 up enforced by HTB, with a dumb
# 1000-packet pfifo under each — same bandwidth as the cake run, so the only
# thing that differs between the two columns of the table is what happens once
# the pipe is full.
#
#   sudo ./setup-router-org.sh
#       apply the baseline. Run the test, then switch to the other half:
#       sudo ./setup-router-cake.sh
#
#   sudo ./setup-router-org.sh off
#       tear it all down, leave the WAN bare. Cleanup, not a test state.
#
#   DOWN=270mbit UP=40mbit sudo ./setup-router-org.sh
#       match whatever rates you gave setup-router-cake.sh. The comparison is
#       only honest if both halves run at the same numbers.
#
# The masquerade block and the ifb plumbing below are duplicated verbatim in
# setup-router-cake.sh, on purpose: every script in this directory has to run
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
# 1000 packets is the lab's cold-open villain. At 1500-byte packets that is
# 12 Mbit of standing queue: ~240 ms at 50 mbit up, ~120 ms at 100 mbit down.
FIFO_LIMIT=${FIFO_LIMIT:-1000}

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
    echo "ROUTER BARE — no shaper on $WAN. Not a test state."
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

# HTB rate + a dumb FIFO under it. HTB is scaffolding — it makes the link
# behave like a rate-limited uplink. The pfifo is the villain. No stab/overhead
# here, deliberately: this is the same naive setup as the lab's cold open, and
# it is what an untuned box in the wild actually looks like. It also means this
# side is nominally a hair faster on the wire than cake with `ethernet`, which
# biases against cake rather than for it.
fifo_on() {
    local dev=$1 rate=$2
    tc qdisc add dev "$dev" root handle 1: htb default 10
    tc class add dev "$dev" parent 1: classid 1:10 htb rate "$rate" ceil "$rate"
    tc qdisc add dev "$dev" parent 1:10 handle 10: pfifo limit "$FIFO_LIMIT"
}

echo "== upload: htb $UP + pfifo limit $FIFO_LIMIT on $WAN"
fifo_on "$WAN" "$UP"

echo "== ifb plumbing: turning the download into an upload"
# There is no such thing as a download queue — that holds whether what you
# hang on the far end is cake or a dumb FIFO.
ip link add ifb0 type ifb
ip link set ifb0 up
tc qdisc add dev "$WAN" handle ffff: ingress
tc filter add dev "$WAN" parent ffff: matchall action mirred egress redirect dev ifb0

echo "== download: htb $DOWN + pfifo limit $FIFO_LIMIT on ifb0"
fifo_on ifb0 "$DOWN"

echo
echo "== verify"
tc qdisc show dev "$WAN"
tc qdisc show dev ifb0
tc filter show dev "$WAN" parent ffff:

echo
if tc qdisc show dev "$WAN" | grep -q 'pfifo 10:' && tc qdisc show dev ifb0 | grep -q 'pfifo 10:'; then
    echo "ROUTER BASELINE — $DOWN down / $UP up, dumb FIFO both directions."
    echo "Run the test from the client:  libreqos-test --no-submit"
    echo "Then the other half:           sudo ./setup-router-cake.sh"
else
    echo "ROUTER NOT READY — no pfifo on $WAN or ifb0" >&2
    exit 1
fi
