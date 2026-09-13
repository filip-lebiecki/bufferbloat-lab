# The one-machine lab

The same three-box topology, built out of **network namespaces** on a single Linux
machine. Nothing to cable, nothing to reboot, and `teardown-testbed.sh` deletes every
trace of it. If you want to see any of this for yourself in the next five minutes, start
here.

```
  [client]  10.0.1.2                      10.0.2.2  [server]
     c-eth ---------- [router] ---------- s-eth
             r-c  10.0.1.1  10.0.2.1  r-s
                             ^^^ the bottleneck qdisc goes on r-s
```

The return path (server → client) gets 20 ms of `netem` delay, so the baseline RTT looks
like a real internet path instead of 0.05 ms.

You need `iproute2`, `iperf3` and root. That's all.

## Five minutes

```bash
sudo ./setup-testbed.sh

sudo ./set-qdisc.sh pfifo     && sudo ./measure.sh     # ~20 ms idle, ~250 ms loaded
sudo ./set-qdisc.sh sfq       && sudo ./measure.sh
sudo ./set-qdisc.sh fq_codel  && sudo ./measure.sh
sudo ./set-qdisc.sh cake      && sudo ./measure.sh     # loaded ping ≈ idle ping

sudo ./teardown-testbed.sh
```

`set-qdisc.sh show` prints what is currently on the bottleneck. `pfifo`, `sfq`, `red` and
`fq_codel` are pure queue algorithms, not shapers, so the script puts them under an HTB
class that enforces the 50 Mbit bottleneck — exactly how you would deploy them on a real
router. `cake` shapes by itself and replaces the whole tree.

## The demos

Each one is a scenario from the video (or a couple that had to be cut), and each one is
worth running twice — once under `pfifo`, once under `cake` — because the comparison *is*
the result.

| Script | What it shows |
|---|---|
| `measure.sh [sec]` | The core test: idle RTT, loaded RTT, throughput. |
| `fairness.sh [sec]` | **The rude neighbour.** A 60 Mbit UDP flood at a 50 Mbit link, next to a polite TCP upload. Under `pfifo` the flood owns the queue and the polite sender gets a fifth of one percent; under `sfq`/`fq_codel`/`cake` they split it — and the flood drowns in *its own* queue. You cannot fix a rude sender. You can only stop it sharing a line with everyone else. |
| `sparse-flow.sh` | **Why the web feels broken.** How long a 256 KB transfer takes while a bulk upload runs. 1.1 s idle → 4 s loaded under `pfifo`. This is the one non-networking people feel in their gut. |
| `host-isolation.sh [sec]` | **The roommate with eight torrents.** Host A opens 8 flows, host B opens 1. Per-flow fairness gives A 8/9 of the link; `cake` + `dual-srchost` + `nat` splits it 50/50. |
| `ecn-demo.sh [sec]` | The same upload twice, with ECN off and on: congestion signalled by *marking* instead of dropping, with zero packet loss. Watch cake's per-tin drop/mark counters. |
| `ingress-demo.sh [sec]` | **Downloads.** The router's egress toward the client plays the ISP's modem — 50 Mbit with a dumb FIFO you cannot touch. The fix runs entirely on the *client*: redirect ingress through an `ifb` and let cake shape it to 45 Mbit, so the queue forms where you control it. This is exactly what OpenWrt SQM does. |
| `htb-test.sh` / `htb-borrow.sh` | **Who gets how much** (a different question from "why does it lag"). Run `set-qdisc.sh htb-demo` first: two classes, 10 Mbit and 40 Mbit, `ceil 50`. `htb-test.sh` verifies the split; `htb-borrow.sh` shows the bulk class borrowing up to `ceil` while it is alone, then handing the bandwidth back the instant the priority class wakes up. |

## How this differs from the video's lab

Older, simpler, and deliberately so — one kernel instead of three machines. Every
measurement is real (same qdiscs, same `tc`, same TCP), but a few things that only show up
across real NICs don't exist here: no NAT to look through, no link-layer framing to
account for, and no ISP equipment to move the queue off. The video's rig in
[`../lab/`](../lab/) covers those.

If a number here disagrees with a number in the video, the video's rig is the one that was
measured on hardware.
