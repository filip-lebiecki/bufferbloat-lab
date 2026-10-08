# CAKE, taken apart — the video as commands

Companion to the second video: **six lines on a Linux router, and the six problems they
fix.** Every command from the video, chapter by chapter, with what you should see and why.

📺 **Video:** _<!-- TODO: paste the YouTube link here -->_

`# router`, `# client` and `# server` mark which box each command runs on — `.31`, `.32`
and `.33` in the [3-box lab](../lab/). Every number below was measured on that lab.

Jump to: [the six lines](#4--the-whole-fix-in-six-lines) ·
[1 standing queue](#5--problem-one-lag-under-load) ·
[2 sparse flows](#6--problem-two-the-single-carton-of-milk) ·
[3 the bully](#7--problem-three-the-rude-neighbour) ·
[4 the roommate](#8--problem-four-the-roommate-problem) ·
[5 tins](#9--problem-five-not-all-traffic-is-created-equal) ·
[6 both directions](#10--problem-six-the-other-direction) ·
[reading the dashboard](#11--reading-the-dashboard) ·
[your own line](#12--your-own-line)

---

## The short version

```bash
# upload — one line
sudo tc qdisc replace dev eth0 root cake bandwidth 50mbit nat dual-srchost ethernet ack-filter

# download — five lines
sudo ip link add ifb0 type ifb
sudo ip link set ifb0 up
sudo tc qdisc add dev eth0 handle ffff: ingress
sudo tc filter add dev eth0 parent ffff: matchall action mirred egress redirect dev ifb0
sudo tc qdisc replace dev ifb0 root cake bandwidth 100mbit nat dual-dsthost ethernet ingress
```

`eth0` is the interface facing your ISP. On a real line, set both rates **below what the
line actually delivers at busy hours** — see [chapter 12](#12--your-own-line). To make it
survive a reboot, use [`deploy/linux/cake-sqm.sh`](../deploy/linux/cake-sqm.sh) and the
[Linux guide](01-linux-router.md).

| problem | what fixes it | before → after (lab) |
|---|---|---|
| 1. lag under load | COBALT (CoDel + BLUE) keeps the queue short | ping 225 ms → **20.6 ms** |
| 2. small stuff waits behind big stuff | sparse flows are served first | 9.4 Mbit stream at **20.4 ms** next to a saturating upload |
| 3. a flow that won't back off | per-flow queues: it only hurts itself | TCP ~0 → **23 Mbit** next to a 50M UDP flood |
| 4. one machine, eight connections | `nat dual-srchost` / `nat dual-dsthost` | 42 / 5 → **24 / 23** Mbit |
| 5. calls vs backups | tins (DSCP) | LE "torrent" yields to **3 Mbit**; EF tin 148 µs vs 1.38 ms |
| 6. the other direction | shape the download too | upload 35 → **43 Mbit** |

And the libreqos grade on the same line: **D → A+**.

---

## The scripts used in this video

| Script | Runs on | What it does |
|---|---|---|
| [`lab/set-modem.sh`](../lab/set-modem.sh) | router | `bloat` = 100/50 with a 1000-packet FIFO (a bad modem), `cake` = the six lines, `off`, `show` |
| [`lab/load.sh`](../lab/load.sh) | client | starts each chapter's traffic in the background: `./load.sh intro`, `roommate`, `tins`… `stop` |
| [`lab/tins.sh`](../lab/tins.sh) | router | the cake tin table without the noise — `watch -n1 ./tins.sh` |
| [`lab/flowwatch.sh`](../lab/flowwatch.sh) | router | one line per second: sparse/bulk flow gauges and the Best Effort delays |
| [`lab/dscpmap.sh`](../lab/dscpmap.sh) | router | measures which tin every DSCP code point lands in, **on your kernel** |
| [`lab/setup-server.sh`](../lab/setup-server.sh) | server | 20 ms of distance, iperf3 on 5201-5204, `irtt server` |

You also need [`iperf3`](https://iperf.fr/) and [`irtt`](https://github.com/heistp/irtt)
(`apt install iperf3 irtt`) on the client and the server, and `conntrack` on the router.
Every iperf3 stream that runs at the same time as another needs its own server port —
one listener serves one test at a time.

---

## 1 — Intro: the whole demo in one switch

Five things at once on a 50 Mbit uplink: a normal upload, a background "torrent" marked
Lower Effort, a voice call, a ping, and a roommate with eight streams.

```bash
# router
sudo ./set-modem.sh bloat

# client — or all five in one go: ./load.sh intro
iperf3 -c 192.168.12.120 -p 5201 -B 192.168.80.32 -t 3600                 # you
iperf3 -c 192.168.12.120 -p 5203 -B 192.168.80.32 --tos 0x04 -t 3600      # torrent, DSCP 1 (LE)
irtt client -i 20ms -l 172 -d 3600s --dscp=0xb8 192.168.12.120            # the call, EF
ping -i 0.5 192.168.12.120                                                # stand-in for SSH
iperf3 -c 192.168.12.120 -p 5202 -B 192.168.80.42 -P 8 -t 3600            # the roommate

# router
tc -s qdisc show dev eth0
```

| | FIFO (`bloat`) | CAKE (`cake`) |
|---|---|---|
| call round trip | 250 ms, ~1 ms jitter, some loss | ~20 ms, **70 µs** jitter, 0 loss |
| ping | 240 ms | 20 ms |
| roommate's 8 streams | **40 Mbit** — ~10 left for everything else | 22 Mbit |
| your one upload | squeezed into those ~10 | **21 Mbit** |
| LE torrent | about half the link, before the roommate arrives | ~3 Mbit |
| queue | **1000 packets**, ~1.5 MB ≈ 240 ms at 50 Mbit | a few packets |

```bash
# router — same traffic, one change
sudo ./set-modem.sh cake
```

Three separate mechanisms produce that result: queue control (the delay), host fairness
(the roommate), and tins (the torrent). The rest of the video takes them apart one at a time.

---

## 2 — Why queues, not speed, cause lag

A router at a fast-to-slow merge has three options when packets arrive faster than they
can leave: **drop** them, **reorder** them, or **queue** them. Choosing between those is
the job of the queuing discipline — the qdisc.

The traditional answer was a deep FIFO. A thousand packets at 50 Mbit is a quarter of a
second of waiting, inside your own router — and a monitoring tool still calls the link
healthy, because only ~0.2% of packets are dropped. `dropped` is past tense; `backlog`
is what is waiting right now.

`fq_codel` ([video 1](video-commands.md)) fixes the delay on a single machine. It cannot:

1. set your line speed (it needs an HTB shaper in front of it),
2. tell a call from a backup,
3. see that two machines behind NAT are two different people.

CAKE — *Common Applications Kept Enhanced* — does all three in one qdisc.

---

## 3 — The lab

```
                     LAN                                     "WAN"
 [client .32 + .42] ─────────── [eth1  ROUTER .31  eth0] ─────────── [server 192.168.12.120]
  you + your roommate         192.168.80.31   192.168.12.100      netem 20 ms + tbf 100M
                                      │
                       set-modem.sh bloat: HTB 50 up / 100 down
                                       + 1000-packet FIFO  (the "bad modem")
```

```bash
# client — two addresses: you (.32) and your roommate (.42)
ip -4 -br a show dev eth0

# router
ip -4 -br a
tc class show dev eth0          # htb 50mbit  (upload)
tc class show dev ifb0          # htb 100mbit (download)
tc qdisc show dev eth0          # pfifo limit 1000 under the htb class
tc qdisc show dev ifb0

# server — the distance
tc qdisc show dev eth0          # netem delay 20ms

# client — the baseline every result is measured against
ping 192.168.12.120             # ~20.5 ms
```

Then from the client: [fast.com](https://fast.com) shows 100 down / 50 up — the shaper
works — and [test.libreqos.com](https://test.libreqos.com) gives **grade D**: +23 ms on
the download and **+208 ms** on the upload under load. Calls, gaming and phone: poor.

The speed test is fine; the bufferbloat test isn't. The rate limit isn't the problem —
the thousand-packet buffer behind it is.

> **Measure first.** Both of my real fibre lines graded A with no shaping at all — one even
> has a hard 100 Mbit upload cap, but it *drops* the excess instead of queueing it. Rate
> limit, yes; parking lot, no. That is why this lab emulates a bad modem: on a line with
> nothing to fix, CAKE has nothing to show. If your grade is already A, you are done.

---

## 4 — The whole fix, in six lines

### Upload: one line

```bash
# router
sudo tc qdisc replace dev eth0 root cake bandwidth 50mbit nat dual-srchost ethernet ack-filter
```

`replace` on the root swaps out the whole HTB + FIFO tree — CAKE has its own shaper.

| keyword | why |
|---|---|
| `bandwidth 50mbit` | the shaper: how wide the tunnel is |
| `nat dual-srchost` | fair share per *machine*, seen through NAT — [chapter 8](#8--problem-four-the-roommate-problem) |
| `ethernet` | Linux counts the packet but not the wire framing around it (preamble, FCS, inter-frame gap) — 38 bytes per packet with the header. Without it CAKE overfills the line and the queue moves back into the modem. Fibre/plain Ethernet: `ethernet`. Cable, DSL, PPPoE have their own keywords — `man tc-cake`. |
| `ack-filter` | TCP ACKs are cumulative, so a redundant ACK still waiting in the queue can be removed. Worth it on lopsided lines (say 100/10). Here: 52 ACKs removed in a whole test. |

### Download: five lines

Two problems: the download queue is in your ISP's equipment, and a qdisc only controls
packets *leaving* an interface. So shape slightly below the line rate — your router
becomes the narrowest point and the queue forms on your side — and turn arriving traffic
into leaving traffic:

```bash
# router
sudo ip link add ifb0 type ifb      # a virtual NIC whose only job is to receive redirects
sudo ip link set ifb0 up
sudo tc qdisc add dev eth0 handle ffff: ingress     # a checkpoint on eth0's arrival side
sudo tc filter add dev eth0 parent ffff: matchall action mirred egress redirect dev ifb0
sudo tc qdisc replace dev ifb0 root cake bandwidth 100mbit nat dual-dsthost ethernet ingress
```

- The ingress qdisc can't queue anything; it can only hold a filter.
- `mirred egress redirect` *moves* the original packet to `ifb0` — not a copy, unlike a
  SPAN/mirror port. On `ifb0` it is leaving, so CAKE can queue it.
- `dual-dsthost`: coming in, the machine that matters is the one the packet is *for*.
- `ingress`: these packets already crossed the ISP's line, so even the ones CAKE drops
  count against the 100 Mbit.
- No `ack-filter`: the ACKs for your downloads travel on the upload side.

> In this lab CAKE *replaces* the modem's rate limit, so it shapes at the full 100/50. On a
> real line you shape **below** what the line delivers — see [chapter 12](#12--your-own-line).

All six lines are what `sudo ./set-modem.sh cake` applies ([`setup-router-cake.sh`](../lab/setup-router-cake.sh)).

**Result:** libreqos **D → A+**. Latency under load +5 ms down, +4 ms up. Calls, gaming and
phone: poor → excellent. Same machine, same internet, same speed limit.

---

## 5 — Problem one: lag under load

*The standing queue.*

```bash
# router
sudo ./set-modem.sh bloat

# client — ./load.sh standing
iperf3 -c 192.168.12.120 -p 5201 -B 192.168.80.32 -t 3600
ping -i 0.5 192.168.12.120                                      # ~225 ms
irtt client -i 20ms -l 172 -d 5s --dscp=0xb8 192.168.12.120     # a call: 172 B / 20 ms ≈ 69 kbit
```

| irtt, FIFO | |
|---|---|
| RTT | **209 ms** |
| jitter (IPDV) | ~0.5 ms |
| loss | 0.4% |
| send delay / receive delay | 196 ms / ~12 ms |

High but *steady* delay: packets keep leaving, new ones keep taking their place, the queue
never clears. That is a **standing queue**. Low loss alone doesn't mean a good call.

irtt splits one-way delays (if the two clocks are in sync) — almost all of the wait is on
the way *out*, in the upload queue.

```bash
# router
sudo ./set-modem.sh cake

# client
irtt client -i 20ms -l 172 -d 5s --dscp=0xb8 192.168.12.120     # 209 → 20.6 ms

# router — the queue itself
watch -n1 tc -s qdisc show dev eth0
```

`backlog` goes from a thousand packets to a handful, and the upload still runs flat out.

**How:** CAKE's AQM is **COBALT** = **Co**Del + **B**LUE (+ **ALT**ernate).

- **CoDel** timestamps each packet on enqueue and checks its sojourn time on dequeue.
  `target 5ms` is how much *persistent* waiting it tolerates; `interval 100ms` is how long
  it watches before calling it persistent rather than a burst. If even the *shortest* wait
  stays above target for a whole interval, it starts dropping (or ECN-marking), and drops
  more often while the queue stays up. TCP backs off; the queue drains.
- **BLUE** handles flows that ignore that signal — [problem three](#7--problem-three-the-rude-neighbour).

5 ms isn't a per-packet guarantee. It is about acting before a long queue becomes permanent.

---

## 6 — Problem two: the single carton of milk

*Sparse flows.*

Every flow gets its own queue, and CAKE serves them in turns (DRR, roughly one packet's
worth each). A flow that empties its queue within its turn — a ping, DNS, a TCP ACK, an
SSH keystroke — goes on the **sparse** list and is served first. A flow that still has
bytes waiting at the end of its turn is **bulk** and goes to the back. Sparse doesn't mean
small packets. It means *wanting less than your fair share*.

```bash
# router
sudo ./set-modem.sh cake

# client
iperf3 -c 192.168.12.120 -t 3600
ping -i 0.5 192.168.12.120

# router
watch -n1 ./tins.sh            # or: ./flowwatch.sh 30
```

In the Best Effort column: `av_delay` (moving average) ~**2 ms**, `sp_delay` (the low end)
~**200 µs**. Both describe the whole tin, not the ping specifically — the ping's own
replies give its round trip.

"Less than your fair share" can be a lot:

```bash
# client — ./load.sh sparse
irtt client -i 1ms -l 1400 -d 600s 192.168.12.120     # 1400 B every 1 ms ≈ 9.4 Mbit, unmarked
```

**20.4 ms** — idle latency at 9.4 Mbit. With two flows on 50 Mbit the fair share is ~25,
so this stream's queue drains between arrivals.

> Needs `irtt server -i 0` on the server — irtt's default minimum interval is 10 ms.
> [`setup-server.sh`](../lab/setup-server.sh) starts it that way.

> **Why `sp_flows` shows 0 for the ping:** it's an instantaneous gauge, not a counter. A
> never-dropped sparse flow leaves the list the moment its one packet is sent — ~100 µs at
> 50 Mbit. A 1 Hz `watch` almost never catches it. Read the `av_delay` / `sp_delay` gap
> instead, or run a high-rate flow under its fair share for a steady `sp_flows 1`:
> `iperf3 -c 192.168.12.120 -p 5204 -B 192.168.80.32 -u -b 8M -l 100 -t 3600`.

---

## 7 — Problem three: the rude neighbour

*TCP vs an unresponsive UDP flood.*

```bash
# router
sudo ./set-modem.sh bloat

# client
iperf3 -c 192.168.12.120 -p 5201 -B 192.168.80.32 -t 3600     # one TCP: the whole 50
iperf3 -c 192.168.12.120 -p 5202 -B 192.168.80.32 -t 3600     # second TCP: ~24 each. Polite.
```

Stop the second one and replace it with UDP at a fixed 50 Mbit (`./load.sh bully`):

```bash
# client
iperf3 -c 192.168.12.120 -p 5202 -B 192.168.80.32 -u -b 50M -t 3600
```

FIFO: the flood takes almost everything; TCP steps aside. Whoever shouts loudest wins.

```bash
# router
sudo ./set-modem.sh cake
```

**50/50.** TCP gets ~23 Mbit; UDP still sends 50, but only about half gets through. Each
flow has its own lane and gets every other turn, so the flood's excess piles up — and is
dropped — in its *own* queue. On the dashboard: `drops` climbing while TCP's share doesn't
move. That is BLUE's job: a flow that keeps its queue full gets a rising drop probability.

Flow isolation protects TCP's turn; queue management deals with the excess. Something that
ignores congestion can't take the line from you — it can only hurt itself.

---

## 8 — Problem four: the roommate problem

*Fair between flows ≠ fair between people.*

```bash
# router — CAKE defaults
sudo tc qdisc replace dev eth0 root cake bandwidth 50mbit

# client — ./load.sh roommate
iperf3 -c 192.168.12.120 -p 5201 -B 192.168.80.32 -P 8 -t 3600   # you: 8 streams
iperf3 -c 192.168.12.120 -p 5202 -B 192.168.80.42 -t 3600        # roommate: 1 stream
```

**42 / 5 Mbit.** Perfectly fair between nine flows, completely unfair between two people.
The default mode on the dashboard is `triple-isolate`.

```bash
# router
sudo tc qdisc replace dev eth0 root cake bandwidth 50mbit dual-srchost
```

**Still 42 / 5.** `tc` shows `dual-srchost` set — and it does nothing. Here's why:

```bash
# router
sudo conntrack -L -p tcp | grep ESTABLISHED | grep -E 'dport=520[12] '
```

The left half of each entry is the truth (`src=192.168.80.32`, `src=192.168.80.42`). The
right half is how it looks from outside: every reply goes back to **one** address, the
router's. The router masquerades, and by the time a packet reaches the qdisc on `eth0` its
source has already been rewritten. CAKE sees one machine with nine flows.

```bash
# router — one more word
sudo tc qdisc replace dev eth0 root cake bandwidth 50mbit nat dual-srchost
```

**24 / 23 Mbit.** `nat` makes CAKE ask conntrack who really sent each packet.

Downloads, the same in reverse:

```bash
# router
sudo tc qdisc replace dev ifb0 root cake bandwidth 100mbit nat dual-dsthost
```

`nat` is needed here too: the ingress hook grabs packets *before* Linux undoes the NAT, so
without it every download in the house looks addressed to the router itself.

**Rule for both directions:** if your queue sees the rewritten address, it needs `nat` to
see through it.

| | upload (`eth0`) | download (`ifb0`) |
|---|---|---|
| mode | `dual-srchost` | `dual-dsthost` |
| `nat` needed? | yes — after SNAT | yes — before de-NAT |

---

## 9 — Problem five: not all traffic is created equal

*Tins.*

With the default `diffserv3`, CAKE has three priority tins — **Bulk**, **Best Effort**,
**Voice** — the three columns of `tc -s qdisc`. Traffic is sorted by **DSCP**, six bits in
the IP header that the application usually sets itself.

```bash
# client — what SSH marks by default
ssh -G 192.168.12.120 | grep ipqos       # ipqos ef cs0
```

Out of the box (OpenSSH 10+), interactive SSH gets **EF** — Expedited Forwarding, the voice
stamp — and non-interactive transfers get none. Older versions print `af21 cs1`. qBittorrent
marks peer traffic **LE** (Lower Effort, DSCP 1) by default — it volunteers for the slow lane.
If an app gives you no control, rewrite the DSCP in your firewall before CAKE sees it.

### Where each code point lands (measured)

```bash
# router — 12 pings per code point, reports which tin's counter moved
./dscpmap.sh
```

| tin | DSCP |
|---|---|
| **Voice** | EF (46), CS6 (48), CS7 (56) |
| **Bulk** | LE (1), CS1 (8) |
| **Best Effort** | everything else — including **AF41** and **CS5** |

AF41 is what enterprise gear loves for video conferencing. Under `diffserv3`, it gets no
priority at all. The map has changed between kernels — run `dscpmap.sh` on yours before you
trust any table, this one included.

### The slow lane

```bash
# client — ./load.sh tins
iperf3 -c 192.168.12.120 -p 5201 -B 192.168.80.32 -t 3600
iperf3 -c 192.168.12.120 -p 5202 -B 192.168.80.32 -t 3600
iperf3 -c 192.168.12.120 -p 5203 -B 192.168.80.32 --tos 0x04 -t 3600   # ToS 0x04 = DSCP 1 = LE
```

The two normal streams keep their share; the "torrent" gets **~3 Mbit** — 1/16 of the
shaped rate, the Bulk tin's `thresh`. But `thresh` isn't a cap: stop the other two and the
torrent takes the whole line. Bulk isn't limited, it's just first to yield.

> `--tos` takes the whole ToS byte: DSCP × 4. LE (1) = `0x04`, CS1 (8) = `0x20`, EF (46) = `0xb8`.

### The fast lane — and the cheat

```bash
# client — ./load.sh voice
iperf3 -c 192.168.12.120 -p 5201 -B 192.168.80.32 -t 3600
iperf3 -c 192.168.12.120 -p 5202 -B 192.168.80.32 --tos 0xb8 -u -b 1M -t 3600   # EF, 1 Mbit
```

The 1 Mbit EF stream sits in Voice, well under its threshold: `av_delay` **148 µs** vs
**1.38 ms** for Best Effort.

Now mark a *greedy* TCP upload EF (`./load.sh cheat`):

```bash
# client
iperf3 -c 192.168.12.120 -p 5202 -B 192.168.80.32 --tos 0xb8 -t 3600
```

Honest Best Effort: ~**35 Mbit**. Fake priority: ~**12 Mbit** — about the Voice `thresh`, a
quarter of the link. And the Voice tin's delay, pushed past its threshold: **3.69 ms**, worse
than Best Effort next to it. **A high tin buys low latency for a small amount of traffic.
It doesn't buy bandwidth, and it can't starve anybody.**

### Two honest caveats

- **Downloads arrive with the sender's marks.** On the internet most packets carry none and
  the rest are a random mix, so a downloading torrent lands in Best Effort, not Bulk. If a
  torrent download hurts you, cap it in the torrent client.
- **The call didn't need the mark.** Strip the EF from problem one's call and it's still
  20.6 ms: at ~69 kbit/s it's a sparse flow, and flow isolation does the work. CAKE already
  handles most of it without any classification.

---

## 10 — Problem six: the other direction

*Shape both directions.*

Every upload has a return path: its ACKs come back *down* the line. If the download queue is
bloated, they wait in it — and the upload waits for them.

```bash
# router
sudo ./set-modem.sh cake

# client — ./load.sh bothways
iperf3 -c 192.168.12.120 -p 5201 -B 192.168.80.32 -t 30          # upload
iperf3 -c 192.168.12.120 -p 5202 -B 192.168.80.32 -R -t 30       # download, at the same time
```

Upload: **43 Mbit**. Now replace only the *download* shaper with a plain FIFO:

```bash
# router
sudo tc qdisc replace dev ifb0 root pfifo limit 1000
```

A bare `pfifo` has no shaper, so the router stops being the download bottleneck — and the
queue moves to the next narrowest point. In this lab that's the server's `tbf 100mbit` with
its 1.5 MB buffer ([`setup-server.sh`](../lab/setup-server.sh)): the same kind of deep
buffer your ISP's equipment has, and exactly what happens at home if you only shape the
upload.

Restart both tests. Upload: **35 Mbit** — almost a fifth gone, without touching the upload
queue. Its own ACKs are stuck in the bloated download buffer. Bufferbloat in one direction
breaks the other. That's why the fix is six lines, not one.

---

## 11 — Reading the dashboard

Everything at once, CAKE on in both directions:

```bash
# client — ./load.sh dashboard
iperf3 -c 192.168.12.120 -p 5201 -B 192.168.80.32 -t 3600                 # regular upload
iperf3 -c 192.168.12.120 -p 5203 -B 192.168.80.32 --tos 0x04 -t 3600      # backup, LE
ping -i 0.5 192.168.12.120
irtt client -i 20ms -l 172 -d 3600s --dscp=0xb8 192.168.12.120            # call, EF

# router
tc -s qdisc show dev eth0          # or: watch -n1 ./tins.sh
```

### The header — what CAKE is actually running with

| field | meaning |
|---|---|
| `bandwidth 50Mbit` | shaped rate |
| `diffserv3` | three tins |
| `dual-srchost nat` | per-machine fairness, seen through NAT |
| `rtt 100ms` | the RTT CAKE tunes its AQM for (`internet` preset) — **not** your ping |
| `overhead 38 mpu 84` | from the `ethernet` keyword |

### The totals

| field | meaning |
|---|---|
| `backlog` | data waiting to leave *right now*. The FIFO kept it pinned at 1000 packets; here it stays short. |
| `dropped` | running total. Drops are healthy — they're how CAKE tells senders to slow down — but the total doesn't say *who* lost packets. |

### Per tin (columns: Bulk · Best Effort · Voice)

| row | what it tells you | in this snapshot |
|---|---|---|
| `thresh` | the tin's priority threshold — not a cap | 3.1 M · 50 M · 12.5 M |
| `target` / `interval` | CoDel timings; Bulk gets slightly more because packets take longer at its rate | |
| `pk_delay` | recent peak sojourn time | |
| `av_delay` | moving average | **17.4 ms · 5.57 ms · 111 µs** |
| `sp_delay` | the low end (where sparse packets live) | |
| `backlog` | where the waiting data is | Voice: 0 — the call is served between arrivals |
| `pkts` / `bytes` | what passed through — check your marks landed where you expected | |
| `drops` | who lost packets | the busy uploads, not the call |
| `marks` | ECN marks instead of drops | 0 |
| `ack_drop` | ACKs removed by `ack-filter` | 0 |
| `bk_flows` | busy flows with data queued — "bulk" means *busy*, not the Bulk tin | 1 in Bulk, 1 in Best Effort |
| `sp_flows` | sparse flows *at this instant* — usually 0 at a 1 Hz refresh | |

The delays are time inside *this* queue only; the ping also includes the lab's 20 ms path.

**Healthy looks like:** short backlog, low delay for interactive traffic, and drops landing on
the busy flows.

---

## 12 — Your own line

1. **Measure first**, wired, at busy hours: [test.libreqos.com](https://test.libreqos.com) or
   [waveform.com/tools/bufferbloat](https://www.waveform.com/tools/bufferbloat). If it's A or
   A+, you may be done.
2. **Shape both directions, below what the line actually delivers when everyone's online** —
   not below what you pay for.

   > My 500 Mbit line graded A unshaped. CAKE at 440 made it *worse*: +25 ms. That evening
   > the line only delivered 300–370 Mbit, so the shaper was too fast to own the queue. At
   > 300: **A+**. "90% of your plan" can be worse than nothing.
3. **Trust the end-to-end test over the dashboard.** CAKE can show a tiny backlog while
   packets pile up in the ISP's equipment — it only sees its own queue. If loaded latency
   stays high, lower the rate and test again.
4. **Then, optionally, the RTT preset.** The default is `internet` (`rtt 100ms`). If most of
   your traffic goes to nearby servers, try `regional` (30 ms) or `metro` (10 ms) on *both*
   CAKE lines. They can cost throughput on long paths — keep the change only if it helps
   under load.

   ```bash
   sudo tc qdisc replace dev eth0 root cake bandwidth 50mbit nat dual-srchost ethernet ack-filter regional
   sudo tc qdisc replace dev ifb0 root cake bandwidth 100mbit nat dual-dsthost ethernet ingress regional
   ```

- Behind the router: `fq_codel` is the sensible default for laptops and desktops; plain `fq`
  for a server running BBR.
- Watch the router's CPU at high rates — CAKE can be more than a small router can handle.
- Wired test good but calls still stutter on Wi-Fi? The wireless link is the next queue to
  look at.

Persistent setup: [`deploy/linux/cake-sqm.sh`](../deploy/linux/cake-sqm.sh) +
[`cake-sqm.service`](../deploy/linux/cake-sqm.service) — walkthrough in
[docs/01-linux-router.md](01-linux-router.md). MikroTik and UniFi:
[02-mikrotik.md](02-mikrotik.md), [03-unifi.md](03-unifi.md).

---

## The one-card summary

> **CAKE gives you control over the queue.** Six lines on Linux: shape both directions, set
> the rates below what your line delivers at busy hours, and use `nat` so CAKE can see the
> machines sharing it. Then test under load — that's where the difference shows.
>
> - Delay: COBALT keeps the queue short (225 → 20.6 ms).
> - Light traffic jumps the line automatically — no marking needed.
> - Floods only hurt themselves.
> - `nat` or per-machine fairness silently does nothing, in both directions.
> - Tins buy latency for small traffic, not bandwidth. AF41 is just Best Effort.
> - Bloat in one direction costs you the other.
