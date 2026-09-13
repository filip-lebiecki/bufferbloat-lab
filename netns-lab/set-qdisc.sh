#!/usr/bin/env bash
# Swap the bottleneck qdisc on the router's egress (r-s).
# Usage: sudo ./set-qdisc.sh {pfifo|sfq|fq_codel|cake|cake-host|htb-demo|show}
#
# pfifo/sfq/fq_codel are pure queue algorithms, not shapers, so they sit
# under an HTB class that enforces the 50 mbit bottleneck — exactly how
# you'd deploy them on a real router. cake shapes by itself.
set -euo pipefail

RATE=50mbit
DEV=r-s
run() { ip netns exec router "$@"; }

clear_root() { run tc qdisc del dev $DEV root 2>/dev/null || true; }

htb_root() {
    # one class, pure rate limiter; the interesting part is the leaf qdisc
    # r2q: htb derives each class's quantum as rate/r2q. The default r2q of
    # 10 gives 625000 bytes here — htb clamps it to 200000 and warns. 1000
    # keeps quanta a few packets wide and proportional to each class's rate.
    run tc qdisc add dev $DEV root handle 1: htb default 10 r2q 1000
    run tc class add dev $DEV parent 1: classid 1:10 htb rate $RATE ceil $RATE
}

case "${1:-show}" in
    pfifo)
        clear_root; htb_root
        run tc qdisc add dev $DEV parent 1:10 pfifo limit 1000
        ;;
    sfq)
        clear_root; htb_root
        run tc qdisc add dev $DEV parent 1:10 sfq perturb 10
        ;;
    fq_codel)
        clear_root; htb_root
        run tc qdisc add dev $DEV parent 1:10 fq_codel
        ;;
    cake)
        clear_root
        run tc qdisc add dev $DEV root cake bandwidth $RATE
        ;;
    cake-host)
        # Same cake, one word different — and it is the word people leave off.
        # Plain cake defaults to triple-isolate, which with every flow aimed at
        # the SAME server collapses back to per-flow: host-isolation.sh then
        # measures 8:1, exactly like fq_codel. dual-srchost shares by source
        # machine first, and the same test measures 50/50.
        # On a real router behind NAT this needs `nat` as well, so cake can ask
        # conntrack who each packet really belongs to. There is no NAT in this
        # testbed, so the source addresses are already the real ones.
        clear_root
        run tc qdisc add dev $DEV root cake bandwidth $RATE dual-srchost
        ;;
    htb-demo)
        # classful demo: port 5202 traffic is guaranteed 40 of the 50 mbit
        clear_root
        run tc qdisc add dev $DEV root handle 1: htb default 20 r2q 1000
        run tc class add dev $DEV parent 1:  classid 1:1  htb rate $RATE ceil $RATE
        run tc class add dev $DEV parent 1:1 classid 1:10 htb rate 40mbit ceil $RATE
        run tc class add dev $DEV parent 1:1 classid 1:20 htb rate 10mbit ceil $RATE
        run tc qdisc add dev $DEV parent 1:10 fq_codel
        run tc qdisc add dev $DEV parent 1:20 fq_codel
        run tc filter add dev $DEV parent 1: protocol ip u32 \
            match ip dport 5202 0xffff flowid 1:10
        ;;
    show) ;;
    *)
        echo "usage: $0 {pfifo|sfq|fq_codel|cake|cake-host|htb-demo|show}" >&2; exit 1
        ;;
esac

echo "--- qdisc on $DEV ---"
run tc -s qdisc show dev $DEV
