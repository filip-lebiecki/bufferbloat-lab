# cakemeter and caketune

Two single-file web tools. Stdlib only, no packages, no build step — they start offline.
Both are written for [uv](https://docs.astral.sh/uv/) but plain `python3 file.py` works
just as well.

> ⚠️ **Neither has any authentication, and both can reconfigure a router.** Bind them to
> `127.0.0.1` on any network that is not exclusively yours.

---

## cakemeter.py — the meter from the video

Runs on the **client**, serves a UI on `:8420`.

```bash
uv run cakemeter.py                                          # 0.0.0.0:8420
uv run cakemeter.py --host 127.0.0.1 --router 192.168.80.31 --target 192.168.12.120
```

| Flag | Default | |
|---|---|---|
| `--host` / `--port` | `0.0.0.0` / `8420` | where the UI listens |
| `--target` | `192.168.12.120` | the iperf3 server |
| `--router` | `192.168.80.31` | the box the shaper lives on |
| `--router-iface` | `eth0` | `eth0` for egress, `ifb0` for the ingress shaper |

**Measuring** — starts iperf3 and ping the way the video does (start the load, let it
settle, *then* ping), parses both live, and charts ping and throughput against a table of
averages. Beyond a plain upload or download it runs the scenarios: a hostile UDP
neighbour, host fairness across two source addresses, sparse-flow latency, and the ECN
demo.

**Watching** — streams `tc -s -j qdisc show` from the router over one long-lived SSH
connection: the live qdisc, its parameters as chips, and a backlog graph. This is the half
that matters. `sfq`, `fq_codel` and `cake` all report the same flat 20 ms ping under load;
only the queue tells them apart — 25 ms of drain time versus 1 ms. Every run records the
router's config *itself*, so results need no hand-written labels, and freezes the backlog
it saw while measuring into the results table.

**Driving** — applies a qdisc to the router and can build the ingress shaper on `ifb0`.

Needs key-based SSH to the router and passwordless `sudo` there. Results are in memory
only; use the CSV/JSON export buttons to keep a run.

---

## caketune.py — the tuner

Runs **on the router**, serves a UI on `:8421`.

```bash
uv run caketune.py                                  # 0.0.0.0:8421, WAN = eth0
uv run caketune.py --host 127.0.0.1 --wan eth0 --ifb ifb0
```

**Tuning** — every knob cake has as a form control that builds a real `tc` command:
bandwidth, the ack-filter trio, `rtt`, overhead/mpu/link-layer, `nat`, the flow-isolation
modes (`flows` / `dual-srchost` / `dual-dsthost` / `triple-isolate` / …), diffserv tin
sets, `wash`, `split-gso`, `memlimit`, `ingress`. The exact command is shown before it
runs; applying it is one click.

**Watching** — everything `tc -s qdisc show` prints, live at 2 Hz: backlog, drops,
overlimits, requeues, memory, capacity estimate, and the whole per-tin table —
`thresh`, `target`, `interval`, `pk_delay`/`av_delay`/`sp_delay`, `way_inds`/`miss`/`cols`,
drops, marks, `ack_drop`, sparse/bulk/unresponsive flows, `max_len`, `quantum` — plus
derived rates and drain time, charted.

It also builds the ingress side for you (`ifb0` plus the `matchall … mirred egress
redirect` on the WAN) and tears it down as cleanly as it went up.

**Capture windows** let you bracket an external test: hit Start, run a browser bufferbloat
test or `flent`, hit Stop, and the row records the config that was in force and what the
qdisc did during it. `ack-filter` on versus off becomes two rows of one table.

Needs passwordless `sudo` for `tc`/`ip` — reading is unprivileged, only applying is not.

> The `ack-filter` demo needs a genuinely asymmetric link to show anything. On a 100/50 it
> reclaims ~0.2%; at 100/20 it is ~4.7%.
