#!/usr/bin/env bash
# Client .32 — start the traffic for each chapter of the CAKE video in one go.
# No root needed. Everything runs in the background and logs to $LOGDIR.
#
#   ./load.sh intro        the cold open: upload, LE "torrent", call, ping,
#                          and the roommate's 8 streams
#   ./load.sh standing     problem 1: one upload + ping
#   ./load.sh bully        problem 3: one TCP upload + a 50M UDP flood
#   ./load.sh roommate     problem 4: 8 streams from .32, 1 from .42
#   ./load.sh tins         problem 5: two normal uploads + one LE (Bulk tin)
#   ./load.sh voice        problem 5: one upload + a 1M UDP stream marked EF
#   ./load.sh cheat        problem 5: one upload + a greedy TCP marked EF
#   ./load.sh sparse       problem 2: one upload + a 9.4 Mbit unmarked stream
#   ./load.sh bothways     problem 6: one upload + one download at once
#   ./load.sh dashboard    chapter 11: upload, LE upload, ping, EF call
#
#   ./load.sh status       last line of every running test
#   ./load.sh stop         kill everything this script started
#
# Starting a scenario stops the previous one first. Switch the router with
# `sudo ./set-modem.sh bloat|cake` on .31 while a scenario is running — that
# is the before/after in the video — and read the queue with
# `watch -n1 tc -s qdisc show dev eth0` or ./tins.sh.
#
# Needs on the server: iperf3 on 5201-5204 and `irtt server` (setup-server.sh).
# One iperf3 listener runs one test at a time, which is why every concurrent
# stream below has its own port.
set -uo pipefail

SERVER=${SERVER:-192.168.12.120}
A=${A:-192.168.80.32}          # you
B=${B:-192.168.80.42}          # your roommate (second address, setup-client.sh)
T=${T:-3600}                   # seconds; `stop` ends it sooner
LOGDIR=${LOGDIR:-/tmp/cake-load}

mkdir -p "$LOGDIR"

# run NAME CMD... — start CMD in the background, line-buffered, into NAME.log
run() {
    local name=$1; shift
    echo "  $name: $*"
    setsid stdbuf -oL "$@" >"$LOGDIR/$name.log" 2>&1 </dev/null &
    echo $! >"$LOGDIR/$name.pid"
}

up()   { run "$1" iperf3 -c "$SERVER" -p "$2" -B "$A" -t "$T" --forceflush "${@:3}"; }

stop() {
    local p
    for p in "$LOGDIR"/*.pid; do
        [ -e "$p" ] || continue
        kill -- -"$(cat "$p")" 2>/dev/null || kill "$(cat "$p")" 2>/dev/null
        rm -f "$p"
    done
    # the server side of an iperf3 test needs a moment to free its port
    sleep 1
}

status() {
    local l
    for l in "$LOGDIR"/*.log; do
        [ -e "$l" ] || continue
        [ -e "${l%.log}.pid" ] || continue
        printf "%-12s %s\n" "$(basename "$l" .log)" \
            "$(grep -v '^ *$' "$l" | grep -v 'Interval\|Connecting\|local .* port' | tail -n1)"
    done
}

ping_() { run ping ping -i 0.5 -w "$T" "$SERVER"; }
call()  { run call irtt client -i 20ms -l 172 -d "${T}s" --dscp=0xb8 "$SERVER"; }

case "${1:-}" in
    stop)   stop; echo "stopped." ; exit 0 ;;
    status) status; exit 0 ;;
    "")     sed -n '2,/^set -uo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 1 ;;
esac

stop
echo "== ${1}"
case "$1" in
    intro)
        up upload    5201
        up torrent   5203 --tos 0x04                 # DSCP 1, LE -> Bulk tin
        call
        ping_
        run roommate iperf3 -c "$SERVER" -p 5202 -B "$B" -P 8 -t "$T" --forceflush
        ;;
    standing)
        up upload 5201
        ping_
        ;;
    sparse)
        up upload 5201
        # 1400 B every 1 ms ~ 9.4 Mbit, unmarked: under its fair share of ~25
        run sparse irtt client -i 1ms -l 1400 -d "${T}s" "$SERVER"
        ;;
    bully)
        up tcp   5201
        up flood 5202 -u -b 50M                      # no congestion control
        ;;
    roommate)
        up you       5201 -P 8
        run roommate iperf3 -c "$SERVER" -p 5202 -B "$B" -t "$T" --forceflush
        ;;
    tins)
        up normal1 5201
        up normal2 5202
        up torrent 5203 --tos 0x04                   # DSCP 1, LE -> Bulk tin
        ;;
    voice)
        up upload 5201
        up voice  5202 --tos 0xb8 -u -b 1M           # EF, well under thresh
        ;;
    cheat)
        up upload 5201
        up fakevoice 5202 --tos 0xb8                 # EF, but greedy TCP
        ;;
    bothways)
        up upload   5201
        up download 5202 -R
        ;;
    dashboard)
        up upload  5201
        up torrent 5203 --tos 0x04
        ping_
        call
        ;;
    *)
        echo "unknown scenario: $1 (run with no arguments for the list)" >&2
        exit 1
        ;;
esac

echo
echo "logs: $LOGDIR/*.log    ./load.sh status    ./load.sh stop"
