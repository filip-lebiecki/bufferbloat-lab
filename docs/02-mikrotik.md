# Chapter 2 — cake on MikroTik / RouterOS

RouterOS 7.1+ ships cake as a queue type, so you get the same algorithm as Linux — with
two structural differences that will silently make your config do **absolutely nothing**
if you miss them.

Paste-and-go config: [`deploy/mikrotik/mikrotik-home-router.rsc`](../deploy/mikrotik/mikrotik-home-router.rsc)

```
/import mikrotik-home-router.rsc
```

Edit `ether1` → your WAN interface, and `45M`/`90M` → ~90% of your **measured** rates,
before you run it.

---

## The three traps, first, because they are the whole chapter

### Trap 1 — FastTrack must be off, or nothing is shaped

This config classifies traffic with packet marks. **Fasttracked packets never get marks**,
so the queues see no traffic and shape nothing — silently, with no error anywhere. Stock
RouterOS ships a fasttrack rule in its default firewall, so assume you have one:

```
/ip firewall filter print where action=fasttrack-connection
/ip firewall filter disable [find action=fasttrack-connection]
```

Confirm — both must be true:

```
/ip settings print                                              # ipv4-fasttrack-active: no
/ip firewall filter print where action=fasttrack-connection     # empty
```

The cost is real: FastTrack existed to save CPU. Shaping costs CPU. That is the trade.

### Trap 2 — `cake-bandwidth` does not shape. At all.

This is the one that eats afternoons, because the config *looks* right and the counters
*look* alive. Measured on an RB5009 running RouterOS 7.23.2:

| Config | Actual throughput |
|---|---|
| `cake-bandwidth=420M`, no `max-limit` | **490 Mbps** — ignored entirely |
| `cake-bandwidth=50M`, no `max-limit` | **206 Mbps** — 4× over |
| `max-limit=440M`, `cake-bandwidth=0` | caps at 435. Waveform A+ |

`max-limit` on the queue **tree** enforces the rate. `cake-bandwidth` on the queue **type**
contributes nothing — `max-limit=440M` with `cake-bandwidth=450M` measured identically to
`cake-bandwidth=0`. HTB shapes; cake does AQM, flow isolation and per-host fairness, which
is all it was ever doing here.

So: put the rate on `max-limit`, in one place, and leave `cake-bandwidth=0`. Don't
"helpfully" mirror the rate into both — it buys nothing and creates two numbers that can
drift apart.

The tree will happily report growing byte counters and `dropped=0` while passing 4× its
configured rate. A config review will not catch this. **Only a measured rate cap will.**

### Trap 3 — the IPv6 mangle rules are not optional

`/ip firewall mangle` does not see IPv6 traffic at all. Without a duplicate set of rules
under `/ipv6 firewall mangle`, your entire IPv6 traffic is unshaped in both directions —
and IPv6 is what most large sites and most speed tests actually use. You will get a good
grade and a bad connection.

(Native IPv6 on the WAN only. IPv6 riding a 6in4/HE tunnel is encapsulated as IPv4
proto-41 to the router itself, so forward-chain rules miss it in both directions.)

---

## The configuration

**1. Two queue types** — one per direction:

```
/queue type
add name=cake-up kind=cake cake-bandwidth=0 cake-diffserv=diffserv3 \
    cake-flowmode=dual-srchost cake-nat=yes cake-ack-filter=filter \
    cake-rtt-scheme=internet cake-overhead-scheme=ethernet
add name=cake-down kind=cake cake-bandwidth=0 cake-diffserv=diffserv3 \
    cake-flowmode=dual-dsthost cake-nat=yes \
    cake-rtt-scheme=internet cake-overhead-scheme=ethernet
```

`cake-nat=yes` + `dual-srchost`/`dual-dsthost` is per-LAN-machine fairness seen through
the NAT — the same pairing as Linux. `cake-ack-filter` on upload only.

**2. Direction classifiers** — a `parent=global` tree sees both directions at once and
cannot tell them apart, so mangle supplies the direction. `in-interface=<wan>` is
download, `out-interface=<wan>` is upload. Written **twice**, IPv4 and IPv6:

```
/ip firewall mangle
add chain=forward action=mark-packet new-packet-mark=wan-dl passthrough=no in-interface=ether1
add chain=forward action=mark-packet new-packet-mark=wan-ul passthrough=no out-interface=ether1

/ipv6 firewall mangle
add chain=forward action=mark-packet new-packet-mark=wan-dl passthrough=no in-interface=ether1
add chain=forward action=mark-packet new-packet-mark=wan-ul passthrough=no out-interface=ether1
```

**3. The trees** — `max-limit` is the rate, and the only place it appears:

```
/queue tree
add name=sqm-download parent=global packet-mark=wan-dl queue=cake-down max-limit=90M
add name=sqm-upload   parent=global packet-mark=wan-ul queue=cake-up   max-limit=45M
```

---

## Verifying

```
/queue tree print stats
```

Run a speed test while you watch it. **Climbing byte counters only prove classification.**
What you need to see is the **rate capping at your configured value**. Then run a
bufferbloat test — [test.libreqos.com](https://test.libreqos.com) is the better one here,
because it adds a bidirectional phase that waveform.com lacks.

Do **not** judge cake by RouterOS's queue counters:

- `dropped=0` always, working or not — the counter simply isn't populated.
- `queued-bytes` shows the *bulk* flow's backlog (~2 MB under a saturated download). That
  is not added latency: cake's flow isolation keeps interactive traffic out of that queue.
  Dividing it by the rate to "compute" latency produces a scary number that is wrong.

End-to-end measured latency is the only signal that means anything.

---

## Hardware reality check

cake runs on the CPU and FastTrack is off, so throughput is bounded by the box:

| Device | Realistic cake throughput |
|---|---|
| hEX / hAP | ~100–200 Mbit |
| RB5009 | 440 Mbps at 37–50% CPU on one direction |
| CCR class | gigabit-ish |

If your line is faster than your router, shape only the direction that hurts (usually
upload), or accept a lower shaped rate. A shaped 300 Mbit with A+ latency beats an
unshaped 500 Mbit that grades C.

---

## The alternative that keeps FastTrack

FastTrack skips simple queues and `parent=global` trees, but **not** queues attached to an
interface:

```
/queue tree
add name=sqm-upload   parent=ether1 packet-mark=no-mark queue=cake-up   max-limit=45M
add name=sqm-download parent=bridge packet-mark=no-mark queue=cake-down max-limit=90M
```

`packet-mark=no-mark` is required (fasttracked packets carry no marks), and no mangle
rules are needed at all — including for IPv6 — because an interface queue sees every
packet regardless of protocol.

**The catch:** this does not work on all hardware, and it fails *silently*. On an RB5009,
trees parented to the WAN or to a trunk VLAN reported `ACTIVE-QUEUE=queue-tree` and moved
exactly 0 bytes; RouterOS refused a software queue outright with *"non rate limit queues
are useless on this interface"*. Typical on hardware-queue-only ports and on VLANs sharing
a trunk. If `/queue tree print stats` shows zero bytes during a speed test, that is this
failure — go back to the `parent=global` version.

In one line: interface-parented keeps FastTrack but is not portable; `parent=global` is
portable but costs you FastTrack.
