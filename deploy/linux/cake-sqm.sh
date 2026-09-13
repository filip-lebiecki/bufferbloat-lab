#!/usr/bin/env bash
# SQM for a Linux box acting as your router — cake in both directions.
#
# Set the rates, run as root, done. Run it again anytime to re-apply; it is
# idempotent and safe to re-run on a live link.
#
#   WAN=eth0 UP=45mbit DOWN=90mbit sudo ./cake-sqm.sh
#   sudo ./cake-sqm.sh status        # what is actually on the wire right now
#   sudo ./cake-sqm.sh off           # remove everything, leave the WAN bare
#
# UP/DOWN: ~90% of the rates a speed test shows on an IDLE line — measured,
#          not the tier you pay for. Shaping slightly BELOW line rate is the
#          whole trick: it moves the queue off the ISP's equipment and onto
#          yours, where cake can manage it. You spend ~10% of the link to own
#          the queue.
#
# OVERHEAD: the per-packet link-layer framing cake should account for.
#          ethernet (plain ethernet/fibre) | docsis (cable) |
#          pppoe-vcmux (DSL over PPPoE)   | conservative (don't know, be safe)
#          Get this wrong on the low side and you type 50 and put 51 on the
#          wire — and hand the queue straight back to your ISP.
#
# ACK_FILTER: worth 5-10% of your upload on a lopsided line (say 500/25).
#          On a symmetric-ish line it does close to nothing. "on" | "off".
#
# Persist it: see cake-sqm.service next to this script, or docs/01-linux-router.md.
set -euo pipefail

WAN=${WAN:-eth0}                   # the interface your ISP is on (ppp0 for PPPoE)
UP=${UP:-45mbit}                   # upload shape rate
DOWN=${DOWN:-90mbit}               # download shape rate
OVERHEAD=${OVERHEAD:-ethernet}     # ethernet | docsis | pppoe-vcmux | conservative
ACK_FILTER=${ACK_FILTER:-on}       # on | off
IFB=${IFB:-ifb-sqm}                # virtual device the download gets shaped on

MODE=${1:-on}
case "$MODE" in on|off|status) ;; *) echo "usage: $0 [on|off|status]" >&2; exit 1 ;; esac

if [ "$MODE" = status ]; then
    echo "== $WAN (egress — your uploads)"
    tc -s qdisc show dev "$WAN"
    echo
    echo "== $IFB (ingress — your downloads)"
    tc -s qdisc show dev "$IFB" 2>/dev/null || echo "   (no ingress shaper)"
    exit 0
fi

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
ip link show "$WAN" >/dev/null 2>&1 || { echo "no such interface: $WAN" >&2; exit 1; }

# Start from a known state either way: re-applying must not stack a second
# ingress filter on top of the first.
tc qdisc del dev "$WAN" root    2>/dev/null || true
tc qdisc del dev "$WAN" ingress 2>/dev/null || true
tc qdisc del dev "$IFB" root    2>/dev/null || true

if [ "$MODE" = off ]; then
    ip link del "$IFB" 2>/dev/null || true
    echo "SQM removed — $WAN is bare."
    tc qdisc show dev "$WAN"
    exit 0
fi

ack=ack-filter
[ "$ACK_FILTER" = off ] && ack=no-ack-filter

# --- uploads: cake replaces whatever is on the WAN root -------------------
# nat            look through conntrack, so per-host fairness sees the LAN
#                address that sent the packet and not the router's public IP.
#                Without it, every machine behind the NAT looks like one host.
# dual-srchost   fair share per LAN machine first, then per flow inside it.
#                Going out, the host that matters is the one that SENT it.
tc qdisc replace dev "$WAN" root cake bandwidth "$UP" \
    nat dual-srchost "$OVERHEAD" $ack

# --- downloads: there is no such thing as a download queue ----------------
# A qdisc only controls traffic LEAVING an interface, and downloads arrive.
# So redirect everything arriving on the WAN onto a virtual card, where the
# kernel considers it to be leaving, and put cake on that.
modprobe ifb numifbs=0 2>/dev/null || true
ip link add "$IFB" type ifb 2>/dev/null || true
ip link set "$IFB" up
tc qdisc add dev "$WAN" handle ffff: ingress
tc filter add dev "$WAN" parent ffff: matchall \
    action mirred egress redirect dev "$IFB"

# ingress        the bytes were already spent crossing the real link before we
#                saw them, so count what we drop against the rate too.
# dual-dsthost   coming in, the host that matters is the one it is FOR.
tc qdisc replace dev "$IFB" root cake bandwidth "$DOWN" \
    ingress nat dual-dsthost "$OVERHEAD"

echo
tc qdisc show dev "$WAN"
tc qdisc show dev "$IFB"
echo
if tc qdisc show dev "$WAN" | grep -q cake && tc qdisc show dev "$IFB" | grep -q cake; then
    echo "SQM active on $WAN — $DOWN down / $UP up (overhead: $OVERHEAD, $ack)"
    echo "Now test it from a wired client: https://www.waveform.com/tools/bufferbloat"
else
    echo "SQM NOT active — no cake on $WAN or $IFB" >&2
    exit 1
fi
