# Chapter 3 — bufferbloat on UniFi

UniFi is the awkward one. There is a GUI fix that takes thirty seconds and works, and
there is a proper cake fix that Ubiquiti does not support and that your gateway may not be
able to run. Both are below. Start with the GUI one — for most people it is the end of the
story.

---

## Option A — Smart Queues (the supported way)

**Settings → Internet → [your WAN] → Advanced → `Manual` → Smart Queues**, then set the
two rate fields.

- It is **off by default**.
- Set **Download** and **Upload** to **~90% of your measured rates** (85–95% is the working
  range). Measure the line unshaped first, from a wired client. If you have 50 Mbit down,
  enter 45.
- Available on the UniFi gateways — UXG Lite / Max / Pro / Enterprise, the UDM family, and
  the legacy USG line. If your gateway and firmware don't show the option, it isn't
  supported on that model.

That's it. Re-run a bufferbloat test and you should go from a C or D to an A.

### What it actually is, and the two things to know

Smart Queues is Ubiquiti's SQM: an **fq_codel**-family shaper running in software on the
gateway CPU. So it is chapter 13 of the video, not chapter 14 — you get fair queueing and
controlled delay, which is 95% of the win. You do not get cake's per-host isolation
through NAT, its ack filter, or its link-layer overhead accounting.

**1. It disables the hardware offload path** for WAN traffic on most models. This is not a
bug and there is no way around it: offload exists precisely to skip the queueing your
gateway now has to do. That is where the throughput collapse people report comes from.

**2. Ubiquiti recommends against it above ~300 Mbps**, for CPU reasons. Historical
per-model ceilings were around 85 Mbps on the original USG and 250 Mbps on the USG-Pro;
newer gateways go higher, but every one of them has a ceiling.

If your line is faster than your gateway can shape, you have three honest options:

- Shape anyway at a rate your gateway *can* sustain, and accept the cap. A shaped
  300 Mbit that grades A+ genuinely feels better than an unshaped 900 Mbit that grades C.
- Shape **upload only**, which is usually where the pain is and costs the least CPU.
- Put a box that can do the job in front of the gateway — a small Linux router or OpenWrt
  device running [chapter 1](01-linux-router.md). This is the pragmatic answer on a
  gigabit line.

---

## Option B — real cake over SSH (unsupported, and it fights back)

UniFi OS is Debian underneath, so if the kernel has `sch_cake` you can run the exact
commands from [chapter 1](01-linux-router.md). Three obstacles, in order:

### 1. Is cake even there?

SSH into the gateway (enable SSH in **Settings → System → Advanced**), then:

```bash
modprobe sch_cake && tc qdisc add dev lo root cake && tc qdisc del dev lo root && echo "cake: works"
```

`sch_cake` is in mainline since 4.19 and UniFi OS kernels are 4.19+, but whether it is
*compiled* for your model and firmware varies. If that fails, stop here — Option A is your
answer, or Option B on different hardware.

### 2. Which interface is the WAN?

It is not `eth0`, and it differs per model and per WAN type.

```bash
ip -br a                        # find the one with your public/ISP address
tc -s qdisc show dev <that>     # confirm — and see what Smart Queues put there
```

Typically `eth8` on a UDM-Pro, an `eth*`/`ethN` on UXG hardware, and **`ppp0` if you are
on PPPoE** — with PPPoE you must shape `ppp0`, not the physical port underneath it.

**Turn Smart Queues off in the GUI first.** Two shapers stacked on one interface is a
worse configuration than either alone.

### 3. Nothing you type survives

`tc` state is wiped by a reboot *and* by a controller provision — which can happen any
time you change an unrelated setting. So a one-off `tc qdisc replace` is a demo, not a
deployment.

For persistence, `/data` and `/etc` survive firmware updates on UniFi OS, which is what
the community boot-script tooling uses — e.g.
[unifios-utils](https://github.com/johnstonjs/unifios-utils), which installs a systemd
service that runs scripts symlinked into its `enabled/` directory on every boot. Put
[`deploy/linux/cake-sqm.sh`](../deploy/linux/cake-sqm.sh) there with your WAN interface
and rates:

```bash
WAN=eth8 UP=45mbit DOWN=90mbit /data/cake-sqm.sh on
```

### Know what you are signing up for

None of this is supported by Ubiquiti, it can break on any firmware release, and it will
not appear anywhere in the UI — the next person to look at this network (including you, in
a year) will have no idea it is there. Leave a comment in the script saying why it exists.

**Honest recommendation:** if Smart Queues gets you an A, take the A. Option B is worth it
when you specifically need what cake adds over fq_codel: per-host fairness through NAT
(the roommate-with-eight-torrents problem), `ack-filter` on a badly asymmetric line, or
correct DOCSIS/PPPoE overhead accounting.

---

## Verify, either way

Run [waveform.com/tools/bufferbloat](https://www.waveform.com/tools/bufferbloat) or
[test.libreqos.com](https://test.libreqos.com) from a **wired** client behind the gateway,
before and after. Watch the latency-under-load grade, not the speed number — and expect
the speed number to drop by about 10%. That is the rent you pay for owning the queue.

Wi-Fi has its own queues and its own bufferbloat problem, which no amount of WAN shaping
fixes. Test wired first, always.
