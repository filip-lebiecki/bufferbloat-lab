# bufferbloat-lab

Companion repository for the bufferbloat videos:

| | Video | Commands and notes |
|---|---|---|
| 1 | **Bufferbloat: why your internet lags, and the one line that fixes it** — 📺 _<!-- TODO: paste the YouTube link here -->_ | [docs/video-commands.md](docs/video-commands.md) |
| 2 | **CAKE, taken apart: six lines, six problems** — 📺 _<!-- TODO: paste the YouTube link here -->_ | [docs/cake-explained.md](docs/cake-explained.md) |

Your connection is fast. Your speed test says so. And the moment somebody starts an
upload, your game rubber-bands, your call freezes and your ping goes from 20 ms to
250 ms — at full throughput, with no packet loss, on a link that is working perfectly.

That is **bufferbloat**, and it is not your ISP's fault. More bandwidth will not fix it.
This is the fix:

```bash
sudo tc qdisc replace dev eth0 root cake bandwidth 50mbit
```

Same link, same upload still running, ping back to idle — for about 2% of throughput.

| | idle ping | ping under load | throughput |
|---|---|---|---|
| `pfifo` (1000 packets — the 1990s default) | 20.5 ms | **227 ms** | 48.6 Mbit/s |
| `sfq` | 20.5 ms | 20.5 ms | full — but 25 ms still sitting in the queue |
| `fq_codel` | 20.5 ms | 20.5 ms | full, **1 ms** of queue |
| `cake` | 20.5 ms | 20.5 ms | full, 1 ms of queue, **and it shapes itself** |

Measured on the 3-box lab in this repo, 50 Mbit uplink, 20 ms emulated internet.

---

## CAKE in six lines (video 2)

One line fixes the upload. The full fix — both directions, fair per machine through NAT —
is six:

```bash
# upload
sudo tc qdisc replace dev eth0 root cake bandwidth 50mbit nat dual-srchost ethernet ack-filter
# download: turn arriving traffic into leaving traffic, then queue it
sudo ip link add ifb0 type ifb
sudo ip link set ifb0 up
sudo tc qdisc add dev eth0 handle ffff: ingress
sudo tc filter add dev eth0 parent ffff: matchall action mirred egress redirect dev ifb0
sudo tc qdisc replace dev ifb0 root cake bandwidth 100mbit nat dual-dsthost ethernet ingress
```

Same lab with a "bad modem" (100/50, 1000-packet FIFO), same traffic, only the queue changed:

| problem | mechanism | FIFO → CAKE |
|---|---|---|
| lag under load | COBALT (CoDel + BLUE) | call 209 ms → **20.6 ms** |
| small packets stuck behind big ones | sparse flows go first | a 9.4 Mbit stream at idle latency next to a full upload |
| a UDP flood that won't back off | per-flow queues | TCP squeezed out → **23 of 50 Mbit** |
| one machine with 8 connections | `nat dual-srchost` | 42 / 5 → **24 / 23 Mbit** |
| calls vs backups | tins (DSCP) | LE backup yields to ~3 Mbit; EF stays at µs |
| the other direction | shape the download too | upload 35 → **43 Mbit** |
| **libreqos grade** | | **D → A+** |

Every command, what it shows and why: **[docs/cake-explained.md](docs/cake-explained.md)**.
To reproduce it, [`lab/set-modem.sh`](lab/set-modem.sh) `bloat|cake` switches the router and
[`lab/load.sh`](lab/load.sh) starts each chapter's traffic.

---

## Just fix my router

Three self-contained chapters. Pick your box:

| | Guide | What you get |
|---|---|---|
| 🐧 **Linux** | [docs/01-linux-router.md](docs/01-linux-router.md) | cake both directions, per-host fair through NAT, persists across reboots |
| 📡 **MikroTik / RouterOS** | [docs/02-mikrotik.md](docs/02-mikrotik.md) | queue tree + cake, with the two traps that silently make it do nothing |
| 🛜 **UniFi** | [docs/03-unifi.md](docs/03-unifi.md) | Smart Queues in the GUI, and the SSH route if you want real cake |

The short version for a Linux router, if you just want to paste something:

```bash
WAN=eth0 UP=45mbit DOWN=90mbit sudo ./deploy/linux/cake-sqm.sh
```

Set `UP`/`DOWN` to **~90% of your measured rates**, not your advertised tier. Measure
the line unshaped first — **at busy hours**, when everyone's online. The 10% you give up is
what buys you the queue. (A 500 Mbit line that only delivers 300–370 in the evening got
*worse* with CAKE at 440, and A+ at 300.)

**Then test it.** [waveform.com/tools/bufferbloat](https://www.waveform.com/tools/bufferbloat)
or [test.libreqos.com](https://test.libreqos.com) — run it from a wired machine behind
the router, before and after. You are looking for the latency-under-load grade, not the
speed number.

---

## Build the lab

Everything in the video is reproducible. Two ways to get a lab, depending on how much
hardware you feel like finding:

### Option A — three boxes ([`lab/`](lab/))

The rig the video is shot on: a client, a Linux router with two NICs, and a server that
plays “the internet” (20 ms of emulated distance, then a 100 Mbit pipe with a fat dumb
buffer — i.e. your ISP's modem). Three VMs work fine.

```
                    LAN — fast                          WAN — the slow uplink
 [client .32] ═══════════════════════ [eth1 ▶ ROUTER ◀ eth0] ──────────── [server — "the internet"]
                                     fat pipe        │
                                                     ▼
                                              ★ THE QUEUE ★
```

For video 2 the router also plays the bad modem: `sudo ./lab/set-modem.sh bloat` puts a
100/50 Mbit HTB with a 1000-packet FIFO on both directions, `set-modem.sh cake` swaps in
the six lines. See [lab/README.md](lab/README.md#video-2-the-cake-deep-dive).

### Option B — one laptop, zero hardware ([`netns-lab/`](netns-lab/))

The same three-box topology built out of network namespaces on a single Linux machine.
Nothing to cable, nothing to reboot, `teardown-testbed.sh` deletes all of it. This is
the fastest way to see any of this for yourself:

```bash
sudo ./netns-lab/setup-testbed.sh
sudo ./netns-lab/set-qdisc.sh pfifo     && sudo ./netns-lab/measure.sh
sudo ./netns-lab/set-qdisc.sh fq_codel  && sudo ./netns-lab/measure.sh
sudo ./netns-lab/set-qdisc.sh cake      && sudo ./netns-lab/measure.sh
sudo ./netns-lab/teardown-testbed.sh
```

It is an older, simpler concept than the video's rig — one machine instead of three — but
every measurement in it is real, and it carries a few demos the video had to cut (HTB
class borrowing, ECN marking instead of dropping).

---

## The tools ([`tools/`](tools/))

Both are single files, stdlib only, no packages to install.

- **`cakemeter.py`** — the meter from the video. Runs on the client, serves a web UI on
  `:8420`: runs the load, charts ping and throughput, and streams the router's *own queue*
  over SSH twice a second. Backlog and drain time are the numbers that tell sfq, fq_codel
  and cake apart — all three give you the same flat ping.
- **`caketune.py`** — runs on the router, `:8421`. Every knob cake has as a form control
  that builds a real `tc` command, plus live per-tin telemetry.

```bash
uv run tools/cakemeter.py --router 192.168.80.31 --target 192.168.12.120
uv run tools/caketune.py --wan eth0
```

> ⚠️ Both pages can reconfigure the router and neither has any authentication.
> Bind them to `127.0.0.1` on a network that is not exclusively yours.

---

## The whole videos, as commands

- [docs/video-commands.md](docs/video-commands.md) — video 1: every command, chapter by
  chapter, in order, with what each one is for.
- [docs/cake-explained.md](docs/cake-explained.md) — video 2: the six lines, the six
  problems they fix, how to read `tc -s qdisc show` for cake, the measured DSCP → tin map,
  and tuning for your own line.

Useful if you are following along or just want the `tc` lines without the narration.

---

## Repository map

```
docs/     01-linux-router.md  02-mikrotik.md  03-unifi.md   deploy guides
          video-commands.md                                  video 1, as a command list
          cake-explained.md                                  video 2: cake, taken apart
lab/      setup-{router,client,server}.sh                     the 3-box rig
          setup-router-{cake,org}.sh  set-modem.sh            bad modem vs cake, at matched rates
          load.sh                                             each chapter's traffic, one command
          tins.sh  flowwatch.sh  dscpmap.sh                   reading cake's queue on the router
netns-lab/ setup-testbed.sh + set-qdisc.sh + 8 demos          the one-machine lab
tools/    cakemeter.py  caketune.py                           the meter and the tuner
deploy/   linux/cake-sqm.sh  mikrotik/mikrotik-home-router.rsc  paste-and-go configs
```

## Further reading

- [bufferbloat.net](https://www.bufferbloat.net/) — the project that named and fixed this
- `man tc-cake`, `man tc-fq_codel`, `man tc-sfq` — genuinely good man pages
- [RFC 8290](https://datatracker.ietf.org/doc/html/rfc8290) — fq_codel
- [OpenWrt SQM](https://openwrt.org/docs/guide-user/network/traffic-shaping/sqm) — the same idea, packaged

## License

MIT — see [LICENSE](LICENSE). Use anything here on your own network.
