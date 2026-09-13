#!/usr/bin/env bash
# ECN demo (run with sudo, with cake on the bottleneck): instead of DROPPING
# packets to signal "slow down", the AQM MARKS them (CE bit) and the receiver
# echoes the signal back — congestion control with zero packet loss.
# Runs the same upload twice: ECN off (drops + retransmits) vs ECN on
# (marks, no retransmits). Look at cake's per-tin drop/mark counters.
# Usage: sudo ./ecn-demo.sh [seconds]
set -euo pipefail

DUR=${1:-10}
SRV=10.0.2.2

run_upload() {
    pkill -x iperf3 2>/dev/null || true
    sleep 0.2
    ip netns exec server iperf3 -s -D
    sleep 0.3
    # reset cake counters (replace keeps stats; del+add zeroes them)
    ip netns exec router tc qdisc del dev r-s root 2>/dev/null || true
    ip netns exec router tc qdisc add dev r-s root cake bandwidth 50mbit
    ip netns exec client iperf3 -c $SRV -t "$DUR" --logfile /tmp/ecn.log
    grep sender /tmp/ecn.log | sed 's/^/   /'
    echo "   cake counters:"
    ip netns exec router tc -s qdisc show dev r-s \
        | grep -E '^\s+(drops|marks)' | sed 's/^/   /'
    rm -f /tmp/ecn.log
    pkill -x iperf3 2>/dev/null || true
}

echo "== ECN OFF: congestion signalled by dropping =="
ip netns exec client sysctl -qw net.ipv4.tcp_ecn=0
ip netns exec server sysctl -qw net.ipv4.tcp_ecn=0
run_upload

echo
echo "== ECN ON: congestion signalled by marking =="
ip netns exec client sysctl -qw net.ipv4.tcp_ecn=1
ip netns exec server sysctl -qw net.ipv4.tcp_ecn=1
run_upload

# back to the kernel default (accept if asked, don't initiate)
ip netns exec client sysctl -qw net.ipv4.tcp_ecn=2
ip netns exec server sysctl -qw net.ipv4.tcp_ecn=2
