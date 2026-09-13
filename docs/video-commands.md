# The video, as a command list

Every command from the video, in order, with what it is for. `# router`, `# client` and
`# server` mark which box each one runs on — in the 3-box lab that is `.31`, `.32` and
`.33`; in the [netns lab](../netns-lab/) it is `sudo ip netns exec router …` and friends.

Jump to: [the disease](#the-disease) · [the ladder](#the-ladder-sfq--red--fq_codel) ·
[cake](#cake) · [the deployable configuration](#the-deployable-configuration)

---

## 1 — The hook

```bash
# client
ping 192.168.12.120                       # 20 ms. This is a normal connection.
iperf3 -c 192.168.12.120 -t 300           # start ONE upload. Watch the ping.
```

Ping goes to ~220 ms average, 256 ms worst. 11× the idle ping, from one upload.

```bash
# router
sudo tc qdisc replace dev eth0 root cake bandwidth 50mbit
```

Same link, same upload still running: **20.5 ms**. That is the whole video in one line.

## 2 — Find out what you are running right now

```bash
tc qdisc show dev eth0
```

The first word after `qdisc` is the algorithm managing your packets at this moment. If it
says `pfifo_fast`, or `mq` feeding `pfifo_fast`, that is the 1990s answer.

The arithmetic that predicts the damage:

```
1000 packets × 1514 bytes × 8 bits  =  12,112,000 bits in the queue
12.1 Mbit ÷ 50 Mbit/s               =  242 ms of waiting
```

Add the link's own 20 ms and you get 262 ms — which is what we measured. Not a quirk of
one lab: arithmetic.

## 3 — The topology

```bash
# router
ip -4 -br a                     # eth0 = WAN (the slow uplink), eth1 = LAN (fast)
ip r                            # default route out eth0
sysctl net.ipv4.ip_forward      # =1. This is the only thing making it a router.
```

A queue only ever forms where a fast pipe feeds a slow one, so exactly one queue in this
topology matters: **eth0's egress**.

```
                    LAN — fast                          WAN — the slow uplink
 [client .32] ═══════════════════════ [eth1 ▶ ROUTER ◀ eth0] ──────────── [server]
                                     fat pipe        │
                                                     ▼
                                              ★ THE QUEUE ★
```

## The disease

```bash
# router — build the villain: a 50 mbit bottleneck with a dumb 1000-packet FIFO
sudo tc qdisc add dev eth0 root handle 1: htb default 10
sudo tc class add dev eth0 parent 1: classid 1:10 htb rate 50mbit ceil 50mbit
sudo tc qdisc add dev eth0 parent 1:10 pfifo limit 1000
```

```bash
# router — the diagnostic that every dashboard gets wrong
tc -s qdisc show dev eth0
```

`dropped: 145` out of 140,000 — a tenth of a percent, no errors, full throughput. This
interface is "perfectly healthy". Now read `backlog`: **835 packets**, over a megabyte of
your data sitting in the queue right now. `dropped` is past tense. **`backlog` is present
tense.**

## The meter

```bash
# client
uv run tools/cakemeter.py --router 192.168.80.31 --target 192.168.12.120
```

Web UI on `:8420`: runs the load, charts ping and throughput, and streams the router's own
queue over SSH twice a second. **Backlog** is how many packets are in the queue right now;
**drain time** is that backlog divided by the drain rate — the latency sitting in the
queue before a packet even reaches the ping test.

## The ladder: sfq → RED → fq_codel

Each of these is a queue, not a shaper, so each sits under the same HTB class that
enforces the 50 Mbit bottleneck — exactly how you would deploy them on a real router.

```bash
# router — SFQ (1990): don't track flows, hash them. 1024 buckets, round-robin.
sudo tc qdisc replace dev eth0 parent 1:10 sfq perturb 10
```

Ping under load: 20.5 ms — identical to idle. **And the queue is still full**: 125 packets
at the top of every cycle, 25 ms to drain, cycling against its own `depth 127` ceiling.
The ping looks fine because a ping is a *sparse* flow with its own empty bucket. A video
call is not sparse. SFQ separates traffic; it does not manage delay.

```bash
# router — RED (1993), thresholds tuned for this link
sudo tc qdisc replace dev eth0 parent 1:10 red \
    limit 1500000 min 62500 max 187500 avpkt 1500 burst 69 probability 0.02 bandwidth 50mbit
```

47.6 Mbit, ping 26.9 ms, 42 packets draining in 5 ms — the first thing in the video that
actually drained the queue, for *everything* in it. Then tune it for 2 Mbit instead of 50
and watch throughput fall to 39.8 Mbit with an A+ latency grade and nothing, anywhere,
that would tell you a fifth of your link is gone. That is why nobody deployed it.

```bash
# router — fq_codel (2012): a queue per conversation, CoDel policing each one
sudo tc qdisc replace dev eth0 parent 1:10 fq_codel
```

`target 5ms`, `interval 100ms`. Every packet is timestamped in; on the way out CoDel
checks how long it sat, and if even the *best-case* packet waited over 5 ms across a
rolling 100 ms window, it starts dropping from the head. Same 20.5 ms ping as SFQ — and
**1 ms** of queue instead of 25. "5 ms of waiting" means the same thing at 1 Mbit and at
10 Gbit, which is why it needs no tuning where RED did.

## cake

What fq_codel actually costs you on every interface, spelled out:

```bash
# router
sudo tc qdisc del dev eth0 root
sudo tc qdisc add dev eth0 root handle 1: htb default 10
sudo tc class add dev eth0 parent 1: classid 1:10 htb rate 50mbit ceil 50mbit
sudo tc qdisc add dev eth0 parent 1:10 fq_codel
tc qdisc show dev eth0                    # two qdiscs: one shapes, one queues
```

The same thing with cake:

```bash
# router
sudo tc qdisc replace dev eth0 root cake bandwidth 50mbit
tc qdisc show dev eth0                    # one line. One qdisc, both jobs.
```

`replace` on the root swaps out the entire tree — htb, class, leaf, all of it.

cake ties fq_codel on latency, because it *is* the same CoDel at the same 5 ms target
behind a shaper at the same rate. It is not here to beat fq_codel. It is here so you never
had to type the htb line — and for what else is in the box:

```bash
# router — the dashboard no other qdisc prints
tc -s qdisc show dev eth0
```

Three tins — Bulk, Best Effort, Voice — sorted by DSCP with no configuration. `av_delay`
is the time packets actually spent in the queue, measured by the queue itself, per tin.

```bash
# client — mark the ping as expedited forwarding, exactly like a VoIP app does
ping -Q 0xb8 192.168.12.120
```

Best Effort (the bulk upload): 7.7 ms average. Voice (the ping): **17 microseconds**. Same
interface, same instant, 450× difference, and nobody configured it. You cannot game it by
marking everything voice — each tin has a bandwidth `thresh`, past which it stops being
special. A high tin buys latency, not capacity.

### Host fairness — the roommate with eight torrents

```bash
# client — give the client a second address to play "host B"
ip -4 -br a
```

Host A opens 8 connections, host B opens 1. A perfectly fair per-flow queue hands host A
**8/9 of the link** — and every algorithm on this ladder calls that fair, because by the
only definition any of them has, it is.

```bash
# router — two words fix it, and people leave the second one off
sudo tc qdisc replace dev eth0 root cake bandwidth 50mbit nat dual-srchost
```

50/50. Host A keeps its 8 connections, it just does not get *paid* for them any more.

`dual-srchost` shares by source machine. `nat` is what makes it work at all: behind
masquerade every packet leaving the router already carries the same source address — the
router's — so without `nat`, cake sees one host and diligently shares the link with
itself. It has to ask conntrack who each packet really belongs to.

## The deployable configuration

Downloads have two problems uploads don't: the queue is in the ISP's equipment, and qdiscs
only control traffic *leaving* an interface. So turn arriving traffic into leaving traffic.

```bash
# router
sudo ip link add ifb0 type ifb                     # a network card that doesn't exist
sudo ip link set ifb0 up
sudo tc qdisc add dev eth0 handle ffff: ingress    # a checkpoint on the arrival side
sudo tc filter add dev eth0 parent ffff: matchall \
    action mirred egress redirect dev ifb0         # every packet -> ifb0, as if leaving
sudo tc qdisc add dev ifb0 root cake bandwidth 90mbit ingress
```

`ffff:` is just the name Linux reserves for the ingress hook. That qdisc cannot queue
anything — no buffering, no scheduling, no dropping. It is a checkpoint, not a waiting
room, and the one thing a checkpoint can do is hold a filter.

`ingress` on the last line tells cake to count the bytes it *throws away* against the
rate, because that bandwidth was already spent crossing the real link. It is also why the
download lands slightly under the number you typed.

**The whole thing, both directions:**

```bash
# router
sudo tc qdisc replace dev eth0 root cake bandwidth 50mbit nat dual-srchost ethernet ack-filter
sudo tc qdisc replace dev ifb0 root cake bandwidth 90mbit nat dual-dsthost ethernet ingress
```

`nat dual-srchost` going out, `nat dual-dsthost` coming back — share by whoever sent it,
and by whoever it is for. `ethernet` accounts for the 38 bytes of framing cake otherwise
pretends aren't there (without it you type 50 and put 51 on the wire — and if the ISP's
limit is exactly 50, you just handed the queue back to them). `ack-filter` drops redundant
acks on the upload: near-nothing on a symmetric line, 5–10% on a lopsided one.

Then run both directions flat out at once. Both links saturated, ping still on idle.

## Proof, on the real internet

Same machine, same ISP, only the router's qdisc changed:

| | latency under load | grade |
|---|---|---|
| baseline (HTB + pfifo at matched rates) | 78 ms | **C** |
| cake, 100 down / 50 up | ~1 ms | **A+** |

Run it yourself from a wired client: [waveform.com/tools/bufferbloat](https://www.waveform.com/tools/bufferbloat)
or [test.libreqos.com](https://test.libreqos.com).

The two halves of that A/B are [`lab/setup-router-org.sh`](../lab/setup-router-org.sh) and
[`lab/setup-router-cake.sh`](../lab/setup-router-cake.sh) — same rates in both, so the only
thing that differs is what happens once the pipe is full.

---

## The one-card summary

> **Traffic isn't the problem — the queue is.** Control the queue where it actually forms.
> Shape below your real line speed, then test again.
>
> **cake for the router. fq_codel for everything behind it.** cake is more CPU-hungry than
> fq_codel + HTB; on a weak router, use fq_codel. A laptop has no shaper, no NAT and no
> other hosts to be fair between, so fq_codel is the right answer there. fq_codel on a LAN
> needs no HTB; on an asymmetric WAN it does.
>
> And cake only fixes the queue you control. If the grade is good and it still lags, look
> upstream — or at Wi-Fi.
