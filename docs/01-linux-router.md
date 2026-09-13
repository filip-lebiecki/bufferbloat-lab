# Chapter 1 — cake on a Linux router

Everything here runs on the box that sits between your LAN and your modem: a Debian or
Ubuntu machine with two NICs, an OpenWrt device, a Raspberry Pi, a VM — anything with a
modern kernel and `tc`.

You need:

```bash
sudo apt install iproute2          # tc lives here
modinfo sch_cake >/dev/null && echo "cake: present"
```

`sch_cake` has been in mainline Linux since **4.19** (2018), so any current distro has it.
If `modinfo` comes up empty, install `kmod-sched-cake` (OpenWrt) or
`linux-modules-extra-$(uname -r)` (Ubuntu).

---

## Step 0 — measure the line first, and do not skip this

Every number in this chapter depends on knowing what your link *actually* delivers, which
is not what you pay for. With the WAN unshaped:

```bash
# from a wired client, not over Wi-Fi
iperf3 -c <some server> -t 20          # or just run a browser speed test
```

Write down the download and upload figures. **Shape to ~90% of those.** Not 100%, not the
advertised tier.

This is the one idea the whole guide rests on: the queue that is ruining your latency is
in your ISP's equipment, on hardware you cannot log into. You cannot delete that buffer.
So move the queue instead — make *your* box the slowest point in the path, by a small
margin, and the queue physically relocates onto a device where cake can manage it.

90% is the working figure. On a real 490 Mbit line, 94% still graded B and 90% got A+.

---

## Step 1 — the upload, in one line

```bash
sudo tc qdisc replace dev eth0 root cake bandwidth 45mbit
```

That is a complete, working fix for the upload direction. `replace` means it does not
care what was there before, and it takes effect on the next packet — you can run this
with an upload in flight and watch the ping drop.

Verify:

```bash
tc -s qdisc show dev eth0
```

Read two fields and ignore the rest:

- **`dropped`** — what already happened. A healthy-looking interface can drop ~0.1% and
  still be holding your traffic hostage, which is why every monitoring dashboard misses
  this.
- **`backlog`** — present tense. This is how much of your data is sitting in the queue
  *right now*. On a bloated FIFO under load it will read 800+ packets and over a megabyte.
  Under cake it should sit in the single digits.

---

## Step 2 — the full configuration, both directions

Downloads have two problems uploads don't: the queue isn't on your router, and qdiscs only
control traffic *leaving* an interface. So we turn arriving traffic into leaving traffic.

```bash
WAN=eth0

# 1. uploads
sudo tc qdisc replace dev $WAN root cake \
    bandwidth 45mbit nat dual-srchost ethernet ack-filter

# 2. a network card that doesn't exist, to hang a download queue on
sudo ip link add ifb-sqm type ifb
sudo ip link set ifb-sqm up

# 3. a checkpoint on the arrival side of the WAN...
sudo tc qdisc add dev $WAN handle ffff: ingress

# 4. ...that hands every arriving packet to ifb-sqm as though ifb-sqm were sending it
sudo tc filter add dev $WAN parent ffff: matchall \
    action mirred egress redirect dev ifb-sqm

# 5. downloads — plain cake, on a card that isn't real
sudo tc qdisc replace dev ifb-sqm root cake \
    bandwidth 90mbit ingress nat dual-dsthost ethernet
```

Or just run the script, which is those five commands with error handling:

```bash
WAN=eth0 UP=45mbit DOWN=90mbit sudo ./deploy/linux/cake-sqm.sh
sudo ./deploy/linux/cake-sqm.sh status
sudo ./deploy/linux/cake-sqm.sh off
```

### What every keyword is doing

| Keyword | Why it is there |
|---|---|
| `bandwidth 45mbit` | The shaper. ~90% of measured. This is what makes you the bottleneck. |
| `nat` | Look through conntrack. **Without it, per-host fairness is dead behind NAT** — every machine on your LAN appears as the router's single public IP, so cake sees one host and shares per-flow only. If you run NAT, you want this. |
| `dual-srchost` (egress) | Share the link per LAN machine first, then per flow inside each machine. Going out, the host that matters is the one that *sent* the packet. One roommate with eight torrents gets one machine's share, not eight flows' worth. |
| `dual-dsthost` (ingress) | The same idea pointed the other way: coming in, the host that matters is the one the packet is *for*. |
| `ethernet` | Tells cake about the 38 bytes of per-packet framing it otherwise pretends aren't there. Without it you type 45 and put ~46 on the wire — and if the ISP's limit is exactly 45, you just handed the queue back to them. |
| `ack-filter` | Drops redundant TCP acks on the narrow direction. How much it buys depends entirely on **how lopsided** the line is — see the table below. Leave it on; it costs nothing when it finds nothing. |
| `ingress` | Only on the download side. The bytes were already spent crossing the real link before you ever saw them, so cake must count what it throws away against the rate as well. This is also why your download lands slightly under the number you typed. |

> **A note if you also run RouterOS:** `nat` on the *ingress* queue is safe on Linux and
> measured identical either way (A+ at 0.9 ms with `nat`, 0.8 ms with `nonat`, same client
> and same line). On RouterOS the equivalent setting on the download queue is actively
> harmful — see [trap 4 in the MikroTik chapter](02-mikrotik.md). Do not carry that finding
> across; it does not apply here.

`matchall` in step 4 is the match condition — every packet, no exceptions — and
`mirred egress redirect` is the action: take the packet off the arrival path and hand it
to `ifb-sqm` as though `ifb-sqm` were sending it. The packet was arriving; now, as far as
the kernel is concerned, it is leaving a device you own. There is no such thing as a
download queue, so we turned the download into an upload.

### How much `ack-filter` is actually worth

Measured, both directions saturated, 16-second runs:

| | upload @ 100/50 | upload @ 100/20 |
|---|---|---|
| `no-ack-filter` (default) | 43.2 Mbit | 15.7 Mbit |
| `ack-filter` | 43.3 (**+0.2%**) — 55 acks dropped | 16.4 (**+4.7%**) — 16,650 acks dropped |
| `ack-filter-aggressive` | 43.6 (+0.8%) | 17.3 (**+10.5%**) |

The arithmetic behind it: the ack stream for a 91 Mbit download is roughly 3 Mbit. On a
20 Mbit uplink that is 15% of the link, so acks genuinely queue behind each other and the
filter has something to collapse. On a 50 Mbit uplink it is 6%, with enough headroom that
they rarely stack up at all. **100/50 is a 2:1 line, and 2:1 is not lopsided.** If your
line is 5:1 or worse, this is worth real bandwidth; if it is 2:1, it is noise.

### Link-layer overhead: pick the right one

| Your line | Use |
|---|---|
| Ethernet / fibre / most of what people call "fibre" | `ethernet` |
| Cable (DOCSIS) | `docsis` |
| DSL over PPPoE | `pppoe-vcmux` (add `mpu 68`) |
| No idea | `conservative` — costs a little throughput, never under-accounts |

---

## Step 3 — make it survive a reboot

`tc` state is runtime-only. Nothing here survives a reboot, which is the single most
common reason somebody's bufferbloat "comes back".

### systemd (any distro)

```bash
sudo install -m755 deploy/linux/cake-sqm.sh /usr/local/sbin/cake-sqm.sh
sudo cp deploy/linux/cake-sqm.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cake-sqm.service
```

Rates live in the unit file as `Environment=` lines — edit, then
`sudo systemctl restart cake-sqm`.

### PPPoE / dynamic WAN

If your WAN is `ppp0`, the interface does not exist at boot and may come and go. Drive it
from the ppp hooks instead of `network-online.target`:

```bash
sudo tee /etc/ppp/ip-up.d/cake <<'SH'
#!/bin/sh
WAN="$1" UP=45mbit DOWN=90mbit OVERHEAD=pppoe-vcmux /usr/local/sbin/cake-sqm.sh on
SH
sudo chmod +x /etc/ppp/ip-up.d/cake
```

### OpenWrt

Don't do any of this by hand — install the packaged version, which is the same idea with
a UI: `opkg install luci-app-sqm sqm-scripts`, then **Network → SQM QoS**: set your
rates, queue discipline `cake`, queue setup script `piece_of_cake.qos` (or
`layer_cake.qos` if you want DiffServ tins).

---

## Step 4 — prove it

Run a bufferbloat test from a **wired** client behind the router, before and after:

- [waveform.com/tools/bufferbloat](https://www.waveform.com/tools/bufferbloat)
- [test.libreqos.com](https://test.libreqos.com)

You are looking at the **latency under load** grade, not the speed number. The
down+up-at-once phase is where a half-tuned config falls apart.

For the real-internet A/B in the video — the same rates, with and without cake, so the
only difference is what happens once the pipe is full — see
[`lab/setup-router-cake.sh`](../lab/setup-router-cake.sh) and
[`lab/setup-router-org.sh`](../lab/setup-router-org.sh).

Watching it live while it runs: [`tools/caketune.py`](../tools/caketune.py) on the router
gives you backlog, drain time and the full per-tin table at 2 Hz.

---

## Troubleshooting

**The grade barely moved.** Your shaped rate is too close to the real one, so the queue is
still forming upstream. Drop to 85% and retest. If that fixes it, walk it back up.

**Throughput fell off a cliff.** cake shapes in software on one CPU. A small router will
top out somewhere between 100 Mbit and a gigabit. Check `top` during a test — if a core is
pinned, you have found the ceiling. Use `fq_codel` under an `htb` class instead (cheaper),
or shape only the direction that actually hurts (usually upload).

**Download is a few percent under the number I typed.** Expected — that is what `ingress`
does. Don't chase it.

**It works, then stops after a reboot.** Step 3.

**One machine still hogs everything.** You're missing `nat`. Behind masquerade, per-host
isolation without it collapses to per-flow.

**Nothing changed at all.** Confirm the qdisc is where you think:
`tc qdisc show dev eth0` should say `cake`, not `mq` or `pfifo_fast`. If you shaped the
LAN interface instead of the WAN, you shaped the wrong door.

**Still laggy with an A+ grade.** cake only fixes the queue you control. Look upstream —
or at your Wi-Fi, which has its own queues and its own answer (`fq_codel` on the AP).

---

## Undo everything

```bash
sudo tc qdisc del dev eth0 root
sudo tc qdisc del dev eth0 ingress
sudo ip link del ifb-sqm
```

Or `sudo ./deploy/linux/cake-sqm.sh off`.
