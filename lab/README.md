# The 3-box lab

The rig the video is shot on. Three machines — VMs are fine — one of which is a Linux
router with two NICs.

```
                    LAN vlan80                          "WAN" vlan12
  [client .32] ────────────────────── [router .31] ─────────────────── [server .33]
  192.168.80.32 + .42            eth1       eth0                 192.168.12.120
   default via .31          192.168.80.31  192.168.12.100     plays "the internet"
                                                                + the ISP's modem
                              masquerade oif eth0
```

| Box | Job |
|---|---|
| **client** `.32` | You, your laptop. Also carries `.42` as a second address so one machine can play two hosts in the fairness tests. |
| **router** `.31` | The only box that matters. `eth0` = WAN (the slow uplink), `eth1` = LAN, `ip_forward=1`, masquerade out `eth0`. Every qdisc in the video goes on `eth0`'s egress. |
| **server** `.33` | "The internet" *and* the ISP's modem: `netem delay 20ms` (distance) feeding `tbf rate 100mbit limit 1500000` (a 1.5 MB dumb buffer — an entirely ordinary size for a modem, and the problem). Runs `iperf3` on 5201 and 5202. |

Order matters and is physically honest: distance first, then the bottleneck.

> **Prefer no hardware?** [`../netns-lab/`](../netns-lab/) builds the same topology out of
> network namespaces on one machine. Start there if you just want to see it work.

## Bring it up

Everything `tc` and `nft` does here is **runtime-only** — a reboot wipes it, which is why
these scripts exist. Addresses and `ip_forward` are expected to come from
systemd-networkd / `sysctl.d` and be correct at boot.

```bash
sudo ./setup-server.sh      # on .33 — 20 ms + 100 mbit + iperf3 listeners
sudo ./setup-router.sh      # on .31 — forwarding, NAT, and the cold-open bottleneck
sudo ./setup-client.sh      # on .32 — IPv6 off, offloads off, second address, default route
```

The router is left staged with the villain: a 50 Mbit HTB class with a dumb 1000-packet
FIFO under it. Tear that down (`sudo tc qdisc del dev eth0 root`) before you start playing
with real qdiscs.

### Things these scripts do on purpose

- **IPv6 off on the client.** Its v6 default route is learned by RA from a different router
  on the shared LAN, and `test.libreqos.com` is dual-stack. Left on, a browser test runs
  around everything you configure and you measure nothing.
- **Offloads off** (`gro`/`gso`/`tso`) on all three. Otherwise the qdisc sees 64 KB
  superpackets and every packet count in the video is a lie.
- **Send buffer clamped** on the client to 4 MB (`tcp_wmem`). This is cosmetic and it is a
  *deviation* from the kernel default, not a restoration of one. Against a bloated FIFO,
  TCP autotunes a multi-megabyte send buffer and iperf3's per-second accounting quantises
  badly — the *sender's* counter moves in gulps of `sndbuf/2` while the wire is perfectly
  steady ("23 / 23 / 11" at 20 Mbit, "25.2 / 25.2 / 25.2 / 0.00" at 32 MB). The receiver
  reads flat throughout either way. It changes what the numbers look like, not what they are.
- **Blackhole routes on the server** for the client's two addresses. The server has a
  second leg on the client's LAN, so without these it answers an unmasqueraded client
  *directly*, in 0.4 ms, on a path that skips the router entirely — and the client appears
  to have working internet with no NAT at all. The blackholes make that case fail cleanly,
  the way the real internet fails for an RFC1918 source.
- `SKIP_NAT=1 sudo ./setup-router.sh` leaves the masquerade off, so you can film
  `lsmod | grep -c nf_conntrack` going 0 → 3 when you add it. That beat only works on a
  router that has never had NAT since boot.

### SSH between the boxes

The client is not directly reachable from outside — the router masquerades its replies —
so ssh to it goes through the router as a jump host:

```bash
ssh -J 192.168.80.31 192.168.80.32
```

`./deploy-scripts.sh` pushes these scripts to all three boxes and verifies checksums;
`./deploy-scripts.sh --check` verifies without changing anything. This directory is the
source of truth, and a stale copy on a box is silent and expensive.

## The real-internet A/B (chapter 17)

Two halves of one test, at **matched rates**, so the only thing that differs between them
is what happens once the pipe is full:

```bash
sudo ./setup-router-org.sh      # baseline: HTB + 1000-packet pfifo, 100 down / 50 up
#   ...run a bufferbloat test from the client...
sudo ./setup-router-cake.sh     # cake both directions, same rates
#   ...run the identical test again...

sudo ./setup-router-cake.sh off # tear it all down
```

Both take your own rates: `DOWN=270mbit UP=40mbit sudo ./setup-router-cake.sh`. Measure
your line first and shape to ~90% of what it actually delivers, not what the ISP sells you.
The comparison is only honest if both halves run the same numbers.

Result in the video: **C / 78 ms** → **A+ / ~1 ms**. Same machine, same ISP, only the
router's qdisc changed.

## Video 2: the CAKE deep-dive

Same three boxes. The difference: the **router** plays the bad modem, so it can be swapped
for cake with one command while the traffic keeps running.

```bash
# router .31
sudo ./set-modem.sh bloat     # 100 down / 50 up, HTB + 1000-packet pfifo, both directions
sudo ./set-modem.sh cake      # the six lines: cake both directions, same rates
sudo ./set-modem.sh show      # what's on eth0 and ifb0 right now
sudo ./set-modem.sh off       # bare WAN
```

`set-modem.sh` is a thin wrapper around `setup-router-org.sh` and `setup-router-cake.sh`,
so it must sit next to them. It is also the A/B below, under a shorter name.

On the client, [`load.sh`](load.sh) starts each chapter's traffic in the background, so you
can flip the router between `bloat` and `cake` and watch the difference:

```bash
# client .32
./load.sh intro       # upload + LE "torrent" + EF call + ping + roommate's 8 streams
./load.sh standing    # problem 1: one upload + ping
./load.sh sparse      # problem 2: one upload + a 9.4 Mbit unmarked stream
./load.sh bully       # problem 3: TCP vs a 50M UDP flood
./load.sh roommate    # problem 4: 8 streams from .32, 1 from .42
./load.sh tins        # problem 5: two normal uploads + one LE
./load.sh voice       # problem 5: upload + 1M EF UDP   (./load.sh cheat: greedy TCP marked EF)
./load.sh bothways    # problem 6: upload + download at once
./load.sh dashboard   # chapter 11: upload, LE upload, ping, EF call
./load.sh status      # last line of each running test
./load.sh stop
```

It needs `iperf3` listeners on 5201-5204 and `irtt server -i 0` on the server —
`setup-server.sh` starts both — and `irtt` on the client (`apt install irtt`).

Reading the queue on the router:

| script | what it shows |
|---|---|
| `watch -n1 tc -s qdisc show dev eth0` | everything, as in the video |
| `watch -n1 ./tins.sh [eth0\|ifb0]` | just the per-tin rows: thresh, delays, backlog, pkts, drops, flows |
| `./flowwatch.sh [seconds] [dev]` | one line per second: sparse/bulk flow gauges and Best Effort `av_delay` / `sp_delay` |
| `./dscpmap.sh` | pings once per DSCP code point and reports which tin's counter moved — the map on *your* kernel |
| `sudo conntrack -L -p tcp \| grep ESTABLISHED \| grep -E 'dport=520[12] '` | why `dual-srchost` without `nat` does nothing |

The walkthrough, with every number from the video: [docs/cake-explained.md](../docs/cake-explained.md).

## Adapting this to your addresses

The addresses are hardcoded at the top of each script (`192.168.80.0/23` for the LAN,
`192.168.12.0/24` for the "WAN", `eth0`/`eth1` as the interface names). Change them there,
in one place per script, and run `./deploy-scripts.sh`.
