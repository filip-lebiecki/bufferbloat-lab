#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
cakemeter — live bufferbloat bench for the CAKE lab.

Runs on the client (.32) and serves a web UI on :8420.

Measuring: starts iperf3 and ping using the video's method
(start the load, wait N seconds, then ping), parses both live, and charts ping
and throughput against a table of averages. Beyond a plain upload/download it
runs the repo's scenario scripts — a hostile UDP neighbour, host fairness
across two source addresses, sparse-flow latency, and the ECN demo.

Watching: streams `tc -s -j qdisc show` from the router over one long-lived
ssh, showing the live qdisc, its parameters, and a backlog graph. Every run
records the router's config itself, so results need no hand-written labels,
and freezes the backlog it saw while measuring into the results table — sfq,
fq_codel and cake all ping 20 ms under load, and only the queue tells them
apart.

Driving: applies a qdisc to the router (the set-qdisc.sh recipes) and can set
up the chapter 6 ingress shaper on ifb0. Both need key-based ssh to the router
and passwordless sudo there.

    uv run cakemeter.py                     # listen on 0.0.0.0:8420
    uv run cakemeter.py --host 127.0.0.1 --router 192.168.80.31

Note that the page can reconfigure the router and is unauthenticated: bind it
to localhost if the network is not yours alone.

Results are kept in memory only — restarting the server gives a clean slate.
Use the CSV/JSON export buttons to keep a run.

Stdlib only, so it starts offline with no package downloads.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_HISTORY = 200          # in-memory only: restarting the server clears the runs

ROUTER = {
    "host": "192.168.80.31",   # where the shaper lives
    "iface": "eth0",           # eth0 for egress, ifb0 for the ingress shaper
    "interval": 0.5,           # seconds between tc samples
    "window": 240,             # samples kept for a late-joining browser
}

# qdiscs that only schedule or classify — the queue we care about is below them
SHELL_KINDS = {"mq", "htb", "prio", "hfsc", "multiq", "clsact", "ingress", "drr"}
IFACE_OK = re.compile(r"^[A-Za-z0-9_.:@-]{1,32}$")
RATE_OK = re.compile(r"^\d{1,7}(kbit|mbit|gbit)$")

# cake shapes on its own. Behind masquerade its per-host isolation collapses to
# per-flow unless it is told to look through NAT — see chapter 7.1.
CAKE_MODES = {
    "cake": "",
    "cake-host": " dual-srchost nat",
}

# RED's thresholds are byte counts, so the same numbers mean a different delay
# on every link — which is precisely why the one AQM the IETF told the internet
# to deploy is the one almost nobody could configure. Deriving them from the
# shaper rate is what a careful operator did by hand in 1998; the video's point
# is that they had to, and that getting it wrong cost throughput.
RED_MIN_MS = 10          # average queue where marking/dropping starts
RED_MAX_MS = 30          # ...and where the drop probability is at maximum
RED_AVPKT = 1500         # ethernet, per tc-red(8)


def rate_bps(rate: str) -> int:
    """'50mbit' as bits per second. RATE_OK has already vetted the shape."""
    m = re.match(r"^(\d+)(kbit|mbit|gbit)$", rate)
    if not m:
        raise ValueError(f"bad rate {rate!r}")
    return int(m.group(1)) * {"kbit": 10 ** 3, "mbit": 10 ** 6, "gbit": 10 ** 9}[m.group(2)]


def red_spec(rate: str) -> str:
    """tc-red(8)'s own recipe, with min/max set to a target queueing delay.

    `max` at least twice `min` or RED synchronises retransmits; `limit` several
    times `max` so the hard tail is not what is really doing the work; `burst`
    is the man page's (2*min + max) / (3*avpkt).
    """
    per_ms = rate_bps(rate) / 8 / 1000                # bytes the link drains in 1 ms
    lo = max(RED_AVPKT, round(per_ms * RED_MIN_MS))
    hi = max(lo * 2, round(per_ms * RED_MAX_MS))
    burst = max(1, round((2 * lo + hi) / (3 * RED_AVPKT)))
    return (f"red limit {hi * 8} min {lo} max {hi} avpkt {RED_AVPKT} "
            f"burst {burst} probability 0.02 bandwidth {rate}")


# leaf qdiscs that need an htb class above them to make a bottleneck, straight
# out of set-qdisc.sh. A value is either the literal tc arguments or a callable
# taking the shaper rate — red is the only one whose settings depend on it.
LEAF_QDISCS = {
    "pfifo": "pfifo limit 1000",
    "sfq": "sfq perturb 10",
    "red": red_spec,
    "fq_codel": "fq_codel",
}


def leaf_args(kind: str, rate: str) -> str:
    spec = LEAF_QDISCS[kind]
    return spec(rate) if callable(spec) else spec


# menu order and wording for the qdisc picker; the page builds its dropdown
# from this, so a new qdisc cannot be added to one half and not the other.
# Order follows the video's ladder, so red sits where the M6 detour does.
QDISC_LABELS = {
    "pfifo": "pfifo limit 1000",
    "sfq": "sfq perturb 10",
    "red": f"red — 1993 AQM ({RED_MIN_MS}/{RED_MAX_MS} ms, tuned to rate)",
    "fq_codel": "fq_codel",
    "cake": "cake (shapes itself)",
    "cake-host": "cake + dual-srchost nat (per-host)",
    "clear": "clear — no shaper",
}
assert set(QDISC_LABELS) == set(LEAF_QDISCS) | set(CAKE_MODES) | {"clear"}, \
    "QDISC_LABELS drifted from the tables that build the tc commands"

DEFAULTS = {
    "label": "",
    "host": "192.168.12.120",
    "port_up": 5201,
    "port_down": 5202,
    "scenario": "up",        # see build_jobs() for the full list
    "duration": 16,          # iperf3 -t
    "settle": 4,             # sleep before the ping starts
    "ping_count": 40,
    "ping_interval": 0.2,
    "idle_count": 10,        # idle pings taken before the load starts
    "streams": 1,            # iperf3 -P
    "tos": "",               # ping -Q, e.g. 0xb8
    "udp_rate": "62M",       # hostile neighbour flood rate (iperf3 -u -b)
    "src_a": "",             # host A source IP; filled in from the route at startup
    "host_b": "192.168.80.42",   # second source IP for the host-fairness test
    "small_size": "256K",    # sparse-flow transfer size (iperf3 -n)
    "small_tries": 3,
}

RATE_ARG_OK = re.compile(r"^\d{1,6}[KMG]?$")
# a value starting with "-" would be read as an option by ssh/iperf3 rather
# than as an address, so addresses are checked before they reach a command line
HOST_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
TOS_OK = re.compile(r"^(0x[0-9A-Fa-f]{1,2}|\d{1,3})$")

SCENARIOS = {
    "up": "upload",
    "down": "download (-R)",
    "both": "both directions",
    "hostile": "hostile neighbour (UDP flood)",
    "hostfair": "host fairness (8 vs 1)",
    "sparse": "sparse flow (256 KB)",
    "ecn": "ECN demo (marks vs drops)",
}


# --------------------------------------------------------------------------
# event bus
# --------------------------------------------------------------------------

_subscribers: set[queue.Queue] = set()
_sub_lock = threading.Lock()


def publish(event: dict) -> None:
    """Fan one event out to every open SSE stream.

    A backed-up subscriber loses its oldest events rather than being dropped:
    disconnecting it would leave that browser connected but permanently blind,
    which looks like a hung page rather than a slow one.
    """
    payload = json.dumps(event)
    with _sub_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()          # make room, oldest first
                    q.put_nowait(payload)
                except (queue.Empty, queue.Full):
                    dead.append(q)
        for q in dead:
            _subscribers.discard(q)


def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=2000)
    with _sub_lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _sub_lock:
        _subscribers.discard(q)


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------

def local_source_ip(target: str) -> str:
    """The address this host uses to reach the server — host A in the fairness
    test, with the .42 alias as host B."""
    try:
        out = subprocess.run(["ip", "-o", "route", "get", target],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"\bsrc\s+(\S+)", out)
        return m.group(1) if m else ""
    except Exception:
        return ""


def interface_addrs(target: str) -> list[str]:
    """Every IPv4 address on the interface used to reach `target`.

    Only needed to pick a different host A when the route prefers the alias
    that the fairness test uses as host B.
    """
    try:
        route = subprocess.run(["ip", "-o", "route", "get", target],
                               capture_output=True, text=True, timeout=5).stdout
        dev = re.search(r"\bdev\s+(\S+)", route)
        if not dev:
            return []
        out = subprocess.run(["ip", "-o", "-4", "addr", "show", "dev", dev.group(1)],
                             capture_output=True, text=True, timeout=5).stdout
        return re.findall(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", out)
    except Exception:
        return []


def build_jobs(cfg: dict) -> list[dict]:
    """The iperf3 processes a scenario runs, straight from the repo scripts.

    `key` names the series; `label` is what the page shows.
    """
    up, down = int(cfg["port_up"]), int(cfg["port_down"])
    scenario = cfg["scenario"]
    if scenario == "up":
        return [{"key": "up", "label": "upload", "port": up}]
    if scenario == "down":
        return [{"key": "down", "label": "download", "port": up, "reverse": True}]
    if scenario == "both":
        return [{"key": "up", "label": "upload", "port": up},
                {"key": "down", "label": "download", "port": down, "reverse": True}]
    if scenario == "hostile":
        # fairness.sh: a UDP flood aimed above the link rate, next to a normal
        # TCP upload. pfifo lets the flood take the queue; sfq/fq_codel/cake
        # hand each flow its share.
        return [{"key": "flood", "label": f"UDP flood {cfg['udp_rate']}", "port": up,
                 "udp": True, "extra": ["-u", "-b", str(cfg["udp_rate"])]},
                {"key": "polite", "label": "polite TCP", "port": down}]
    if scenario == "hostfair":
        # host-isolation.sh / chapter 7.1. Per-FLOW fairness gives the 8-flow
        # host 8/9 of the link. cake's per-host isolation only fixes that if it
        # can see past masquerade — plain cake and bare dual-srchost both still
        # measure ~7:1 here; it takes `dual-srchost nat` to reach 1:1.
        #
        # The scenario names its own flow count — 8, as in host-isolation.sh.
        # -P only raises it. It used to be max(2, -P), so the default -P 1 ran
        # a 2-vs-1 test, and 2 vs 1 sits inside the run-to-run noise on every
        # qdisc: measured 2.0:1 on fq_codel against 1.05:1 on dual-srchost nat.
        n = int(cfg["streams"]) if int(cfg["streams"]) > 1 else 8
        return [{"key": "hostA", "label": f"host A ×{n}", "port": up,
                 "extra": ["-B", str(cfg["src_a"]), "-P", str(n)]},
                {"key": "hostB", "label": "host B ×1", "port": down,
                 "extra": ["-B", str(cfg["host_b"])]}]
    if scenario == "sparse":
        return [{"key": "bulk", "label": "bulk upload", "port": up}]
    return []


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------

def median(values: list) -> float | None:
    """Middle value, Nones skipped.

    The backlog breathes — TCP fills the queue, takes a drop, backs off — so a
    mean would follow the ramp-up as much as the steady state. The median is
    what the panel looked like for most of the run.
    """
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def ping_stats(samples: list[float]) -> dict:
    """min/avg/max/mdev the way iputils computes them, plus p95."""
    if not samples:
        return {}
    n = len(samples)
    avg = sum(samples) / n
    mean_sq = sum(s * s for s in samples) / n
    mdev = math.sqrt(max(mean_sq - avg * avg, 0.0))
    ordered = sorted(samples)
    idx = min(n - 1, round(0.95 * (n - 1)))
    return {
        "min": min(samples),
        "avg": avg,
        "max": max(samples),
        "mdev": mdev,
        "p95": ordered[idx],
        "count": n,
    }


def grade(added_ms: float | None) -> str:
    """Bufferbloat grade from latency added by the load."""
    if added_ms is None:
        return "-"
    for limit, letter in ((5, "A+"), (30, "A"), (60, "B"), (100, "C"), (200, "D")):
        if added_ms < limit:
            return letter
    return "F"


# --------------------------------------------------------------------------
# the run engine
# --------------------------------------------------------------------------

@dataclass
class Run:
    id: str
    cfg: dict
    started: float
    t0: float = 0.0                      # wall clock of iperf launch
    q_t0: float = 0.0                    # queue window: settled load, measuring
    q_t1: float = 0.0                    # queue window: measurement done
    phase: str = "starting"
    idle_ping: list[dict] = field(default_factory=list)
    load_ping: list[dict] = field(default_factory=list)
    series: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    sparse: dict = field(default_factory=dict)
    loss_pct: float | None = None
    qdisc: dict = field(default_factory=dict)
    qdisc_end: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    finished: bool = False

    def public(self) -> dict:
        d = asdict(self)
        return d


class Engine:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.run: Run | None = None
        self.procs: list[subprocess.Popen] = []
        self.cancel = threading.Event()
        self.history: list[dict] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self, cfg: dict) -> str:
        run = Run(id=uuid.uuid4().hex[:8], cfg=cfg, started=time.time())
        with self.lock:
            # claim the slot under the lock, so two requests arriving together
            # cannot both start a run
            if self.run is not None and not self.run.finished:
                raise RuntimeError("a run is already in progress")
            self.run = run
            self.cancel.clear()
            self.procs = []
        threading.Thread(target=self._execute, args=(run,), daemon=True).start()
        return run.id

    def stop(self) -> None:
        self.cancel.set()
        with self.lock:
            procs = list(self.procs)
        for p in procs:
            try:
                p.send_signal(signal.SIGINT)
            except Exception:
                pass

    def _set_phase(self, run: Run, phase: str, note: str = "") -> None:
        run.phase = phase
        publish({"type": "phase", "run_id": run.id, "phase": phase, "note": note})

    # -- workers -----------------------------------------------------------

    def _ping(self, run: Run, kind: str, count: int,
              interval: float) -> tuple[list[dict], float | None]:
        """Run one ping burst, publishing each reply as it lands.

        Deliberately no -O: under bufferbloat every RTT exceeds the 0.2 s
        interval, so -O would report "no answer yet" for a packet that arrives
        perfectly well a moment later. Real loss is taken from ping's own
        summary line and from gaps in icmp_seq once the burst is over.
        """
        cfg = run.cfg
        cmd = ["ping", "-n", "-D", "-i", str(interval), "-c", str(count)]
        if cfg.get("tos"):
            cmd += ["-Q", str(cfg["tos"])]
        cmd.append(cfg["host"])

        samples: list[dict] = []
        loss_pct: float | None = None
        rx = re.compile(r"^\[(\d+\.\d+)\].*icmp_seq=(\d+).*time=([\d.]+)\s*ms")
        stat_rx = re.compile(r"([\d.]+)% packet loss")
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            run.errors.append("ping not found")
            return samples, None
        with self.lock:
            self.procs.append(proc)

        assert proc.stdout is not None
        for line in proc.stdout:
            if self.cancel.is_set():
                proc.terminate()
                break
            m = rx.match(line)
            if m:
                stamp, seq, rtt = float(m.group(1)), int(m.group(2)), float(m.group(3))
                base = run.t0 or stamp
                sample = {"t": round(stamp - base, 4), "seq": seq, "rtt": rtt, "lost": False}
                samples.append(sample)
                publish({"type": "ping", "run_id": run.id, "kind": kind, **sample})
                continue
            m = stat_rx.search(line)
            if m:
                loss_pct = float(m.group(1))
        proc.wait()
        with self.lock:
            if proc in self.procs:
                self.procs.remove(proc)

        # fill in the packets that never came back, so the chart shows them
        if samples:
            seen = {s["seq"] for s in samples}
            first = samples[0]
            origin = first["t"] - first["rtt"] / 1000.0 - (first["seq"] - 1) * interval
            for seq in range(1, count + 1):
                if seq in seen:
                    continue
                lost = {"t": round(origin + (seq - 1) * interval, 4),
                        "seq": seq, "rtt": None, "lost": True}
                samples.append(lost)
                publish({"type": "ping", "run_id": run.id, "kind": kind, **lost})
            samples.sort(key=lambda s: s["seq"])
            if loss_pct is None:
                loss_pct = round(100.0 * (count - len(seen)) / count, 1)
        return samples, loss_pct

    def _small_transfers(self, run: Run, kind: str) -> list[float]:
        """sparse-flow.sh: how long a 256 KB transfer takes, wall clock.

        Under pfifo a brand-new flow's handshake and slow-start crawl through
        the whole standing queue; fq_codel and cake give sparse flows priority.
        """
        cfg = run.cfg
        size = str(cfg["small_size"])
        if not RATE_ARG_OK.match(size):
            run.errors.append(f"bad transfer size {size!r}")
            return []
        times: list[float] = []
        for i in range(max(1, int(cfg["small_tries"]))):
            if self.cancel.is_set():
                break
            cmd = ["iperf3", "-c", cfg["host"], "-p", str(int(cfg["port_down"])),
                   "-n", size, "--json"]
            t0 = time.time()
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
            except FileNotFoundError:
                run.errors.append("iperf3 not found")
                break
            with self.lock:              # so Stop can interrupt this too
                self.procs.append(proc)
            try:
                out, err = proc.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                proc.kill()
                run.errors.append("small transfer timed out")
                break
            finally:
                with self.lock:
                    if proc in self.procs:
                        self.procs.remove(proc)
            ms = round((time.time() - t0) * 1000, 1)
            if proc.returncode != 0:
                if self.cancel.is_set():
                    break                # Stop killed it; that is not a failure
                run.errors.append((out or err or "").strip()[:200])
                continue
            times.append(ms)
            publish({"type": "sparse", "run_id": run.id, "kind": kind,
                     "try": i + 1, "ms": ms})
        return times

    def _iperf(self, run: Run, job: dict, done: dict) -> None:
        cfg = run.cfg
        key = job["key"]
        cmd = [
            "iperf3", "-c", cfg["host"], "-p", str(job["port"]),
            "-t", str(cfg["duration"]), "--json-stream",
        ]
        extra = job.get("extra") or []
        if not extra and int(cfg.get("streams", 1)) > 1 and cfg["scenario"] in ("up", "down", "both"):
            cmd += ["-P", str(int(cfg["streams"]))]
        cmd += extra
        if job.get("reverse"):
            cmd.append("-R")

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            run.errors.append("iperf3 not found")
            return
        with self.lock:
            self.procs.append(proc)

        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if not line.startswith("{"):
                if "error" in line.lower():
                    run.errors.append(line)
                    publish({"type": "error", "run_id": run.id, "message": line})
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev, data = msg.get("event"), msg.get("data", {})
            if ev == "interval":
                s = data.get("sum", {})
                point = {
                    "t": round(float(s.get("end", 0.0)), 3),
                    "mbps": round(float(s.get("bits_per_second", 0.0)) / 1e6, 3),
                    "retr": s.get("retransmits"),
                }
                streams = data.get("streams") or []
                if streams:
                    st = streams[0]
                    if st.get("rtt"):
                        point["rtt_ms"] = round(st["rtt"] / 1000.0, 3)
                    if st.get("snd_cwnd"):
                        point["cwnd_kb"] = round(st["snd_cwnd"] / 1024.0, 1)
                run.series.setdefault(key, []).append(point)
                publish({"type": "iperf", "run_id": run.id, "key": key, **point})
            elif ev == "end":
                # udp fills in sum_sent/sum_received as well as sum, so one
                # path covers both; jitter and loss simply are not there for tcp
                sent = data.get("sum_sent") or {}
                recv = data.get("sum_received") or sent
                entry = {
                    "label": job["label"], "udp": bool(job.get("udp")),
                    # 3 decimals, not 2: the polite flow in the hostile test lands
                    # around 0.16 Mbit/s, and at 2 decimals that is quantised to
                    # 10 kbit steps before anything gets to render it as kbit
                    "sender_mbps": round(float(sent.get("bits_per_second", 0.0)) / 1e6, 3),
                    "receiver_mbps": round(float(recv.get("bits_per_second", 0.0)) / 1e6, 3),
                    "retransmits": sent.get("retransmits"),
                    "seconds": round(float(sent.get("seconds", 0.0)), 2),
                    "bytes": sent.get("bytes"),
                }
                if recv.get("jitter_ms") is not None:
                    entry["jitter_ms"] = round(float(recv["jitter_ms"]), 3)
                if recv.get("lost_percent") is not None:
                    entry["loss_pct"] = round(float(recv["lost_percent"]), 2)
                done[key] = entry
            elif ev == "error":
                msg_txt = data if isinstance(data, str) else json.dumps(data)
                run.errors.append(msg_txt)
                publish({"type": "error", "run_id": run.id, "message": msg_txt})
        proc.wait()
        with self.lock:
            if proc in self.procs:
                self.procs.remove(proc)


    # -- the ECN demo ------------------------------------------------------

    def _read_ecn(self) -> str | None:
        """This machine's current tcp_ecn, so the demo can put it back."""
        try:
            out = subprocess.run(["sysctl", "-n", "net.ipv4.tcp_ecn"],
                                 capture_output=True, text=True, timeout=10)
        except Exception:
            return None
        value = out.stdout.strip()
        return value if value in ("0", "1", "2") else None

    def _set_ecn(self, value: str, run: Run) -> bool:
        res = subprocess.run(["sudo", "-n", "sysctl", "-qw",
                              f"net.ipv4.tcp_ecn={value}"],
                             capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            run.errors.append(f"could not set tcp_ecn={value}: "
                              f"{(res.stderr or '').strip()[:120]}")
            return False
        return True

    def _execute_ecn(self, run: Run, done: dict) -> None:
        """ecn-demo.sh: the same upload twice, congestion signalled two ways.

        With ECN off the AQM drops packets and TCP retransmits. With ECN on it
        sets the CE bit instead — the sender slows down just the same, but
        nothing is lost, so the retransmit count collapses and the qdisc's mark
        counter climbs in its place.

        Only this host's sysctl is touched: it sits at 2 (accept if asked) by
        default, as does the server, so nobody negotiates ECN until one side is
        told to ask. Flipping the client to 1 is enough.
        """
        cfg = run.cfg
        original = self._read_ecn()
        phases = [("0", "ecnoff", "ECN off"), ("1", "ecnon", "ECN on")]
        try:
            for value, key, label in phases:
                if self.cancel.is_set():
                    break
                if not self._set_ecn(value, run):
                    break
                self._set_phase(run, f"ecn-{value}", f"upload with {label}")
                run.labels[key] = label
                publish({"type": "labels", "run_id": run.id, "labels": run.labels})

                before = ROUTER_MONITOR.counters()
                job = {"key": key, "label": label, "port": int(cfg["port_up"])}
                th = threading.Thread(target=self._iperf, args=(run, job, done),
                                      daemon=True)
                th.start()
                th.join(timeout=float(cfg["duration"]) + 30)
                # give the monitor one poll to catch the tail of the transfer
                time.sleep(max(1.0, ROUTER["interval"] * 3))
                after = ROUTER_MONITOR.counters()
                if key in done:
                    for field, name in (("drops", "qdisc_drops"),
                                        ("marks", "qdisc_marks")):
                        a, b = after[field], before[field]
                        done[key][name] = None if (a is None or b is None) else a - b
                    if after["drops"] is None:
                        run.errors.append("router counters unavailable — "
                                          "drop/mark columns are blank")
        finally:
            # back to whatever the machine had, even if the run was cancelled
            if original is not None:
                self._set_ecn(original, run)
            else:
                # never guess at a value we could not read — say it is flipped
                run.errors.append("could not read the original net.ipv4.tcp_ecn, "
                                  "so it was left as the demo set it; check with "
                                  "sysctl net.ipv4.tcp_ecn")

    # -- orchestration -----------------------------------------------------

    def _execute(self, run: Run) -> None:
        cfg = run.cfg
        idle_origin = time.time()
        publish({"type": "run_start", "run_id": run.id, "cfg": cfg,
                 "started": run.started})
        try:
            # 1. idle baseline, before any load exists
            if int(cfg.get("idle_count", 0)) > 0 and not self.cancel.is_set():
                self._set_phase(run, "idle", "baseline ping, link unloaded")
                run.t0 = time.time()          # provisional origin for idle samples
                idle_origin = run.t0
                run.idle_ping, _ = self._ping(
                    run, "idle", int(cfg["idle_count"]), float(cfg["ping_interval"])
                )

            # 1b. sparse-flow.sh: time a small transfer on an idle link first,
            # so the loaded number has something to be compared against
            if cfg["scenario"] == "sparse" and not self.cancel.is_set():
                self._set_phase(run, "sparse-idle", "256 KB transfers, idle link")
                run.sparse["idle_ms"] = self._small_transfers(run, "idle")

            if self.cancel.is_set():
                return self._finish(run, cancelled=True)

            # 2. the load
            done: dict = {}
            threads = []
            run.t0 = time.time()
            run.qdisc = ROUTER_MONITOR.config_summary()
            # idle samples were timed against a provisional origin; now that the
            # real load start is known, re-base them onto it so they sit at the
            # negative time they actually happened (the sparse scenario runs
            # transfers in between, so the gap is seconds, not milliseconds)
            for sample in run.idle_ping:
                sample["t"] = round(idle_origin + sample["t"] - run.t0, 4)

            # the ECN demo is two sequential uploads, not one measured load
            if cfg["scenario"] == "ecn":
                self._execute_ecn(run, done)
                run.summary = self._summarize(run, done)
                return self._finish(run, cancelled=self.cancel.is_set())

            self._set_phase(run, "load", "iperf3 running")
            jobs = build_jobs(cfg)
            run.labels = {j["key"]: j["label"] for j in jobs}
            publish({"type": "labels", "run_id": run.id, "labels": run.labels})
            for job in jobs:
                th = threading.Thread(target=self._iperf, args=(run, job, done),
                                      daemon=True)
                th.start()
                threads.append(th)

            # 3. settle, then measure
            need = float(cfg["settle"]) + int(cfg["ping_count"]) * float(cfg["ping_interval"])
            if cfg["scenario"] == "sparse":
                need += 3 * int(cfg["small_tries"])      # rough, transfers are ~1-3 s
            if need > float(cfg["duration"]):
                msg = (f"load runs {cfg['duration']}s but the measurements need "
                       f"~{need:.0f}s — the tail was measured on an idle link")
                run.errors.append(msg)
                publish({"type": "error", "run_id": run.id, "message": msg})

            settle = float(cfg["settle"])
            self._set_phase(run, "settle", f"waiting {settle:g}s for TCP to fill the queue")
            deadline = time.time() + settle
            while time.time() < deadline and not self.cancel.is_set():
                time.sleep(0.05)

            # the queue window opens where the ping window does: the load is up
            # and the queue has reached whatever depth it is going to hold. The
            # drain that follows iperf3's exit must stay outside it, or a short
            # run spends a third of its samples reading an empty queue.
            run.q_t0 = time.time()

            if cfg["scenario"] == "sparse" and not self.cancel.is_set():
                self._set_phase(run, "sparse-load", "256 KB transfers under load")
                run.sparse["loaded_ms"] = self._small_transfers(run, "loaded")

            if not self.cancel.is_set():
                self._set_phase(run, "measure", "ping under load")
                run.load_ping, run.loss_pct = self._ping(
                    run, "load", int(cfg["ping_count"]), float(cfg["ping_interval"])
                )

            run.q_t1 = time.time()

            self._set_phase(run, "draining", "waiting for iperf3 to finish")
            limit = time.time() + float(cfg["duration"]) + 20
            for th in threads:
                th.join(timeout=max(1.0, limit - time.time()))

            run.summary = self._summarize(run, done)
            self._finish(run, cancelled=self.cancel.is_set())
        except Exception as exc:                      # keep the server alive
            run.errors.append(f"{type(exc).__name__}: {exc}")
            publish({"type": "error", "run_id": run.id, "message": str(exc)})
            self._finish(run, cancelled=True)

    def _summarize(self, run: Run, done: dict) -> dict:
        idle = ping_stats([s["rtt"] for s in run.idle_ping if s["rtt"] is not None])
        load = ping_stats([s["rtt"] for s in run.load_ping if s["rtt"] is not None])
        added = (load["avg"] - idle["avg"]) if (idle and load) else None

        up = done.get("up", {})
        down = done.get("down", {})
        order = list(run.labels) or list(done)
        flows = [{"key": k, **done[k]} for k in order if k in done]

        # a cancelled run never reaches the line that closes the window; take
        # what was gathered up to now rather than filing the run with none
        queue = ROUTER_MONITOR.window(run.q_t0, run.q_t1 or time.time())

        summary = {
            "queue": queue,
            "idle_ping": idle,
            "load_ping": load,
            "loss_pct": run.loss_pct,
            "added_ms": round(added, 2) if added is not None else None,
            "grade": grade(added),
            "scenario": run.cfg.get("scenario"),
            "flows": flows,
            "up_mbps": up.get("receiver_mbps"),
            "up_sender_mbps": up.get("sender_mbps"),
            "up_retransmits": up.get("retransmits"),
            "down_mbps": down.get("receiver_mbps"),
            "down_sender_mbps": down.get("sender_mbps"),
            "down_retransmits": down.get("retransmits"),
        }

        # the number each scenario exists to produce
        if run.cfg.get("scenario") == "hostfair":
            a = (done.get("hostA") or {}).get("receiver_mbps")
            b = (done.get("hostB") or {}).get("receiver_mbps")
            # 0.0 is a real reading here — a host that failed to connect reports
            # exactly that, and it must not be silently dropped as "missing"
            if a is not None and b is not None:
                summary["fairness"] = {"a": a, "b": b,
                                       "ratio": round(a / b, 2) if b else None}
        elif run.cfg.get("scenario") == "hostile":
            flood = (done.get("flood") or {}).get("receiver_mbps")
            polite = (done.get("polite") or {}).get("receiver_mbps")
            if flood is not None and polite is not None:
                summary["fairness"] = {"a": flood, "b": polite,
                                       "ratio": round(flood / polite, 2)
                                       if polite else None}
        elif run.cfg.get("scenario") == "ecn":
            off, on = done.get("ecnoff") or {}, done.get("ecnon") or {}
            summary["ecn"] = {
                "off_retransmits": off.get("retransmits"),
                "on_retransmits": on.get("retransmits"),
                "off_marks": off.get("qdisc_marks"), "on_marks": on.get("qdisc_marks"),
                "off_drops": off.get("qdisc_drops"), "on_drops": on.get("qdisc_drops"),
                "off_mbps": off.get("receiver_mbps"), "on_mbps": on.get("receiver_mbps"),
            }
        elif run.cfg.get("scenario") == "sparse":
            med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
            i, l = med(run.sparse.get("idle_ms") or []), med(run.sparse.get("loaded_ms") or [])
            summary["sparse"] = {
                "idle_ms": run.sparse.get("idle_ms") or [],
                "loaded_ms": run.sparse.get("loaded_ms") or [],
                "idle_med": i, "loaded_med": l,
                "added_ms": round(l - i, 1) if (i is not None and l is not None) else None,
                "factor": round(l / i, 1) if (i and l is not None) else None,
            }
        return summary

    def _finish(self, run: Run, cancelled: bool = False) -> None:
        if run.finished:                 # the error path must not file it twice
            return
        run.finished = True
        run.phase = "cancelled" if cancelled else "done"
        run.qdisc_end = ROUTER_MONITOR.config_summary()
        # a qdisc swapped mid-run makes the result belong to neither config
        changed = bool(run.qdisc and run.qdisc.get("kind")
                       and run.qdisc_end.get("text") != run.qdisc["text"])
        if changed:
            run.errors.append(f"router config changed during the run: "
                              f"{run.qdisc['text']} → {run.qdisc_end['text']}")
        if not run.summary:
            run.summary = self._summarize(run, {})
        record = {
            "id": run.id,
            "label": run.cfg.get("label") or "",
            "qdisc": run.qdisc,
            "qdisc_end": run.qdisc_end,
            "qdisc_changed": changed,
            "cfg": run.cfg,
            "started": run.started,
            "cancelled": cancelled,
            "summary": run.summary,
            "idle_ping": run.idle_ping,
            "load_ping": run.load_ping,
            "series": run.series,
            "labels": run.labels,
            "sparse": run.sparse,
            "errors": run.errors,
        }
        self.history.insert(0, record)
        del self.history[MAX_HISTORY:]
        publish({"type": "run_end", "run_id": run.id, "run": record})

    def snapshot(self) -> dict:
        with self.lock:
            run = self.run
        return {
            "busy": bool(run and not run.finished),
            "current": run.public() if run and not run.finished else None,
            "history": list(self.history),
            "defaults": DEFAULTS,
            "scenarios": SCENARIOS,
            "qdiscs": QDISC_LABELS,
            "router": ROUTER_MONITOR.snapshot(),
        }



# --------------------------------------------------------------------------
# router qdisc monitor
# --------------------------------------------------------------------------

def find_shaper(leaf: dict, classes: list[dict]) -> dict | None:
    """Bits per second the bottleneck is set to, and what enforces it.

    cake carries its own bandwidth; the htb recipes carry it on the class above
    the leaf, which is a different `tc` object entirely.
    """
    if leaf.get("kind") == "cake":
        bw = (leaf.get("options") or {}).get("bandwidth")
        if isinstance(bw, (int, float)):
            return {"kind": "cake", "bps": int(bw) * 8}
        return None
    parent = leaf.get("parent")
    htb = [c for c in classes if c.get("class") == "htb" and c.get("rate")]
    if not htb:
        return None
    match = next((c for c in htb if c.get("handle") == parent), None)
    match = match or max(htb, key=lambda c: c.get("rate", 0))
    return {"kind": "htb", "bps": int(match["rate"]) * 8}


def summarize_qdiscs(qdiscs: list[dict], classes: list[dict] | None = None) -> dict:
    """Reduce `tc -s -j qdisc show` to the one queue that actually holds packets.

    With `mq` root there is one fq_codel per hardware queue, so same-kind leaves
    are summed. With `htb` root the htb shell and its pfifo leaf report identical
    byte counts, so the shell is dropped and the leaf wins.
    """
    real = [q for q in qdiscs if q.get("kind") not in SHELL_KINDS]
    if not real:
        real = [q for q in qdiscs if q.get("kind") not in ("ingress", "clsact")]
    if not real:
        return {"kind": None, "count": 0, "options": {}}

    groups: dict[str, list[dict]] = {}
    for q in real:
        groups.setdefault(q.get("kind", "?"), []).append(q)
    kind, members = max(groups.items(),
                        key=lambda kv: sum(m.get("bytes", 0) or 0 for m in kv[1]))

    total = lambda f: sum(m.get(f) or 0 for m in members)
    # cake keeps its ECN marks per tin; fq_codel keeps one at the top level
    def marks_of(q: dict) -> int:
        tins = q.get("tins")
        if tins:
            return sum(t.get("ecn_mark") or 0 for t in tins)
        return q.get("ecn_mark") or 0

    marks = sum(marks_of(m) for m in members)
    return {
        "ecn_mark": marks,
        "kind": kind,
        "count": len(members),
        "shaper": find_shaper(members[0], classes or []),
        "options": members[0].get("options", {}) or {},
        "bytes": total("bytes"),
        "packets": total("packets"),
        "drops": total("drops"),
        "overlimits": total("overlimits"),
        "requeues": total("requeues"),
        "backlog": total("backlog"),
        "qlen": total("qlen"),
        "parent": members[0].get("parent"),
        "handle": members[0].get("handle"),
    }


def qdisc_spec(kind: str, rate: str, iface: str, ingress: bool,
               tune: str | None = None) -> list[str]:
    """The tc lines that put `kind` on `iface`, as root or under an htb class.

    `tune` is the rate red's thresholds are computed for. It is normally the
    shaper rate; setting it to something else is how the app reproduces the
    1998 failure — a link whose RED was sized for a different link.
    """
    if kind in CAKE_MODES:
        # `ingress` tells cake it is shaping traffic that has already crossed
        # the link, so it accounts for what it drops differently
        return [f"sudo tc qdisc add dev {iface} root cake bandwidth {rate}"
                + (" ingress" if ingress else "") + CAKE_MODES[kind]]
    # r2q 1000 as in set-qdisc.sh: keeps quanta a few packets wide and
    # silences the 'quantum is big' warning
    return [
        f"sudo tc qdisc add dev {iface} root handle 1: htb default 10 r2q 1000",
        f"sudo tc class add dev {iface} parent 1: classid 1:10 "
        f"htb rate {rate} ceil {rate}",
        f"sudo tc qdisc add dev {iface} parent 1:10 {leaf_args(kind, tune or rate)}",
    ]


def apply_ingress(host: str, wan: str, kind: str, rate: str) -> tuple[bool, str]:
    """Chapter 6: shape the download direction through an ifb device.

    Arriving traffic cannot be queued directly — a qdisc only schedules what
    leaves. So every packet arriving on the WAN is redirected out through a
    virtual ifb device, which turns "arriving" into "departing" and gives cake
    something it can shape. Rate below the ISP's, so the queue forms here
    rather than in their modem.
    """
    if not HOST_OK.match(host):
        return False, "bad router address"
    if not IFACE_OK.match(wan) or wan == "ifb0":
        return False, "pick the WAN interface (not ifb0) before enabling ingress"
    if kind != "clear" and not RATE_OK.match(rate):
        return False, "rate must look like 90mbit"
    if kind not in QDISC_LABELS:
        return False, f"unknown qdisc {kind}"

    if kind == "clear":
        # tear down wherever the redirect actually is, not just where this
        # session thinks it put it — otherwise a filter left on another
        # interface would point at an ifb0 that no longer exists
        sweep = ("for d in /sys/class/net/*; do d=${d##*/}; "
                 "if [ \"$d\" != ifb0 ] && "
                 "tc filter show dev \"$d\" parent ffff: 2>/dev/null "
                 "| grep -q ifb0; then "
                 "sudo tc qdisc del dev \"$d\" ingress 2>/dev/null || true; "
                 "fi; done; true")
        steps = ["sudo tc qdisc del dev ifb0 root 2>/dev/null || true",
                 sweep,
                 f"sudo tc qdisc del dev {wan} ingress 2>/dev/null || true",
                 "sudo ip link del ifb0 2>/dev/null || true",
                 f"tc qdisc show dev {wan}"]
    else:
        steps = [
            "sudo modprobe ifb numifbs=1 2>/dev/null || true",
            "sudo ip link add ifb0 type ifb 2>/dev/null || true",
            "sudo ip link set ifb0 up",
            f"sudo tc qdisc del dev {wan} ingress 2>/dev/null || true",
            f"sudo tc qdisc add dev {wan} handle ffff: ingress",
            f"sudo tc filter add dev {wan} parent ffff: matchall "
            f"action mirred egress redirect dev ifb0",
            "sudo tc qdisc del dev ifb0 root 2>/dev/null || true",
            *qdisc_spec(kind, rate, "ifb0", ingress=True),
            "tc qdisc show dev ifb0",
        ]
    return run_on_router(host, steps)


def run_on_router(host: str, steps: list[str]) -> tuple[bool, str]:
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host,
           "set -e; " + "; ".join(steps)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return False, "timed out talking to the router"
    out, err = (res.stdout or "").strip(), (res.stderr or "").strip()
    if res.returncode != 0:
        return False, (err.splitlines()[-1] if err else f"tc exited {res.returncode}")
    return True, out


def apply_qdisc(host: str, iface: str, kind: str, rate: str,
                tune: str | None = None) -> tuple[bool, str]:
    """Rebuild the qdisc on the router, the way set-qdisc.sh does it.

    pfifo/sfq/fq_codel are queue algorithms, not shapers, so they hang off an
    htb class that enforces the bottleneck; cake shapes on its own as root.
    """
    if not HOST_OK.match(host):
        return False, "bad router address"
    if not IFACE_OK.match(iface):
        return False, "bad interface name"
    if kind != "clear" and not RATE_OK.match(rate):
        return False, "rate must look like 50mbit"
    if tune and not RATE_OK.match(tune):
        return False, "tuned-for rate must look like 50mbit"
    if kind not in QDISC_LABELS:
        return False, f"unknown qdisc {kind}"

    steps = [f"sudo tc qdisc del dev {iface} root 2>/dev/null || true"]
    if kind != "clear":
        steps += qdisc_spec(kind, rate, iface, ingress=False, tune=tune)
    steps.append(f"tc qdisc show dev {iface}")
    return run_on_router(host, steps)


class RouterMonitor(threading.Thread):
    """Streams `tc -s -j qdisc show` from the router over one long-lived ssh.

    Re-running ssh twice a second would cost a full handshake each time, so the
    remote side loops and we read one JSON array per line.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.cfg = {"host": ROUTER["host"], "iface": ROUTER["iface"]}
        self.samples: collections.deque = collections.deque(maxlen=ROUTER["window"])
        self.info: dict = {"kind": None, "count": 0, "options": {}}
        self.ifaces: list[str] = []
        self.error: str | None = "connecting…"
        self.proc: subprocess.Popen | None = None
        self.generation = 0
        self._drop_prev = threading.Event()

    # -- public ------------------------------------------------------------

    def configure(self, host: str | None, iface: str | None) -> None:
        with self.lock:
            if host and HOST_OK.match(host.strip()):
                self.cfg["host"] = host.strip()
            if iface and IFACE_OK.match(iface.strip()):
                self.cfg["iface"] = iface.strip()
            self.samples.clear()
            self.info = {"kind": None, "count": 0, "options": {}}
            self.error = "reconnecting…"
            self.generation += 1
            proc = self.proc
        if proc:
            try:
                proc.kill()
            except Exception:
                pass

    def reset_history(self) -> None:
        """Forget buffered samples so a newly applied qdisc starts clean.

        Peak backlog and the chart are read from this window, and a del+add
        zeroes the kernel's counters, so carrying the old samples across a swap
        would show the previous qdisc's peak against the new one's name.
        """
        with self.lock:
            self.samples.clear()
        self._drop_prev.set()      # and no deltas across the discontinuity
        publish({"type": "router", "reset": True})

    def config_summary(self) -> dict:
        """What the router's queue looks like right now, for a run to record.

        This is the thing the label field used to carry by hand: which device,
        which qdisc, and at what rate.
        """
        with self.lock:
            iface, info, err = self.cfg["iface"], dict(self.info), self.error
        kind = info.get("kind")
        if err is not None or kind is None:
            return {"text": "router unavailable", "iface": iface, "kind": None}

        opts = info.get("options") or {}
        shaper = info.get("shaper") or {}
        ingress = iface == "ifb0" or bool(opts.get("ingress"))
        bits = [iface, kind]
        if (info.get("count") or 1) > 1:
            bits.append(f"×{info['count']}")
        if shaper.get("bps"):
            bps = shaper["bps"]
            bits.append(f"{bps / 1e9:.2f}gbit" if bps >= 1e9 else f"{round(bps / 1e6)}mbit")
        if ingress:
            bits.append("ingress")
        if kind == "cake" and opts.get("nat"):
            bits.append(str(opts.get("flowmode") or "nat"))
        # red's thresholds are byte counts, and a byte count means nothing until
        # you know the rate draining it. Recording what they are worth in
        # milliseconds *on this link* is what makes a mistuned row obvious
        # later: 10/30ms is the tuned one, 0.4/1.2ms is red sized for a link
        # twenty-five times slower than the one it ended up on.
        if kind == "red" and shaper.get("bps") and opts.get("min"):
            ms = lambda by: by * 8 * 1000 / shaper["bps"]
            num = lambda v: f"{v:.1f}" if v < 10 else f"{v:.0f}"
            bits.append(f"{num(ms(opts['min']))}/{num(ms(opts['max']))}ms")
        return {
            "text": " ".join(bits), "iface": iface, "kind": kind,
            "count": info.get("count"), "options": opts,
            "shaper": info.get("shaper"), "ingress": ingress,
        }

    def window(self, t0: float, t1: float) -> dict:
        """What the queue held while a run was measuring.

        The Router queue panel carries the number the ladder turns on — a ping
        under load reads the same 20 ms on sfq, fq_codel and cake, and only the
        backlog tells them apart — but the panel is live and clears on the next
        qdisc swap. This freezes that window onto the run so the results table
        can still show it four rungs later.

        `drain_ms` is the backlog expressed as time at the rate the queue was
        actually draining, which is the comparable figure across rates: sfq
        holds ~the same packets at 20 and 50 Mbit, and therefore twice the
        milliseconds at the lower rate.
        """
        if not t0 or t1 <= t0:
            return {}
        with self.lock:
            rows = [s for s in self.samples if t0 <= s["t"] <= t1]
        if not rows:
            return {}
        qlens = [s.get("qlen") for s in rows]
        drains = [s["drain_ms"] for s in rows if s.get("drain_ms") is not None]
        present = [q for q in qlens if q is not None]
        return {
            "qlen": median(qlens),
            "qlen_min": min(present, default=None),
            "qlen_peak": max(present, default=None),
            "backlog": median([s.get("backlog") for s in rows]),
            "drain_ms": median(drains),
            "drain_min_ms": min(drains, default=None),
            "drain_peak_ms": max(drains, default=None),
            "samples": len(rows),
        }

    def counters(self) -> dict:
        """Cumulative drop and ECN-mark totals, for before/after differencing.

        Returns None when there is no live reading — a zero here would be
        indistinguishable from "the queue dropped nothing".
        """
        with self.lock:
            if self.error is not None or self.info.get("kind") is None:
                return {"drops": None, "marks": None}
            return {"drops": self.info.get("drops") or 0,
                    "marks": self.info.get("ecn_mark") or 0}

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "host": self.cfg["host"], "iface": self.cfg["iface"],
                "info": dict(self.info), "error": self.error,
                "ifaces": list(self.ifaces),
                "samples": list(self.samples),
            }

    # -- worker ------------------------------------------------------------

    def run(self) -> None:
        backoff = 2
        while True:
            with self.lock:
                gen = self.generation
            try:
                got = self._session(gen)
            except Exception as exc:
                self._fail(f"{type(exc).__name__}: {exc}")
                got = False
            # a missing interface would otherwise mean an ssh handshake every
            # 2 s for the whole shoot; ease off, but recover the moment it works
            backoff = 2 if got else min(backoff * 2, 10)
            time.sleep(backoff)

    def _fail(self, message: str) -> None:
        with self.lock:
            self.error = message
        publish({"type": "router", "error": message})

    def _session(self, gen: int) -> bool:
        with self.lock:
            host, iface = self.cfg["host"], self.cfg["iface"]
        if not IFACE_OK.match(iface):
            self._fail(f"refusing suspicious interface name {iface!r}")
            return False
        if not HOST_OK.match(host):
            self._fail(f"refusing suspicious router address {host!r}")
            return False

        # one connection does both jobs: announce the interface list once, then
        # stream tc samples. ifb0 usually does not exist until the ingress
        # shaper is set up, so the UI adds it to the list regardless.
        discover = ("echo \"IFACES: $(ip -o link show | awk -F': ' "
                    "'{print $2}' | cut -d@ -f1 | tr '\\n' ' ')\"; ")
        guard = (f"tc qdisc show dev {iface} >/dev/null 2>&1 || "
                 f"{{ echo 'ERR: no interface {iface} on the router'; exit 1; }}; ")
        # classes first, then qdiscs: the htb rate lives on the class, and the
        # qdisc line is what triggers a sample, so class data is always fresh
        body = (f"printf 'C:'; tc -j class show dev {iface} | tr -d '\\n'; echo; "
                f"printf 'Q:'; tc -s -j qdisc show dev {iface} | tr -d '\\n'; echo; "
                f"sleep {ROUTER['interval']}")
        remote = discover + guard + f"while :; do {body}; done"
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
               "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
               host, remote]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        with self.lock:
            self.proc = proc

        prev: dict | None = None
        prev_t = 0.0
        produced = False
        classes: list[dict] = []
        last_identity: tuple | None = None
        noise: collections.deque = collections.deque(maxlen=3)
        assert proc.stdout is not None
        for line in proc.stdout:
            with self.lock:
                if gen != self.generation:
                    break
            line = line.strip()
            if line.startswith("ERR:"):
                self._fail(line[4:].strip())
                break
            if line.startswith("IFACES:"):
                names = [n for n in line[7:].split() if n != "lo"]
                with self.lock:
                    self.ifaces = names
                publish({"type": "router", "ifaces": names})
                continue
            if line.startswith("C:"):
                try:
                    classes = json.loads(line[2:] or "[]")
                except json.JSONDecodeError:
                    classes = []
                continue
            if not line.startswith("Q:"):
                if line:
                    noise.append(line)      # ssh chatter, kept for the error
                continue
            try:
                qdiscs = json.loads(line[2:] or "[]")
            except json.JSONDecodeError:
                continue

            now = time.time()
            if self._drop_prev.is_set():
                prev = None                    # counters just went back to zero
                self._drop_prev.clear()
            info = summarize_qdiscs(qdiscs, classes)
            if info["kind"] is None:
                continue

            # a qdisc swapped from the terminal must clear the window too, not
            # just one applied from the page. del+add always yields a fresh
            # handle, so identity is (kind, handle, options).
            identity = (info["kind"], info.get("handle"),
                        json.dumps(info.get("options"), sort_keys=True))
            if last_identity is not None and identity != last_identity:
                with self.lock:
                    self.samples.clear()
                prev = None
                publish({"type": "router", "reset": True})
            last_identity = identity

            sample = {"t": round(now, 2),
                      "backlog": info["backlog"], "qlen": info["qlen"]}
            if prev and now > prev_t:
                dt = now - prev_t
                d_bytes = max(0, info["bytes"] - prev["bytes"])
                d_drops = max(0, info["drops"] - prev["drops"])
                sample["mbps"] = round(d_bytes * 8 / dt / 1e6, 3)
                sample["dps"] = round(d_drops / dt, 2)
                bps = d_bytes * 8 / dt
                # how long the current backlog takes to drain at the current rate
                sample["drain_ms"] = round(info["backlog"] * 8 / bps * 1000, 1) \
                    if bps > 0 else None
            prev, prev_t = info, now

            produced = True
            with self.lock:
                self.samples.append(sample)
                self.info = info
                self.error = None
            publish({"type": "router", "sample": sample, "info": info,
                     "host": host, "iface": iface})

        proc.wait()
        err = noise[-1] if noise else ""
        with self.lock:
            if self.proc is proc:
                self.proc = None
            stale = gen != self.generation
        if not stale and self.error is None:
            self._fail(err or "tc stream ended")
        return produced


ROUTER_MONITOR = RouterMonitor()


ENGINE = Engine()


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "cakemeter"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    # -- helpers -----------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    MAX_BODY = 1 << 20

    def _body(self) -> dict:
        """The request body as a dict, or {} for anything unusable.

        Everything here is unauthenticated, so a bad Content-Length, an
        oversized body, or valid JSON that simply is not an object must all
        come back as "nothing" rather than as a traceback.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > self.MAX_BODY:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length))
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, PAGE_BYTES, "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(ENGINE.snapshot())
        elif path == "/api/export.csv":
            self._send(200, export_csv().encode(), "text/csv; charset=utf-8")
        elif path == "/api/export.json":
            self._send(200, json.dumps(list(ENGINE.history), indent=2).encode(),
                       "application/json")  # copy: a run may be filing itself
        elif path == "/api/stream":
            self._stream()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/run":
            cfg = dict(DEFAULTS)
            cfg.update({k: v for k, v in self._body().items() if k in DEFAULTS})
            # a number input under a comma locale can hand us "0,2"
            for k in ("settle", "ping_interval"):
                if isinstance(cfg[k], str):
                    cfg[k] = cfg[k].replace(",", ".").strip() or DEFAULTS[k]
            try:
                cfg["duration"] = max(2, int(float(cfg["duration"])))
                cfg["settle"] = max(0.0, float(cfg["settle"]))
                cfg["ping_count"] = max(1, int(float(cfg["ping_count"])))
                cfg["ping_interval"] = max(0.2, float(cfg["ping_interval"]))
                cfg["idle_count"] = max(0, int(float(cfg["idle_count"])))
                cfg["streams"] = max(1, int(float(cfg["streams"])))
                cfg["small_tries"] = max(1, min(10, int(float(cfg["small_tries"]))))
                cfg["port_up"] = int(cfg["port_up"])
                cfg["port_down"] = int(cfg["port_down"])
            except (TypeError, ValueError) as exc:
                return self._json({"error": f"bad parameter: {exc}"}, 400)
            for field in ("host", "host_b"):
                if not HOST_OK.match(str(cfg[field])):
                    return self._json({"error": f"bad address in {field}"}, 400)
            if cfg["src_a"] and not HOST_OK.match(str(cfg["src_a"])):
                return self._json({"error": "bad address in src_a"}, 400)
            if cfg["tos"] and not TOS_OK.match(str(cfg["tos"])):
                return self._json({"error": "tos must look like 0xb8 or 184"}, 400)
            if cfg["scenario"] not in SCENARIOS:
                return self._json({"error": f"unknown scenario {cfg['scenario']}"}, 400)
            if not RATE_ARG_OK.match(str(cfg["udp_rate"])):
                return self._json({"error": "udp rate must look like 62M"}, 400)
            if not RATE_ARG_OK.match(str(cfg["small_size"])):
                return self._json({"error": "transfer size must look like 256K"}, 400)
            if cfg["scenario"] == "hostfair" and not cfg["src_a"]:
                return self._json({"error": "could not work out this host's source IP"}, 400)
            # both -B flags pointing at one address measures a host against
            # itself and reports a healthy ~1:1 on every qdisc — the exact
            # answer the test exists to disprove, and nothing on screen would
            # look wrong. Refuse rather than produce it.
            if cfg["scenario"] == "hostfair" and str(cfg["src_a"]) == str(cfg["host_b"]):
                return self._json({"error": f"host A and host B are both "
                                            f"{cfg['src_a']} — set one of them to "
                                            f"the other address on this interface"}, 400)
            try:
                rid = ENGINE.start(cfg)
            except RuntimeError as exc:
                return self._json({"error": str(exc)}, 409)
            self._json({"run_id": rid, "cfg": cfg})
        elif path == "/api/router":
            body = self._body()
            iface = str(body.get("iface") or "").strip()
            if iface and not IFACE_OK.match(iface):
                return self._json({"error": "bad interface name"}, 400)
            host = str(body.get("host") or "").strip()
            if host and not HOST_OK.match(host):
                return self._json({"error": "bad router address"}, 400)
            ROUTER_MONITOR.configure(host, iface)
            self._json({"ok": True, **ROUTER_MONITOR.snapshot()})
        elif path == "/api/qdisc":
            body = self._body()
            snap = ROUTER_MONITOR.snapshot()
            kind = str(body.get("kind") or "")
            rate = str(body.get("rate") or "50mbit")
            if body.get("ingress"):
                wan = str(body.get("wan") or snap["iface"])
                ok, out = apply_ingress(snap["host"], wan, kind, rate)
                if ok:
                    # watch whichever device now holds the queue
                    ROUTER_MONITOR.configure(None, wan if kind == "clear" else "ifb0")
            else:
                ok, out = apply_qdisc(snap["host"], snap["iface"], kind, rate,
                                      str(body.get("tune") or "") or None)
            if ok:
                ROUTER_MONITOR.reset_history()
            self._json({"ok": ok, "output": out} if ok else {"error": out},
                       200 if ok else 400)
        elif path == "/api/stop":
            ENGINE.stop()
            self._json({"ok": True})
        elif path == "/api/delete":
            rid = self._body().get("id")
            ENGINE.history[:] = [r for r in ENGINE.history if r["id"] != rid]
            self._json({"ok": True})
        elif path == "/api/clear":
            ENGINE.history.clear()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def _stream(self):
        q = subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                try:
                    payload = q.get(timeout=15)
                    chunk = f"data: {payload}\n\n"
                except queue.Empty:
                    chunk = ": keepalive\n\n"
                self.wfile.write(chunk.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            unsubscribe(q)


def export_csv() -> str:
    cols = ["started", "router_config", "config_changed", "label", "scenario",
            "duration_s", "up_mbps", "down_mbps",
            "flows", "retransmits", "idle_avg_ms", "ping_min_ms", "ping_avg_ms",
            "ping_max_ms", "ping_mdev_ms", "added_ms",
            "queue_ms", "queue_peak_ms", "queue_pkt", "queue_peak_pkt",
            "queue_bytes",
            "grade", "loss_pct"]
    rows = [",".join(cols)]
    for r in list(ENGINE.history):
        s, c = r["summary"], r["cfg"]
        lp, ip = s.get("load_ping") or {}, s.get("idle_ping") or {}
        q = s.get("queue") or {}
        retr = s.get("up_retransmits") if s.get("up_retransmits") is not None \
            else s.get("down_retransmits")

        def fmt(v, nd=2):
            return "" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))

        rows.append(",".join([
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["started"])),
            '"' + str((r.get("qdisc") or {}).get("text", "")).replace('"', "'") + '"',
            "yes" if r.get("qdisc_changed") else "",
            '"' + str(r["label"]).replace('"', "'") + '"',
            c.get("scenario", ""), str(c.get("duration", "")),
            fmt(s.get("up_mbps")), fmt(s.get("down_mbps")),
            '"' + " / ".join(f"{f.get('label')} {f.get('receiver_mbps')}"
                             for f in (s.get("flows") or [])) + '"',
            fmt(retr),
            fmt(ip.get("avg"), 3), fmt(lp.get("min"), 3), fmt(lp.get("avg"), 3),
            fmt(lp.get("max"), 3), fmt(lp.get("mdev"), 3), fmt(s.get("added_ms")),
            fmt(q.get("drain_ms"), 1), fmt(q.get("drain_peak_ms"), 1),
            fmt(q.get("qlen"), 1),
            fmt(q.get("qlen_peak")), fmt(q.get("backlog"), 0),
            s.get("grade", ""), fmt(s.get("loss_pct"), 1),
        ]))
    return "\n".join(rows) + "\n"


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cakemeter</title>
<style>
  :root{
    --bg:#0e1116; --panel:#161b22; --panel2:#1c2230; --line:#2a3140;
    --fg:#e6edf3; --dim:#8b97a8; --accent:#4aa8ff; --up:#4aa8ff; --down:#c792ea;
    --ping:#ff8a5b; --idle:#6b7688; --ok:#3fb950; --warn:#d29922; --bad:#f85149;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{padding:18px 20px;max-width:1500px;margin:0 auto}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
         padding:14px 16px;margin-bottom:16px}
  .row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
  label{display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--dim);
        text-transform:uppercase;letter-spacing:.06em}
  input,select{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
        border-radius:6px;padding:7px 9px;font:13px var(--mono);min-width:90px}
  input:focus,select:focus{outline:1px solid var(--accent);border-color:var(--accent)}
  #label{min-width:260px;font-family:inherit}
  button{background:var(--accent);color:#06121f;border:0;border-radius:6px;
         padding:9px 16px;font-weight:600;font-size:13px;cursor:pointer}
  button:disabled{opacity:.4;cursor:not-allowed}
  button.ghost{background:transparent;color:var(--dim);border:1px solid var(--line)}
  button.ghost:hover:not(:disabled){color:var(--fg);border-color:var(--dim)}
  button.ghost.on{color:var(--fg);border-color:var(--accent);background:var(--panel2)}
  button.ghost.small{padding:4px 10px;font-size:11px}
  button.danger{background:transparent;color:var(--bad);border:1px solid var(--line)}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:1000px){.grid{grid-template-columns:1fr}}
  .chart-head{display:flex;justify-content:space-between;align-items:baseline;
              margin-bottom:8px}
  .chart-head h2{font-size:13px;margin:0;letter-spacing:.04em;text-transform:uppercase;
                 color:var(--dim)}
  .legend{display:flex;gap:12px;font:11px var(--mono);color:var(--dim)}
  .legend i{display:inline-block;width:9px;height:9px;border-radius:2px;
            margin-right:5px;vertical-align:middle}
  /* the loss marker is a full-height rule on the chart, so its swatch is one too */
  .legend i.bar{width:3px;height:12px;border-radius:1px}
  canvas{width:100%;display:block}
  .live-nums{display:flex;gap:22px;flex-wrap:wrap;margin:10px 0 2px;font:12px var(--mono)}
  .live-nums b{display:block;font:600 21px/1.2 var(--mono);color:var(--fg)}
  .live-nums span{color:var(--dim);font-size:11px;text-transform:uppercase;
                  letter-spacing:.06em}
  table{width:100%;border-collapse:collapse;font:12px var(--mono)}
  th,td{padding:7px 9px;text-align:right;border-bottom:1px solid var(--line);
        white-space:nowrap}
  th{color:var(--dim);font-weight:500;font-size:10px;text-transform:uppercase;
     letter-spacing:.06em;cursor:pointer;user-select:none}
  th:first-child,td:first-child{text-align:left}
  tbody tr:hover{background:var(--panel2)}
  tbody tr.sel{background:#1d2a3d}
  td.lab{font-family:inherit;max-width:280px;overflow:hidden;text-overflow:ellipsis}
  td.cfg{font:12px var(--mono);color:var(--fg);max-width:250px}
  .g{padding:1px 7px;border-radius:4px;font-weight:700;font-size:11px}
  .g.A\+,.g.A{background:rgba(63,185,80,.16);color:var(--ok)}
  .g.B,.g.C{background:rgba(210,153,34,.16);color:var(--warn)}
  .g.D,.g.F{background:rgba(248,81,73,.16);color:var(--bad)}
  .x{color:var(--dim);cursor:pointer;padding:0 4px}
  .x:hover{color:var(--bad)}
  .err{color:var(--bad);font:12px var(--mono);margin-top:8px;white-space:pre-wrap}
  .rerr{color:var(--warn);font:11px var(--mono)}
  .opts{display:flex;gap:6px;flex-wrap:wrap;margin:2px 0 4px}
  .opts span{background:var(--panel2);border:1px solid var(--line);border-radius:4px;
             padding:2px 7px;font:11px var(--mono);color:var(--dim)}
  .opts b{color:var(--fg);font-weight:600}
  .kind{color:var(--fg);font:600 13px var(--mono);text-transform:none;
        letter-spacing:0}
  .hint{color:var(--dim);font-size:12px;margin-top:10px}
</style></head><body>

<div class="wrap">
  <div class="panel">
    <div class="row">
      <label>label <input id="label" placeholder="e.g. pfifo 1000 @ 50 Mbit"></label>
      <label>target <input id="host" size="14"></label>
      <label>scenario <select id="scenario"></select></label>
      <label>seconds <input id="duration" type="number" min="2" max="600" style="width:80px"></label>
      <label>settle <input id="settle" type="number" min="0" max="60" step="0.5" style="width:80px"></label>
      <label>pings <input id="ping_count" type="number" min="1" max="2000" style="width:80px"></label>
      <label>interval <input id="ping_interval" type="number" min="0.2" step="0.1" style="width:80px"></label>
      <label>idle pings <input id="idle_count" type="number" min="0" max="200" style="width:80px"></label>
      <label>-P <input id="streams" type="number" min="1" max="64" style="width:64px"></label>
      <label>-Q tos <input id="tos" size="6" placeholder="0xb8" style="width:80px"></label>
      <label id="wrap_udp" style="display:none">flood <input id="udp_rate" size="5" style="width:74px"></label>
      <label id="wrap_hostb" style="display:none">host B <input id="host_b" size="13" style="width:120px"></label>
      <label id="wrap_small" style="display:none">size <input id="small_size" size="5" style="width:74px"></label>
      <span style="display:flex;gap:8px;margin-left:auto">
        <button id="go">Run test</button>
        <button id="stop" class="ghost" disabled>Stop</button>
        <button id="reset" class="ghost" title="blank the charts, the live numbers and the queue trace for a fresh run. Saved runs stay in the table — Clear all deletes those">Clear</button>
      </span>
    </div>
    <div class="err" id="err"></div>
  </div>

  <div class="live-nums" id="nums"></div>

  <div class="grid">
    <div class="panel">
      <div class="chart-head"><h2>Ping <span id="viewing"></span></h2>
        <div class="legend">
          <span><i style="background:var(--idle)"></i>idle</span>
          <span><i style="background:var(--ping)"></i>under load</span>
          <span title="a ping that never came back — one rule per lost packet"><i
            class="bar" style="background:var(--bad)"></i>lost</span>
        </div></div>
      <canvas id="pingChart" height="260"></canvas>
    </div>
    <div class="panel">
      <div class="chart-head"><h2>Throughput</h2>
        <div class="legend" id="speedLegend"></div></div>
      <canvas id="speedChart" height="260"></canvas>
    </div>
  </div>

  <div class="panel">
    <div class="chart-head">
      <h2>Router queue <span id="rtitle"></span></h2>
      <div class="row" style="gap:6px;align-items:center">
        <span id="rerr" class="rerr"></span>
        <input id="r_host" size="13" title="router address">
        <select id="r_iface" title="interface — eth0 for egress, ifb0 for ingress"
                style="min-width:78px"></select>
        <button id="r_apply" class="ghost">watch</button>
      </div>
    </div>
    <div class="row" style="gap:8px;margin:2px 0 8px;align-items:center">
      <label style="flex-direction:row;align-items:center;gap:6px">shaper
        <select id="q_rate">
          <option>10mbit</option><option>20mbit</option>
          <option selected>50mbit</option><option>90mbit</option>
          <option>95mbit</option><option>99mbit</option>
          <option>100mbit</option><option>200mbit</option>
        </select></label>
      <label style="flex-direction:row;align-items:center;gap:6px">qdisc
        <select id="q_kind"></select></label>
      <label id="q_tune_wrap" style="display:none;flex-direction:row;align-items:center;gap:6px"
             title="the rate red's thresholds are sized for. Leave it on the shaper rate for a tuned queue; set it lower and red drops far too early, which is the 1998 failure that got RED switched off everywhere">tuned for
        <select id="q_tune">
          <option value="">= shaper</option>
          <option>2mbit</option><option>10mbit</option><option>20mbit</option>
          <option>50mbit</option><option>100mbit</option><option>200mbit</option>
        </select></label>
      <label style="flex-direction:row;align-items:center;gap:5px;text-transform:none"
             title="shape the download direction: redirect the WAN's arriving traffic through ifb0 and put the qdisc there">
        <input type="checkbox" id="q_ingress"> ingress (ifb0)</label>
      <button id="q_apply" class="ghost">apply to router</button>
      <span id="q_msg" class="rerr"></span>
    </div>
    <div id="ropts" class="opts"></div>
    <div class="live-nums" id="rnums"></div>
    <div class="legend" style="justify-content:flex-end;margin-bottom:-6px">
      <span><i style="background:var(--warn)"></i>backlog bytes</span>
      <span><i style="background:var(--down)"></i>backlog packets</span>
    </div>
    <canvas id="backlogChart" height="200"></canvas>
  </div>

  <div class="panel">
    <div class="chart-head"><h2>Runs compared <span id="cmpTitle"></span></h2>
      <div class="row" style="gap:6px;align-items:center">
        <span class="legend"><span id="cmpLegend"></span></span>
        <button id="cmp_ping" class="ghost small">ping</button>
        <button id="cmp_queue" class="ghost small">queue</button>
      </div></div>
    <canvas id="cmpChart" height="220"></canvas>
  </div>

  <div class="panel">
    <div class="chart-head"><h2>Results</h2>
      <div class="row" style="gap:8px">
        <button class="ghost" onclick="location='/api/export.csv'">CSV</button>
        <button class="ghost" onclick="location='/api/export.json'">JSON</button>
        <button class="danger" id="clear">Clear all</button>
      </div></div>
    <div style="overflow-x:auto"><table id="tbl">
      <thead><tr>
        <th data-k="started">when</th><th data-k="config">router config</th>
        <th data-k="label">label</th><th data-k="dir">dir</th>
        <th data-k="up">up Mbit/s</th><th data-k="down">down Mbit/s</th>
        <th data-k="detail">flows</th><th data-k="retr">retr</th>
        <th data-k="idle">idle ms</th><th data-k="min">min</th><th data-k="avg">avg ms</th>
        <th data-k="max">max</th><th data-k="mdev">mdev</th><th data-k="added">+lat</th>
        <th data-k="qms" title="median time the router's backlog took to drain, over the measurement window — what separates sfq from fq_codel once the ping no longer does">q ms</th>
        <th data-k="qpeak" title="deepest backlog reached during the measurement window, in packets">peak q</th>
        <th data-k="grade">grade</th><th data-k="loss">loss</th><th></th>
      </tr></thead><tbody></tbody>
    </table></div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const F = ["label","host","scenario","duration","settle","ping_count",
           "ping_interval","idle_count","streams","tos","udp_rate","host_b",
           "small_size"];
let cur = null, history = [], selected = new Set(), sortKey = "started", sortAsc = false;

// ---------- charts ----------
function setup(cv){
  const dpr = window.devicePixelRatio || 1;
  // capture the CSS height once: assigning cv.height rewrites the attribute,
  // so re-reading it on every redraw would compound by dpr each time
  if(!cv.dataset.h) cv.dataset.h = cv.getAttribute("height");
  const w = cv.clientWidth, h = +cv.dataset.h;
  cv.width = w * dpr; cv.height = h * dpr;
  cv.style.height = h + "px";
  const c = cv.getContext("2d");
  c.setTransform(dpr,0,0,dpr,0,0); c.clearRect(0,0,w,h);
  return {c,w,h};
}
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

function axes(c,w,h,pad,xr,yr,xlab,ylab,fmtY){
  c.strokeStyle = css("--line"); c.fillStyle = css("--dim");
  c.font = "10px ui-monospace,monospace"; c.lineWidth = 1;
  const X = v => pad.l + (v - xr[0]) / ((xr[1]-xr[0])||1) * (w - pad.l - pad.r);
  const Y = v => h - pad.b - (v - yr[0]) / ((yr[1]-yr[0])||1) * (h - pad.t - pad.b);
  for(let i=0;i<=4;i++){
    const v = yr[0] + (yr[1]-yr[0])*i/4, y = Math.round(Y(v))+0.5;
    c.beginPath(); c.moveTo(pad.l,y); c.lineTo(w-pad.r,y); c.stroke();
    c.textAlign="right"; c.textBaseline="middle";
    c.fillText(fmtY?fmtY(v):v.toFixed(0), pad.l-6, y);
  }
  c.textAlign="center"; c.textBaseline="top";
  for(let i=0;i<=4;i++){
    const v = xr[0] + (xr[1]-xr[0])*i/4;
    c.fillText(v.toFixed(v>=10?0:1)+"s", X(v), h-pad.b+6);
  }
  c.save(); c.translate(11,(h-pad.b+pad.t)/2); c.rotate(-Math.PI/2);
  c.textAlign="center"; c.textBaseline="middle"; c.fillText(ylab,0,0); c.restore();
  return {X,Y};
}
function nice(max){ if(max<=0) return 1;
  const p = Math.pow(10,Math.floor(Math.log10(max))), n = max/p;
  return (n<=1?1:n<=2?2:n<=2.5?2.5:n<=5?5:10)*p; }

function drawPing(){
  const {c,w,h} = setup($("#pingChart"));
  const idle = (cur?.idle_ping)||[], load = (cur?.load_ping)||[];
  const all = idle.concat(load);
  if(!all.length){ empty(c,w,h,"run a test"); return; }
  const rtts = all.filter(s=>s.rtt!=null).map(s=>s.rtt);
  const pad = {l:46,r:12,t:12,b:26};
  const xs = all.map(s=>s.t);
  const xr = [Math.min(0,...xs), Math.max(1,...xs)];
  const yr = [0, nice(Math.max(...rtts,1)*1.12)];
  const {X,Y} = axes(c,w,h,pad,xr,yr,"s","rtt ms");
  // load window shading
  if(load.length){
    c.fillStyle="rgba(74,168,255,.06)";
    c.fillRect(X(0),pad.t,X(xr[1])-X(0),h-pad.t-pad.b);
    c.strokeStyle="rgba(74,168,255,.35)"; c.setLineDash([4,4]);
    c.beginPath(); c.moveTo(X(0),pad.t); c.lineTo(X(0),h-pad.b); c.stroke();
    c.setLineDash([]);
  }
  const line = (pts,col) => {
    const ok = pts.filter(s=>s.rtt!=null);
    if(!ok.length) return;
    c.strokeStyle=col; c.lineWidth=1.6; c.beginPath();
    ok.forEach((s,i)=> i?c.lineTo(X(s.t),Y(s.rtt)):c.moveTo(X(s.t),Y(s.rtt)));
    c.stroke();
    c.fillStyle=col;
    ok.forEach(s=>{c.beginPath();c.arc(X(s.t),Y(s.rtt),1.9,0,7);c.fill();});
    pts.filter(s=>s.rtt==null).forEach(s=>{
      c.strokeStyle=css("--bad"); c.lineWidth=1;
      c.beginPath(); c.moveTo(X(s.t),pad.t); c.lineTo(X(s.t),h-pad.b); c.stroke();
    });
  };
  line(idle, css("--idle")); line(load, css("--ping"));
  // idle average reference
  const iv = idle.filter(s=>s.rtt!=null).map(s=>s.rtt);
  if(iv.length){
    const a = iv.reduce((x,y)=>x+y,0)/iv.length;
    c.strokeStyle=css("--idle"); c.setLineDash([2,4]); c.lineWidth=1;
    c.beginPath(); c.moveTo(pad.l,Y(a)); c.lineTo(w-pad.r,Y(a)); c.stroke();
    c.setLineDash([]);
  }
}

const SERIES_COLORS = ["--up","--down","--ping","--ok","--warn","--bad"];
const SERIES_FILL = ["rgba(74,168,255,.12)","rgba(199,146,234,.12)",
                     "rgba(255,138,91,.12)","rgba(63,185,80,.12)",
                     "rgba(210,153,34,.12)","rgba(248,81,73,.12)"];

function seriesList(){
  const S = cur?.series || {}, labels = cur?.labels || {};
  return Object.keys(S).filter(k => (S[k]||[]).length)
    .map((k,i) => ({key:k, label:labels[k]||k, pts:S[k],
                    col:css(SERIES_COLORS[i % SERIES_COLORS.length]),
                    fill:SERIES_FILL[i % SERIES_FILL.length]}));
}
function drawSpeed(){
  const {c,w,h} = setup($("#speedChart"));
  const series = seriesList();
  // legend follows the scenario: upload/download, or flood vs polite, etc.
  $("#speedLegend").innerHTML = series.map(s =>
    `<span><i style="background:${s.col}"></i>${esc(s.label)}</span>`).join("");
  if(!series.length){ empty(c,w,h,"run a test"); return; }
  const all = series.flatMap(s=>s.pts);
  const pad = {l:52,r:12,t:12,b:26};
  const xr = [0, Math.max(1,...all.map(p=>p.t))];
  const yr = [0, nice(Math.max(1,...all.map(p=>p.mbps))*1.12)];
  const {X,Y} = axes(c,w,h,pad,xr,yr,"s","Mbit/s",v=>v>=1000?(v/1000).toFixed(1)+"G":v.toFixed(0));
  for(const sr of series){
    const pts = sr.pts;
    c.strokeStyle=sr.col; c.lineWidth=1.8; c.beginPath();
    pts.forEach((p,i)=> i?c.lineTo(X(p.t),Y(p.mbps)):c.moveTo(X(p.t),Y(p.mbps)));
    c.stroke();
    if(series.length <= 2){
      c.lineTo(X(pts.at(-1).t),Y(0)); c.lineTo(X(pts[0].t),Y(0)); c.closePath();
      c.fillStyle=sr.fill; c.fill();
    }
  }
}

// The same runs, two instruments. Ping under load ties across every fair
// queue — sfq, fq_codel and cake all read ~20 ms — so a ping-only comparison
// says the subject was solved in 1990. Queue depth is what still separates
// them. Same bars, same rows, one click apart.
let cmpMode = "ping";

// bar = min to max, notch = the middle number, coloured by how bad it is
const CMP_MODES = {
  ping: {
    title: "— ping under load, min/avg/max",
    legend: "bar = min to max, notch = average",
    unit: "ms",
    read: r => {
      const s = r.summary?.load_ping;
      if(s?.avg == null) return null;
      const g = r.summary.grade;
      return {lo: s.min, mid: s.avg, hi: s.max,
              col: (g==="A+"||g==="A")?"--ok":(g==="B"||g==="C")?"--warn":"--bad"};
    },
  },
  queue: {
    title: "— router queue, time to drain",
    legend: "bar = min to peak, notch = median",
    unit: "ms",
    read: r => {
      const q = r.summary?.queue;
      if(!q || q.drain_ms == null) return null;
      const lo = q.drain_min_ms ?? q.drain_ms, hi = q.drain_peak_ms ?? q.drain_ms;
      return {lo, mid: q.drain_ms, hi,
              col: q.drain_ms < 10 ? "--ok" : q.drain_ms < 60 ? "--warn" : "--bad"};
    },
  },
};

function drawCmp(){
  const {c,w,h} = setup($("#cmpChart"));
  const mode = CMP_MODES[cmpMode] || CMP_MODES.ping;
  $("#cmpTitle").textContent = mode.title;
  $("#cmpLegend").textContent = mode.legend;
  $("#cmp_ping").classList.toggle("on", cmpMode==="ping");
  $("#cmp_queue").classList.toggle("on", cmpMode==="queue");

  let rows = history.map(r => ({r, v: mode.read(r)})).filter(x => x.v);
  if(selected.size) rows = rows.filter(x=>selected.has(x.r.id));
  rows = rows.slice(0,12).reverse();
  if(!rows.length){
    // a queue-mode blank is a different problem from having no runs at all
    empty(c,w,h, cmpMode==="queue" && history.length
      ? "no router queue readings on these runs — was the router panel connected?"
      : "finished runs appear here — click rows to compare");
    return;
  }
  const pad = {l:190,r:56,t:10,b:24};
  const maxv = nice(Math.max(...rows.map(x=>x.v.hi))*1.1) || 1;
  const bw = (h-pad.t-pad.b)/rows.length;
  const X = v => pad.l + v/maxv*(w-pad.l-pad.r);
  c.font="10px ui-monospace,monospace"; c.strokeStyle=css("--line");
  for(let i=0;i<=4;i++){
    const v = maxv*i/4, x = Math.round(X(v))+0.5;
    c.beginPath(); c.moveTo(x,pad.t); c.lineTo(x,h-pad.b); c.stroke();
    c.fillStyle=css("--dim"); c.textAlign="center"; c.textBaseline="top";
    c.fillText(v.toFixed(0)+mode.unit, x, h-pad.b+5);
  }
  rows.forEach(({r,v},i)=>{
    const y = pad.t+i*bw+bw/2, col = css(v.col);
    c.fillStyle=col+"44"; c.fillRect(X(v.lo),y-7,Math.max(2,X(v.hi)-X(v.lo)),14);
    c.fillStyle=col; c.fillRect(X(v.mid)-1.5,y-9,3,18);
    c.fillStyle=css("--fg"); c.textAlign="right"; c.textBaseline="middle";
    c.font="11px ui-monospace,monospace";
    c.fillText((r.label || (r.qdisc && r.qdisc.text) || "").slice(0,26), pad.l-10, y);
    c.fillStyle=css("--dim"); c.textAlign="left";
    c.fillText(v.mid.toFixed(v.mid<10?1:0), X(v.hi)+7, y);
  });
}
function empty(c,w,h,msg){
  c.fillStyle=css("--dim"); c.font="12px ui-monospace,monospace";
  c.textAlign="center"; c.textBaseline="middle"; c.fillText(msg,w/2,h/2);
}

// ---------- router queue ----------
let router = {host:"", iface:"", info:{}, samples:[], error:null, ifaces:[], wan:"eth0"};

function fmtBytes(b){
  if(b == null) return "—";
  if(b >= 1048576) return (b/1048576).toFixed(2)+" MB";
  if(b >= 1024) return (b/1024).toFixed(1)+" KB";
  return b+" B";
}
// tc reports bandwidth in bytes/sec and times in microseconds — show them the
// way tc's own text output does, so the chips match what you type
const OPTFMT = {
  bandwidth: v => v*8 >= 1e9 ? (v*8/1e9).toFixed(2)+"Gbit" : (v*8/1e6).toFixed(0)+"Mbit",
  rtt: v => (v/1000)+"ms", target: v => (v/1000)+"ms",
  interval: v => (v/1000)+"ms", ce_threshold: v => (v/1000)+"ms",
  memory_limit: v => fmtBytes(v), limit: v => v+"p",
};
function fmtOpt(k, v){
  return (typeof v === "number" && OPTFMT[k]) ? OPTFMT[k](v) : v;
}

const bpsLabel = bps => bps >= 1e9 ? (bps/1e9).toFixed(2)+"gbit"
                                   : Math.round(bps/1e6)+"mbit";

// the same rule as median() on the server — mean of the two middle values on
// an even count — so the live drain reading and the q ms the run freezes are
// the same statistic, not two that drift apart by a sample. Callers pass an
// array already stripped of nulls.
const medianOf = a => {
  if(!a || !a.length) return null;
  const v = [...a].sort((x, y) => x - y), mid = v.length >> 1;
  return v.length % 2 ? v[mid] : (v[mid-1] + v[mid]) / 2;
};

// on page load only, point the pickers at whatever the router is running, so
// "apply" starts from the current state instead of a stale default
function syncQdiscControls(){
  const i = router.info || {};
  if(!i.kind) return;
  // fq_codel spread over every hardware queue is the kernel default, not a
  // leaf someone configured
  let kind = (i.kind === "fq_codel" && i.count > 1) ? "clear" : i.kind;
  if(kind === "cake"){
    const o = i.options || {};
    if(o.nat && String(o.flowmode||"").includes("dual-srchost")) kind = "cake-host";
  }
  const kSel = $("#q_kind");
  if([...kSel.options].some(o => o.value === kind)) kSel.value = kind;
  syncTune();
  $("#q_ingress").checked = (router.iface === "ifb0");
  if(i.shaper){
    const label = bpsLabel(i.shaper.bps), rSel = $("#q_rate");
    if(![...rSel.options].some(o => o.value === label)){
      rSel.add(new Option(label, label));
    }
    rSel.value = label;
  }
}

function fillIfaces(){
  const sel = $("#r_iface");
  if(router.iface && router.iface !== "ifb0") router.wan = router.iface;
  // ifb0 only exists once the ingress shaper is up, so offer it either way
  const names = [...new Set([...(router.ifaces||[]), "eth0", "ifb0",
                             router.iface].filter(Boolean))];
  const keep = sel.value || router.iface;
  sel.innerHTML = names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  sel.value = names.includes(keep) ? keep : router.iface;
}

function renderRouterInfo(){
  const i = router.info || {};
  $("#rtitle").innerHTML = router.host
    ? `<span class="kind">${esc(router.host)} ${esc(router.iface)} — ${esc(i.kind||"?")}`
      + `${i.count>1?" ×"+i.count:""}</span>` : "";
  $("#rerr").textContent = router.error || "";
  // qdisc parameters, straight from tc: this is what identifies the config
  const o = i.options || {}, chips = [];
  if(i.shaper) chips.push(
    `<span style="border-color:var(--accent)">${esc(i.shaper.kind)} `
    + `<b>${bpsLabel(i.shaper.bps)}</b></span>`);
  for(const [k,v] of Object.entries(o)){
    if(v === false || v == null || typeof v === "object") continue;
    if(k === "bandwidth" && i.shaper) continue;   // already in the shaper chip
    chips.push(v === true ? `<span><b>${esc(k)}</b></span>`
                          : `<span>${esc(k)} <b>${esc(fmtOpt(k,v))}</b></span>`);
    if(chips.length >= 12) break;
  }
  $("#ropts").innerHTML = chips.join("");
}
function renderRouterNums(){
  const last = router.samples.at(-1);
  const cell = (k,v) => `<div><span>${k}</span><b>${v}</b></div>`;
  if(!last){ $("#rnums").innerHTML = ""; return; }
  const peak = Math.max(0, ...router.samples.map(x=>x.qlen||0));
  // the live drain reading is only ever the instant the last sample landed,
  // and the backlog breathes — TCP fills the queue, takes a drop, backs off.
  // The median is the figure a run freezes and the table's q ms column shows,
  // so the panel can be read against the ladder; the peak is the moment the
  // video is about, a queue that sits at 3 ms and spikes to 90.
  const drains = router.samples.map(x=>x.drain_ms).filter(v=>v!=null);
  const medDrain = medianOf(drains), peakDrain = drains.length ? Math.max(...drains) : null;
  const ms = v => v==null ? "—" : v.toFixed(0)+" ms";
  $("#rnums").innerHTML =
    cell("backlog", (last.qlen||0) + " pkt")
    + cell("&nbsp;", fmtBytes(last.backlog))
    + cell("drain time", ms(last.drain_ms))
    + cell("median drain", ms(medDrain))
    + cell("peak drain", ms(peakDrain))
    + cell("peak backlog", peak + " pkt")
    + cell("drops/s", last.dps==null?"—":last.dps.toFixed(1))
    + cell("dropped", (router.info.drops??0).toLocaleString())
    + cell("rate", last.mbps==null?"—":last.mbps.toFixed(1)+" M");
}
function drawBacklog(){
  const {c,w,h} = setup($("#backlogChart"));
  const S = router.samples;
  if(!S.length){ empty(c,w,h, router.error || "waiting for the router…"); return; }
  const pad = {l:58,r:52,t:12,b:24};
  const now = S.at(-1).t;
  const span = Math.max(20, now - S[0].t);
  const maxB = nice(Math.max(1, ...S.map(x=>x.backlog||0)));
  const maxP = nice(Math.max(1, ...S.map(x=>x.qlen||0)));
  const X = t => pad.l + (1 - (now - t)/span) * (w - pad.l - pad.r);
  const YB = v => h - pad.b - (v/maxB) * (h - pad.t - pad.b);
  const YP = v => h - pad.b - (v/maxP) * (h - pad.t - pad.b);

  c.font = "10px ui-monospace,monospace"; c.lineWidth = 1;
  for(let i=0;i<=4;i++){
    const y = Math.round(h - pad.b - i/4*(h-pad.t-pad.b)) + 0.5;
    c.strokeStyle = css("--line");
    c.beginPath(); c.moveTo(pad.l,y); c.lineTo(w-pad.r,y); c.stroke();
    c.fillStyle = css("--warn"); c.textAlign="right"; c.textBaseline="middle";
    c.fillText(fmtBytes(maxB*i/4).replace(" ",""), pad.l-6, y);
    c.fillStyle = css("--down"); c.textAlign="left";
    c.fillText((maxP*i/4).toFixed(0)+"p", w-pad.r+6, y);
  }
  c.fillStyle = css("--dim"); c.textAlign="center"; c.textBaseline="top";
  for(let i=0;i<=4;i++){
    const secs = span*(1-i/4);
    c.fillText(secs<1?"now":"-"+secs.toFixed(0)+"s", X(now-secs), h-pad.b+5);
  }
  // backlog bytes as an area
  c.beginPath(); c.moveTo(X(S[0].t), YB(0));
  S.forEach(x => c.lineTo(X(x.t), YB(x.backlog||0)));
  c.lineTo(X(S.at(-1).t), YB(0)); c.closePath();
  c.fillStyle = "rgba(210,153,34,.16)"; c.fill();
  c.beginPath();
  S.forEach((x,i)=> i?c.lineTo(X(x.t),YB(x.backlog||0)):c.moveTo(X(x.t),YB(x.backlog||0)));
  c.strokeStyle = css("--warn"); c.lineWidth = 1.6; c.stroke();
  // backlog packets on the right axis
  c.beginPath();
  S.forEach((x,i)=> i?c.lineTo(X(x.t),YP(x.qlen||0)):c.moveTo(X(x.t),YP(x.qlen||0)));
  c.strokeStyle = css("--down"); c.lineWidth = 1.4; c.stroke();
}

function draw(){
  const v = $("#viewing");
  const name = cur && (cur.label || (cur.qdisc && cur.qdisc.text));
  v.textContent = name ? "— " + name : "";
  v.style.color = "var(--dim)"; v.style.textTransform = "none";
  drawPing(); drawSpeed(); drawCmp(); drawBacklog();
}

// ---------- live numbers ----------
// Percentage of the load burst that never came back — the red rules on the
// ping chart. ping backfills its losses only when the burst ends, so while a
// run is live this counts gaps in the sequence numbers seen so far; once the
// run is filed, ping's own summary line is authoritative.
function pingLoss(c){
  if(!c) return null;
  const done = c.summary && c.summary.loss_pct;
  if(done != null) return done;
  const load = c.load_ping || [];
  if(!load.length) return null;
  const maxSeq = Math.max(...load.map(s=>s.seq||0));
  if(maxSeq < 1) return null;
  return 100 * (maxSeq - load.filter(s=>s.rtt!=null).length) / maxSeq;
}

function nums(){
  const el = $("#nums");
  if(!cur){ el.innerHTML=""; return; }
  const load = (cur.load_ping||[]).filter(s=>s.rtt!=null).map(s=>s.rtt);
  const idle = (cur.idle_ping||[]).filter(s=>s.rtt!=null).map(s=>s.rtt);
  const avg = a => a.length ? a.reduce((x,y)=>x+y,0)/a.length : null;
  const cell = (k,v,u="") => v==null?"":
    `<div><span>${k}</span><b>${typeof v==="number"?v.toFixed(v<10?2:1):v}${u}</b></div>`;
  const a = avg(load), i = avg(idle);
  el.innerHTML = seriesList().map(s => flowCell(s)).join("")
    + cell("idle ping", i, " ms") + cell("ping now", load.at(-1), " ms")
    + cell("avg under load", a, " ms")
    + cell("max", load.length?Math.max(...load):null, " ms")
    + (a!=null&&i!=null ? cell("added", a-i, " ms") : "")
    + lossCell(pingLoss(cur))
    + sparseCells();
}
// The sparse scenario's entire result is a pair of transfer times, and they
// only ever appeared in the results table's detail column — which means they
// were not on screen at the moment they were worth talking about. These two
// cells fill in as the run goes: the idle one during the sparse-idle phase,
// the loaded one while the bulk upload is holding the queue open. Every other
// scenario renders nothing here.
function sparseCells(){
  if(!cur || (cur.cfg || {}).scenario !== "sparse") return "";
  const sp = cur.sparse || {};
  // the same middle-value rule _summarize() uses for sparse, so the panel and
  // the table cannot disagree: upper of the two on an even count, not their
  // mean. This is deliberately not medianOf() — the server has two rules, and
  // this cell has to track the one its own number is computed with.
  const med = a => (a && a.length)
    ? [...a].sort((x, y) => x - y)[Math.floor(a.length / 2)] : null;
  const idle = med(sp.idle_ms), loaded = med(sp.loaded_ms);
  if(idle == null && loaded == null) return "";
  const both = idle != null && loaded != null;
  const sign = v => `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(0)}`;
  // fq_codel and cake hand sparse flows priority, so "loaded" can come back
  // *faster* than idle on the upper rungs — the wording has to survive that
  const factor = !(both && idle && loaded) ? ""
    : loaded >= idle ? `${(loaded / idle).toFixed(1)}× slower`
                     : `${(idle / loaded).toFixed(1)}× faster`;
  const n = (k, v, tip) => v == null ? "" :
    `<div title="${esc(tip)}"><span>${k}</span><b>${v.toFixed(0)} ms</b></div>`;
  const size = esc((cur.cfg || {}).small_size || "256K");
  // the count matters while the run is in flight: a "loaded" median standing on
  // one transfer is not the same claim as one standing on three
  const of = a => { const c = (a || []).length;
                    return `median of ${c} transfer${c === 1 ? "" : "s"}`; };
  // the delta gets its own cell because it is the result: the two medians are
  // the working, this is the answer. Coloured like ping loss — a transfer that
  // got slower under load is the bad news the scenario went looking for.
  const addedCell = !both ? "" :
    `<div title="${esc(`${size} took ${Math.abs(loaded - idle).toFixed(0)} ms `
                       + `${loaded >= idle ? "longer" : "less"} under load`
                       + (factor ? ` — ${factor}` : ""))}">`
    + `<span>${size} added</span>`
    + `<b style="color:${css(loaded > idle ? "--bad" : "--ok")}">`
    + `${sign(loaded - idle)} ms</b></div>`;
  return n(size + " idle", idle,
           `${of(sp.idle_ms)} timed before the load started`)
       + n(size + " loaded", loaded,
           `${of(sp.loaded_ms)} timed under load`
           + (factor ? ` — ${factor}` : ""))
       + addedCell;
}
// its own cell rather than cell(): 0.0% must show (it is the good news on
// every fair queue), and anything above zero is coloured to match the chart
function lossCell(pct){
  if(pct == null) return "";
  const col = pct > 0 ? css("--bad") : css("--ok");
  return `<div><span>ping loss</span>`
       + `<b style="color:${col}">${pct.toFixed(1)}%</b></div>`;
}

// One traffic flow's throughput, with its drop rate beside it where there is
// one. iperf3 only learns UDP loss from the server's closing report, so this
// appears when the run ends, not while it streams — a flood reading 48 Mbit/s
// mid-run is 48 Mbit/s *sent*, and a fifth of it never arrives.
function flowFinal(key){
  return ((cur && cur.summary && cur.summary.flows) || []).find(x => x.key === key) || null;
}
function flowLoss(key){
  const f = flowFinal(key);
  return f && f.loss_pct != null ? f.loss_pct : null;
}
// A starved flow reads 0.00 in Mbit/s and looks like a failed connection when
// it is really the point of the test: under pfifo the polite sender gets a few
// hundred kilobits, and most one-second intervals carry nothing at all. Below
// 1 Mbit/s the number is worth more in kbit — "160 K" says what happened,
// "0.16 M" does not, and "0.00 M" says the opposite.
function flowRate(mb){
  if(mb >= 1) return `${mb.toFixed(mb < 10 ? 2 : 1)} M`;
  const k = mb * 1000;
  return `${k.toFixed(k < 10 ? 1 : 0)} K`;
}
// Mean rate since the load started, weighted by each interval's length.
// The last one-second interval is a coin flip for a starved flow: in the pfifo
// hostile run the polite TCP moved data in 2 of its 16 seconds and read exactly
// zero in the other 14, so a cell showing the latest interval spent the run
// saying "0.0 K" about a flow that averaged 167 K. The mean is what the results
// table reports and what there is to read out loud; the speed chart directly
// below still carries the instant-by-instant shape.
function flowMean(pts){
  let bits = 0, secs = 0, prev = 0;
  for(const p of pts){
    const dt = Math.max(0, p.t - prev);
    prev = p.t;
    bits += (p.mbps || 0) * dt;
    secs += dt;
  }
  return secs > 0 ? bits / secs : null;
}
function flowCell(s){
  // iperf3's intervals count bytes *sent*; only the server's closing report
  // knows how many arrived, and on this rung the gap is the whole story —
  // the polite flow sent 328 K and landed 167 K. So the cell runs on the mean
  // sent while the run is live, then switches to the received figure the
  // moment run_end brings it in, which is the number the table shows too.
  const fin = flowFinal(s.key);
  const done = fin && fin.receiver_mbps != null;
  const mb = done ? fin.receiver_mbps : flowMean(s.pts);
  if(mb == null) return "";
  const now = s.pts.at(-1)?.mbps;
  const lp = flowLoss(s.key);
  const lost = lp == null ? "" :
    ` <em style="font-style:normal;font-size:13px;color:${lp > 0 ? css("--bad") : css("--ok")}"`
    + ` title="share of this flow's packets that never reached the server">`
    + `${lp.toFixed(1)}% lost</em>`;
  const tip = done
    ? `received over the run, from the server's closing report`
      + (fin.sender_mbps != null ? ` — ${flowRate(fin.sender_mbps)} was sent` : "")
    : `mean sent since the load started`
      + (now == null ? "" : ` — latest interval ${flowRate(now)}`);
  return `<div title="${esc(tip)}"><span>${esc(s.label)}</span>`
       + `<b>${flowRate(mb)}${lost}</b></div>`;
}

// ---------- table ----------
function cellsOf(r){
  const s = r.summary||{}, lp = s.load_ping||{}, ip = s.idle_ping||{};
  return {
    started: r.started, label: r.label, dir: r.cfg.scenario,
    config: (r.qdisc && r.qdisc.text) || "",
    detail: flowDetail(r),
    up: s.up_mbps, down: s.down_mbps,
    retr: s.up_retransmits ?? s.down_retransmits,
    idle: ip.avg, min: lp.min, avg: lp.avg, max: lp.max, mdev: lp.mdev,
    added: s.added_ms, grade: s.grade, loss: s.loss_pct,
    qms: (s.queue||{}).drain_ms, qpeak: (s.queue||{}).qlen_peak,
  };
}
// what the router's queue held while this run was measuring, for the cell title
function queueTitle(r){
  const q = (r.summary||{}).queue || {};
  if(!q.samples) return "no router samples for this run";
  return `median ${q.qlen} pkt / ${fmtBytes(q.backlog)}`
       + `, peak ${q.qlen_peak} pkt`
       + (q.drain_peak_ms!=null ? ` / ${q.drain_peak_ms.toFixed(0)} ms` : "")
       + `  (${q.samples} samples)`;
}
// one compact string carrying whatever the scenario was run to find out
function flowDetail(r){
  const s = r.summary || {};
  if(s.sparse){
    const p = s.sparse;
    if(p.idle_med == null || p.loaded_med == null) return "";
    // iperf3 costs a fixed ~1.1 s of setup on a 20 ms path, so the delta says
    // more than the ratio does
    return `256K ${p.idle_med.toFixed(0)} → ${p.loaded_med.toFixed(0)} ms`
         + (p.added_ms != null
              ? `  (${p.added_ms >= 0 ? "+" : "−"}${Math.abs(p.added_ms).toFixed(0)})`
              : "");
  }
  if(s.ecn){
    const e = s.ecn, n = v => v == null ? "?" : v;
    return `off: ${n(e.off_retransmits)} retr / ${n(e.off_marks)} marks`
         + `  →  on: ${n(e.on_retransmits)} retr / ${n(e.on_marks)} marks`;
  }
  const flows = s.flows || [];
  if(flows.length < 2) return "";
  // a flood's drop rate belongs next to its throughput: 48 Mbit/s sent with a
  // fifth discarded is the point of the hostile-neighbour rung, not a footnote
  // flowRate() so a starved flow reads "163 K" rather than "0.2" — the unit
  // suffix is no longer decoration once the two flows can be in different ones
  const parts = flows.map(f => `${f.label} ${flowRate(f.receiver_mbps ?? 0)}`
    + (f.loss_pct != null ? ` (${f.loss_pct.toFixed(0)}% lost)` : ""));
  const ratio = (s.fairness && s.fairness.ratio != null)
    ? `  ${s.fairness.ratio}:1`
    : (s.fairness ? "  (one side got nothing)" : "");
  return parts.join(" / ") + ratio;
}

function renderTable(){
  const tb = $("#tbl tbody"); tb.innerHTML = "";
  const rows = history.map(r=>({r, v:cellsOf(r)}));
  rows.sort((a,b)=>{
    const x=a.v[sortKey], y=b.v[sortKey];
    if(x==null) return 1; if(y==null) return -1;
    return (x>y?1:x<y?-1:0) * (sortAsc?1:-1);
  });
  const n = (v,d=1) => v==null?"—":(typeof v==="number"?v.toFixed(d):v);
  for(const {r,v} of rows){
    const tr = document.createElement("tr");
    if(selected.has(r.id)) tr.className = "sel";
    tr.innerHTML =
      `<td>${new Date(r.started*1000).toLocaleTimeString()}</td>`+
      `<td class="lab cfg" title="${esc(v.config)}">${esc(v.config) || "—"}`+
        `${r.qdisc_changed ? ' <span title="the router config changed while this'
          + ' run was in flight" style="color:var(--warn)">⚠</span>' : ""}</td>`+
      `<td class="lab" title="${esc(r.label)}">${esc(r.label) || "—"}`+
        `${r.cancelled?" ⚠":""}</td>`+
      `<td>${v.dir}</td><td>${n(v.up,1)}</td><td>${n(v.down,1)}</td>`+
      `<td class="lab" title="${esc(v.detail)}">${esc(v.detail)||"—"}</td>`+
      `<td>${v.retr??"—"}</td><td>${n(v.idle,2)}</td><td>${n(v.min,1)}</td>`+
      `<td><b>${n(v.avg,1)}</b></td><td>${n(v.max,1)}</td><td>${n(v.mdev,2)}</td>`+
      `<td>${v.added==null?"—":(v.added>0?"+":"")+n(v.added,1)}</td>`+
      `<td title="${esc(queueTitle(r))}"><b>${n(v.qms,0)}</b></td>`+
      `<td title="${esc(queueTitle(r))}">${v.qpeak??"—"}</td>`+
      `<td><span class="g ${v.grade}">${v.grade||"—"}</span></td>`+
      `<td>${v.loss==null?"—":n(v.loss,1)+"%"}</td>`+
      `<td><span class="x" data-del="${r.id}">✕</span></td>`;
    tr.onclick = e => {
      if(e.target.dataset.del) return;
      cur = r;                                   // show this run in the charts
      selected.has(r.id) ? selected.delete(r.id) : selected.add(r.id);
      renderTable(); draw(); nums();
    };
    tb.appendChild(tr);
  }
  tb.querySelectorAll("[data-del]").forEach(el => el.onclick = async e => {
    e.stopPropagation();
    await post("/api/delete", {id: el.dataset.del});
    history = history.filter(r=>r.id!==el.dataset.del);
    selected.delete(el.dataset.del); renderTable(); drawCmp();
  });
}
const ESCAPES = {"<":"&lt;", ">":"&gt;", "&":"&amp;", '"':"&quot;", "'":"&#39;"};
const esc = s => String(s ?? "").replace(/[<>&"']/g, m => ESCAPES[m]);

// ---------- wiring ----------
// always resolves to an object: a rejected fetch would otherwise abort the
// caller mid-way and strand the UI in its "applying…" / disabled state
async function post(url, body){
  try {
    const r = await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},
                              body:JSON.stringify(body||{})});
    const text = await r.text();
    try { return JSON.parse(text); }
    catch { return {error: `${r.status}: ${text.slice(0,200) || "no response"}`}; }
  } catch(e) {
    return {error: "request failed: " + e.message};
  }
}
function setBusy(b){
  $("#go").disabled = b; $("#stop").disabled = !b; $("#reset").disabled = b;
}
function syncScenarioFields(){
  const v = $("#scenario").value;
  $("#wrap_udp").style.display   = v === "hostile"  ? "flex" : "none";
  $("#wrap_hostb").style.display = v === "hostfair" ? "flex" : "none";
  $("#wrap_small").style.display = v === "sparse"   ? "flex" : "none";
  // the 8-vs-1 test needs -P to say 8: at -P 1 it would run 2 flows against
  // 1, and that gap is small enough to hide under run-to-run noise on every
  // qdisc, which reads as "the picker does nothing"
  if(v === "hostfair" && +$("#streams").value < 2) $("#streams").value = 8;
  // sparse needs room for idle transfers, three loaded transfers and the
  // ping inside one bulk run
  if(v === "sparse" && +$("#duration").value < 30) $("#duration").value = 30;
}
$("#scenario").onchange = syncScenarioFields;

$("#go").onclick = async () => {
  const cfg = {}; F.forEach(k => cfg[k] = $("#"+k).value);
  localStorage.setItem("cakemeter", JSON.stringify(cfg));
  $("#err").textContent = "";
  cur = {idle_ping:[], load_ping:[], series:{}, labels:{}, sparse:{}};
  draw(); nums(); setBusy(true);
  const res = await post("/api/run", cfg);
  if(res.error){ $("#err").textContent = res.error; setBusy(false); }
};
$("#stop").onclick = () => post("/api/stop");
// Between takes the screen still carries the last run: its ping trace, its
// throughput lines, the rows whose selection drives the comparison, a stale
// error. This is the "as if the page had just loaded, before anything ran"
// state — nothing is deleted, so a cleared screen costs no data.
$("#reset").onclick = () => {
  cur = null;
  selected.clear();
  router.samples = [];              // the monitor refills this within a second
  $("#err").textContent = "";
  $("#q_msg").textContent = "";
  renderTable(); renderRouterNums(); draw(); nums();
};
// the tuned-for box is red's alone — every other qdisc here either has no
// knobs or carries its own rate
function syncTune(){
  $("#q_tune_wrap").style.display = $("#q_kind").value === "red" ? "flex" : "none";
}

$("#q_apply").onclick = async () => {
  const kind = $("#q_kind").value, rate = $("#q_rate").value;
  const ingress = $("#q_ingress").checked;
  const tune = kind === "red" ? $("#q_tune").value : "";
  const btn = $("#q_apply"); btn.disabled = true;
  $("#q_msg").textContent = "applying…";
  // the redirect has to be taken from a real interface, so remember the last
  // non-ifb0 one the dropdown was pointed at
  const r = await post("/api/qdisc", {kind, rate, ingress, tune, wan: router.wan});
  btn.disabled = false;
  if(!r.error){ router.samples = []; renderRouterNums(); drawBacklog(); }
  // the monitor notices the new qdisc on its own within half a second
  $("#q_msg").textContent = r.error ? r.error
    : (kind === "clear" ? (ingress ? "ingress removed" : "cleared")
       : `applied ${kind} ${rate}${tune ? ` tuned for ${tune}` : ""}`
         + `${ingress ? " on ifb0 (ingress)" : ""}`);
  if(!r.error && ingress){
    // the server moved the monitor to whichever device now holds the queue;
    // point the dropdown at it so the panel and the picker agree
    router.iface = (kind === "clear") ? router.wan : "ifb0";
    fillIfaces();
    $("#r_iface").value = router.iface;
  }
  setTimeout(() => { $("#q_msg").textContent = ""; }, 6000);
};
$("#r_apply").onclick = async () => {
  const r = await post("/api/router",
                       {host: $("#r_host").value, iface: $("#r_iface").value});
  if(r.error){ router.error = r.error; }
  else { router.samples = []; router.host = r.host; router.iface = r.iface;
         router.info = r.info; router.error = r.error; }
  renderRouterInfo(); renderRouterNums(); drawBacklog();
};
$("#cmp_ping").onclick  = () => { cmpMode = "ping";  drawCmp(); };
$("#cmp_queue").onclick = () => { cmpMode = "queue"; drawCmp(); };
$("#clear").onclick = async () => {
  if(!confirm("Delete every saved run?")) return;
  await post("/api/clear"); history = []; selected.clear(); renderTable(); drawCmp();
};
document.querySelectorAll("#tbl th[data-k]").forEach(th => th.onclick = () => {
  const k = th.dataset.k;
  sortAsc = (sortKey === k) ? !sortAsc : false;
  sortKey = k; renderTable();
});

const ev = new EventSource("/api/stream");
ev.onmessage = m => {
  const e = JSON.parse(m.data);
  if(e.type === "run_start"){
    cur = {id:e.run_id, cfg:e.cfg, idle_ping:[], load_ping:[], series:{},
           labels:e.labels||{}, sparse:{}};
    setBusy(true);
  } else if(e.type === "phase"){
    setBusy(true);
  } else if(e.type === "ping" && cur){
    (e.kind === "idle" ? cur.idle_ping : cur.load_ping)
      .push({t:e.t, rtt:e.rtt, lost:e.lost, seq:e.seq});
    nums(); drawPing();
  } else if(e.type === "iperf" && cur){
    (cur.series[e.key] ||= []).push({t:e.t, mbps:e.mbps});
    nums(); drawSpeed();
  } else if(e.type === "labels" && cur){
    cur.labels = e.labels; drawSpeed(); nums();
  } else if(e.type === "sparse" && cur){
    (cur.sparse ||= {})[e.kind+"_ms"] = [...((cur.sparse||{})[e.kind+"_ms"]||[]), e.ms];
    nums();
  } else if(e.type === "router"){
    if(e.ifaces){ router.ifaces = e.ifaces; fillIfaces(); return; }
    if(e.reset){
      router.samples = [];
      renderRouterNums(); drawBacklog();
      return;
    }
    if(e.error){ router.error = e.error; }
    else if(e.sample){          // any other router event carries no sample
      router.error = null;
      router.info = e.info; router.host = e.host; router.iface = e.iface;
      router.samples.push(e.sample);
      if(router.samples.length > 240) router.samples.shift();
    }
    renderRouterInfo(); renderRouterNums(); drawBacklog();
  } else if(e.type === "error"){
    $("#err").textContent += e.message + "\n";
  } else if(e.type === "run_end"){
    history.unshift(e.run);
    cur = e.run;
    setBusy(false);
    renderTable(); draw(); nums();
  }
};

(async () => {
  let st;
  try {
    st = await (await fetch("/api/state")).json();
  } catch(e) {
    // without state the page has nothing to draw; say so instead of sitting blank
    $("#err").textContent = "cannot reach cakemeter: " + e.message;
    return;
  }
  history = st.history;
  const options = o => Object.entries(o || {})
    .map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
  $("#scenario").innerHTML = options(st.scenarios);
  $("#q_kind").innerHTML = options(st.qdiscs);
  $("#q_kind").onchange = syncTune;
  syncTune();
  const saved = JSON.parse(localStorage.getItem("cakemeter") || "null");
  F.forEach(k => $("#"+k).value = (saved && saved[k] != null) ? saved[k] : st.defaults[k]);
  if(st.router){
    Object.assign(router, {host:st.router.host, iface:st.router.iface,
                           info:st.router.info, samples:st.router.samples,
                           error:st.router.error, ifaces:st.router.ifaces || []});
  }
  $("#r_host").value = router.host;
  fillIfaces();
  renderRouterInfo(); renderRouterNums(); syncQdiscControls(); syncScenarioFields();
  if(st.current){ cur = st.current; setBusy(true); }
  else if(history.length){ cur = history[0]; }
  renderTable(); draw(); nums();
})();
addEventListener("resize", draw);
</script></body></html>
"""


PAGE_BYTES = PAGE.encode()


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="live bufferbloat bench for the CAKE lab")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8420, help="bind port (default 8420)")
    ap.add_argument("--target", default=DEFAULTS["host"], help="iperf3 server address")
    ap.add_argument("--router", default=ROUTER["host"],
                    help="router to read qdisc stats from over ssh")
    ap.add_argument("--router-iface", default=ROUTER["iface"],
                    help="interface on the router (eth0 egress, ifb0 ingress)")
    args = ap.parse_args()

    for name, value in (("--target", args.target), ("--router", args.router)):
        if not HOST_OK.match(value):
            ap.error(f"{name}: {value!r} is not a usable address")
    if not IFACE_OK.match(args.router_iface):
        ap.error(f"--router-iface: {args.router_iface!r} is not a usable name")

    DEFAULTS["host"] = args.target
    DEFAULTS["src_a"] = local_source_ip(args.target)
    # the kernel may prefer the host-B alias as the source for the target, in
    # which case host A and host B would be the same machine talking to itself
    if DEFAULTS["src_a"] == DEFAULTS["host_b"]:
        other = next((a for a in interface_addrs(args.target)
                      if a != DEFAULTS["host_b"]), "")
        print(f"warning: the route to {args.target} prefers "
              f"{DEFAULTS['src_a']}, which is host B in the fairness test"
              + (f" — using {other} as host A" if other
                 else " — set host A by hand before running it"))
        DEFAULTS["src_a"] = other
    ROUTER["host"], ROUTER["iface"] = args.router, args.router_iface
    ROUTER_MONITOR.cfg.update({"host": args.router, "iface": args.router_iface})
    ROUTER_MONITOR.start()
    for tool, why in (("iperf3", "runs"), ("ping", "runs"),
                      ("ssh", "the router panel")):
        if not shutil.which(tool):
            print(f"warning: {tool} not found on PATH — {why} will fail")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    shown = "localhost" if args.host in ("0.0.0.0", "") else args.host
    print(f"cakemeter → http://{shown}:{args.port}   (target {DEFAULTS['host']}, "
          f"router {ROUTER['host']} {ROUTER['iface']})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
