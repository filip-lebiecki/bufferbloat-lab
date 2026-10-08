#!/usr/bin/env bash
# Router .31 — one switch between "bad modem" and cake. Run with sudo.
# This is the command behind every `set-modem.sh bloat` / `set-modem.sh cake`
# in the CAKE video (docs/cake-explained.md).
#
# Real lines often do not show bufferbloat at all (fiber into a gigabit port,
# an upload policer that drops instead of queueing). So this box plays the
# kind of line that does: a rate limit with a modem-sized buffer.
#
#   sudo ./set-modem.sh bloat   100 down / 50 up, HTB + 1000-packet pfifo.
#                               What a cheap DSL/cable/LTE modem does.
#   sudo ./set-modem.sh cake    same 100 / 50, cake both directions — the
#                               six lines from the video.
#   sudo ./set-modem.sh off     tear it down, WAN bare. Not a test state.
#   sudo ./set-modem.sh show    print what is on the WAN and ifb0 right now.
#
#   DOWN=270mbit UP=40mbit sudo ./set-modem.sh bloat
#       rates pass through to both halves; use the same numbers for both or
#       the comparison is not honest.
#
# The rate limit alone is NOT what makes "bloat" bad — the deep FIFO is. A cap
# that drops instead of queueing (tbf with a small limit, an ISP policer)
# grades fine. That is why bloat sets pfifo limit 1000 explicitly, on ifb0 too:
# htb's default leaf takes its length from the device, and ifb0's is 32.
#
# A thin wrapper on purpose: setup-router-org.sh (bloat) and
# setup-router-cake.sh (cake) already carry the plumbing, and a third copy of
# it would be one more thing to drift. Unlike the setup scripts this one is
# therefore NOT standalone — the two scripts must sit next to it
# (deploy-scripts.sh pushes all three).
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
WAN=${WAN:-eth0}

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

case "${1:-show}" in
    bloat) exec ./setup-router-org.sh ;;
    cake)  exec ./setup-router-cake.sh ;;
    off)   exec ./setup-router-cake.sh off ;;
    show)
        tc qdisc show dev "$WAN"
        tc qdisc show dev ifb0 2>/dev/null || echo "(no ifb0 — download unshaped)"
        ;;
    *) echo "usage: $0 {bloat|cake|off|show}" >&2; exit 1 ;;
esac
