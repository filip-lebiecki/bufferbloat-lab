#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
caketune — CAKE parameter tuner and live qdisc telemetry, served from the router.

Runs *on* the router (.31) and serves a web UI on :8421. Two jobs:

  Tuning.   Every knob CAKE has — bandwidth, the ack-filter trio
            (no-ack-filter / ack-filter / ack-filter-aggressive), rtt,
            overhead/mpu/link-layer, nat, the flow-isolation modes
            (flows / dual-srchost / dual-dsthost / triple-isolate / ...),
            diffserv tin sets, wash, split-gso, memlimit, ingress —
            as form controls that build a real `tc` command. The exact
            command is shown before it runs, and applying it is one click.

  Watching. Everything `tc -s qdisc show dev <x>` prints, live at 2 Hz:
            backlog, drops, overlimits, requeues, memory, capacity estimate,
            and the whole per-tin table — thresh, target, interval,
            pk_delay / av_delay / sp_delay, way_inds/miss/cols, drops, marks,
            ack_drop, sparse/bulk/unresponsive flows, max_len, quantum —
            plus derived rates and drain time, charted over the last minutes.

Both directions are covered: egress on the WAN interface (upload) and ingress
on ifb0 (download), which this tool can build for you — ifb0 plus the
`matchall ... mirred egress redirect` on the WAN, torn down as cleanly as it
went up.

Capture windows let you bracket an external test (libreqos / flent / a
browser bufferbloat run): hit Start, run the test, hit Stop, and the row
records the config that was in force and what the qdisc did during it — so
ack-filter on vs off is two rows of one table.

    uv run caketune.py                       # 0.0.0.0:8421, WAN = eth0
    uv run caketune.py --wan eth0 --port 8421 --host 127.0.0.1

Needs passwordless sudo for `tc`/`ip` (reading is unprivileged; only applying
is not). The page reconfigures the router and is unauthenticated — bind it to
127.0.0.1 and use an ssh tunnel if the network is not yours alone.

Stdlib only, so it starts offline with no package downloads.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

POLL_HZ = 2.0
HISTORY = 1200                  # samples per device: 10 minutes at 2 Hz
MAX_CAPTURES = 100

TC = shutil.which("tc") or "/usr/sbin/tc"
IP = shutil.which("ip") or "/usr/sbin/ip"

# CAKE's documented rtt keywords. This iproute2 (6.19) does NOT accept them as
# words — `rtt lan` is rejected — so the UI ships the numbers instead and emits
# `rtt <n>us`, which is what the keywords mean anyway.
RTT_PRESETS = [
    ("datacentre", 100),
    ("lan", 1_000),
    ("metro", 10_000),
    ("regional", 30_000),
    ("internet", 100_000),
    ("oceanic", 300_000),
    ("satellite", 1_000_000),
]

# Link-layer keywords, each probed against this tc: the ones it rejects
# (pppoe-llc, ipoa-llc, bridged-llc) are not offered. The numbers are what tc
# itself reported back after setting the keyword, not guesses.
OVERHEAD_KEYWORDS = [
    ("raw", "overhead 0, no framing accounted"),
    ("via-ethernet", "overhead 0 (raw), legacy alias"),
    ("ethernet", "overhead 38, mpu 84, noatm"),
    ("docsis", "overhead 18, mpu 64, noatm — cable"),
    ("conservative", "overhead 48, atm — safe for anything"),
    ("pppoe-ptm", "overhead 30, ptm — VDSL2 PPPoE"),
    ("bridged-ptm", "overhead 22, ptm — VDSL2 bridged"),
    ("pppoe-vcmux", "overhead 32, atm — ADSL PPPoE VC-mux"),
    ("pppoa-vcmux", "overhead 10, atm — ADSL PPPoA VC-mux"),
    ("pppoa-llc", "overhead 14, atm — ADSL PPPoA LLC"),
    ("ipoa-vcmux", "overhead 8, atm — ADSL IPoA VC-mux"),
    ("bridged-vcmux", "overhead 24, atm — ADSL bridged VC-mux"),
]

FLOWMODES = [
    ("flowblind", "no flow isolation at all — one FIFO per tin"),
    ("srchost", "one queue per source host"),
    ("dsthost", "one queue per destination host"),
    ("hosts", "one queue per src/dst host pair"),
    ("flows", "one queue per 5-tuple flow"),
    ("dual-srchost", "flows, then fair between source hosts — for egress"),
    ("dual-dsthost", "flows, then fair between dest hosts — for ingress"),
    ("triple-isolate", "flows, fair both ways (default)"),
]

DIFFSERV = [
    ("besteffort", "1 tin — ignore DSCP entirely"),
    ("diffserv3", "3 tins — Bulk / Best Effort / Voice"),
    ("diffserv4", "4 tins — Bulk / Best Effort / Video / Voice"),
    ("diffserv8", "8 tins — full precedence ladder"),
    ("precedence", "8 tins by legacy IP precedence (not recommended)"),
]

ACK_FILTER = [
    ("no-ack-filter", "off — every ACK is forwarded"),
    ("ack-filter", "drop redundant ACKs, keep the ones that carry news"),
    ("ack-filter-aggressive", "drop more, including some SACK detail"),
]

# tc names the tins by how many there are, exactly as q_cake.c does.
TIN_NAMES = {
    1: ["Tin 0"],
    3: ["Bulk", "Best Effort", "Voice"],
    4: ["Bulk", "Best Effort", "Video", "Voice"],
    8: ["Tin %d" % i for i in range(8)],
}

# Every per-tin field tc -s prints, in its print order, with the label tc uses.
TIN_FIELDS = [
    ("threshold_rate", "thresh", "rate"),
    ("target_us", "target", "us"),
    ("interval_us", "interval", "us"),
    ("peak_delay_us", "pk_delay", "us"),
    ("avg_delay_us", "av_delay", "us"),
    ("base_delay_us", "sp_delay", "us"),
    ("backlog_bytes", "backlog", "bytes"),
    ("sent_packets", "pkts", "int"),
    ("sent_bytes", "bytes", "bytes"),
    ("way_indirect_hits", "way_inds", "int"),
    ("way_misses", "way_miss", "int"),
    ("way_collisions", "way_cols", "int"),
    ("drops", "drops", "int"),
    ("ecn_mark", "marks", "int"),
    ("ack_drops", "ack_drop", "int"),
    ("sparse_flows", "sp_flows", "int"),
    ("bulk_flows", "bk_flows", "int"),
    ("unresponsive_flows", "un_flows", "int"),
    ("max_pkt_len", "max_len", "int"),
    ("flow_quantum", "quantum", "int"),
]


# --------------------------------------------------------------------------
# shelling out
# --------------------------------------------------------------------------

def run(cmd: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out: " + " ".join(cmd)
    except FileNotFoundError as e:
        return 127, "", str(e)


def sudo(cmd: list[str]) -> list[str]:
    return cmd if os.geteuid() == 0 else ["sudo", "-n"] + cmd


def tc_json(args: list[str]) -> tuple[list | None, str]:
    rc, out, err = run([TC, "-s", "-j"] + args)
    if rc != 0:
        return None, err.strip() or f"tc exited {rc}"
    try:
        return json.loads(out or "[]"), ""
    except json.JSONDecodeError as e:
        return None, f"bad json from tc: {e}"


# --------------------------------------------------------------------------
# building the cake command line
# --------------------------------------------------------------------------

def cake_args(cfg: dict) -> list[str]:
    """Turn the form's config into cake's argument list.

    Every option is emitted explicitly rather than left to inherit, because
    `tc qdisc replace` on an existing cake *keeps* whatever you don't mention:
    a stale `wash` or `ingress` from ten minutes ago survives and silently
    changes what you think you measured. The two exceptions are memlimit and
    the link-layer keywords, which have no "back to auto" spelling — for those,
    Rebuild (del + add) is the clean slate.
    """
    a: list[str] = []

    if cfg.get("unlimited"):
        a.append("unlimited")
    else:
        a += ["bandwidth", str(cfg.get("bandwidth", "50mbit"))]
    if cfg.get("autorate"):
        a.append("autorate-ingress")

    a.append(cfg.get("diffserv", "diffserv3"))
    a.append(cfg.get("flowmode", "triple-isolate"))
    a.append("nat" if cfg.get("nat") else "nonat")
    a.append("wash" if cfg.get("wash") else "nowash")
    a.append(cfg.get("ackfilter", "no-ack-filter"))
    a.append("split-gso" if cfg.get("split_gso", True) else "no-split-gso")
    a += ["rtt", "%dus" % int(cfg.get("rtt_us", 100_000))]

    if cfg.get("oh_mode") == "keyword":
        a.append(cfg.get("oh_keyword", "raw"))
    else:
        oh = int(cfg.get("overhead", 0))
        a += ["raw"] if oh == 0 and not cfg.get("mpu") else ["overhead", str(oh)]
        a += ["mpu", str(int(cfg.get("mpu", 0)))]
        a.append(cfg.get("atm", "noatm"))
    if cfg.get("ether_vlan"):
        a.append("ether-vlan")

    if cfg.get("memlimit"):
        a += ["memlimit", str(cfg["memlimit"])]
    if cfg.get("fwmark"):
        a += ["fwmark", str(cfg["fwmark"])]

    a.append("ingress" if cfg.get("ingress") else "egress")
    return a


def cake_command(dev: str, cfg: dict, mode: str) -> list[list[str]]:
    """mode: 'apply' (replace — keeps counters if it is already cake) or
    'rebuild' (del + add — fresh counters and every default back)."""
    spec = [TC, "qdisc", "%s", "dev", dev, "root", "cake"] + cake_args(cfg)
    if mode == "rebuild":
        return [
            [TC, "qdisc", "del", "dev", dev, "root"],
            [x if x != "%s" else "add" for x in spec],
        ]
    return [[x if x != "%s" else "replace" for x in spec]]


# --------------------------------------------------------------------------
# reading the qdisc
# --------------------------------------------------------------------------

def tin_names(n: int) -> list[str]:
    return TIN_NAMES.get(n) or ["Tin %d" % i for i in range(n)]


def busiest_tin(tins: list[dict], prev: list[dict] | None) -> int:
    """Which tin actually moved traffic since the last poll — that is the one
    whose delays belong in the headline. Falls back to the fullest, then to
    the one with the most bytes ever."""
    if not tins:
        return 0
    if prev and len(prev) == len(tins):
        deltas = [t.get("sent_bytes", 0) - p.get("sent_bytes", 0)
                  for t, p in zip(tins, prev)]
        if max(deltas) > 0:
            return deltas.index(max(deltas))
    backlogs = [t.get("backlog_bytes", 0) for t in tins]
    if max(backlogs) > 0:
        return backlogs.index(max(backlogs))
    totals = [t.get("sent_bytes", 0) for t in tins]
    return totals.index(max(totals))


def sum_tins(tins: list[dict], key: str) -> int:
    return sum(int(t.get(key, 0) or 0) for t in tins)


class DevState:
    """One interface's rolling view: latest full sample plus chart history."""

    def __init__(self, dev: str):
        self.dev = dev
        self.lock = threading.Lock()
        self.latest: dict | None = None
        self.prev_raw: dict | None = None
        self.prev_t: float = 0.0
        self.hist: deque = deque(maxlen=HISTORY)
        self.error: str = ""

    def ingest(self, q: dict | None, now: float) -> dict:
        """Fold one tc reading into a sample with per-second rates derived."""
        if q is None:
            with self.lock:
                self.latest = {"dev": self.dev, "present": False, "t": now}
                self.prev_raw = None
            return self.latest

        opts = q.get("options") or {}
        tins = q.get("tins") or []
        prev, dt = self.prev_raw, now - self.prev_t

        def rate(key: str, scale: float = 1.0) -> float:
            if not prev or dt <= 0:
                return 0.0
            d = (q.get(key, 0) or 0) - (prev.get(key, 0) or 0)
            return max(0.0, d * scale / dt)

        def tin_rate(key: str) -> float:
            if not prev or dt <= 0 or len(prev.get("tins", [])) != len(tins):
                return 0.0
            d = sum_tins(tins, key) - sum_tins(prev["tins"], key)
            return max(0.0, d / dt)

        bps = rate("bytes", 8.0)
        bi = busiest_tin(tins, prev.get("tins") if prev else None)
        tin = tins[bi] if tins else {}

        # bandwidth comes back as bytes/sec, or the string "unlimited"
        bw = opts.get("bandwidth")
        bw_bps = bw * 8 if isinstance(bw, (int, float)) else None
        backlog = q.get("backlog", 0) or 0
        # what the queue is worth in milliseconds — the number that matters
        drain_ms = (backlog * 8 * 1000.0 / bw_bps) if bw_bps else None

        s = {
            "dev": self.dev,
            "present": True,
            "t": now,
            "kind": q.get("kind"),
            "handle": q.get("handle"),
            "parent": q.get("parent"),
            "root": bool(q.get("root")),
            "refcnt": q.get("refcnt"),
            "opts": opts,
            "bytes": q.get("bytes", 0),
            "packets": q.get("packets", 0),
            "drops": q.get("drops", 0),
            "overlimits": q.get("overlimits", 0),
            "requeues": q.get("requeues", 0),
            "backlog": backlog,
            "qlen": q.get("qlen", 0),
            "memory_used": q.get("memory_used"),
            "memory_limit": q.get("memory_limit"),
            "capacity_estimate": q.get("capacity_estimate"),
            "min_network_size": q.get("min_network_size"),
            "max_network_size": q.get("max_network_size"),
            "min_adj_size": q.get("min_adj_size"),
            "max_adj_size": q.get("max_adj_size"),
            "avg_hdr_offset": q.get("avg_hdr_offset"),
            "tins": tins,
            "tin_names": tin_names(len(tins)),
            "busiest": bi,
            "bw_bps": bw_bps,
            "bps": bps,
            "pps": rate("packets"),
            "drops_ps": rate("drops"),
            "marks_ps": tin_rate("ecn_mark"),
            "ackdrops_ps": tin_rate("ack_drops"),
            "drain_ms": drain_ms,
            "util": (bps / bw_bps * 100.0) if bw_bps else None,
            "pk_delay_us": tin.get("peak_delay_us"),
            "av_delay_us": tin.get("avg_delay_us"),
            "sp_delay_us": tin.get("base_delay_us"),
            "sparse_flows": sum_tins(tins, "sparse_flows"),
            "bulk_flows": sum_tins(tins, "bulk_flows"),
            "unresp_flows": sum_tins(tins, "unresponsive_flows"),
            "marks": sum_tins(tins, "ecn_mark"),
            "ack_drops": sum_tins(tins, "ack_drops"),
            "cfg_str": config_string(q),
        }

        # the chart buffer is a flat tuple, not the whole sample — 1200 of
        # these per device is cheap, 1200 tin tables would not be
        point = [round(now, 2), round(bps), backlog, q.get("qlen", 0) or 0,
                 tin.get("peak_delay_us") or 0, tin.get("avg_delay_us") or 0,
                 tin.get("base_delay_us") or 0, round(s["drops_ps"], 2),
                 round(s["marks_ps"], 2), round(s["ackdrops_ps"], 2),
                 round(drain_ms, 2) if drain_ms is not None else 0,
                 s["sparse_flows"], s["bulk_flows"]]

        with self.lock:
            self.latest = s
            self.hist.append(point)
            self.prev_raw = q
            self.prev_t = now
        return s

    def history(self) -> list:
        with self.lock:
            return list(self.hist)

    def snapshot(self) -> dict | None:
        with self.lock:
            return self.latest


def fmt_rate(bps: float | None) -> str:
    if not bps:
        return "0"
    for unit, div in (("Gbit", 1e9), ("Mbit", 1e6), ("Kbit", 1e3)):
        if bps >= div:
            return f"{bps / div:.3g}{unit}"
    return f"{bps:.0f}bit"


def config_string(q: dict) -> str:
    """A one-line fingerprint of the qdisc, for capture rows and the log."""
    if not q:
        return "—"
    kind = q.get("kind", "?")
    if kind != "cake":
        return kind
    o = q.get("options") or {}
    bw = o.get("bandwidth")
    parts = [kind, "unlimited" if bw == "unlimited" else fmt_rate(
        bw * 8 if isinstance(bw, (int, float)) else None)]
    parts.append(o.get("diffserv", ""))
    parts.append(o.get("flowmode", ""))
    if o.get("nat"):
        parts.append("nat")
    if o.get("wash"):
        parts.append("wash")
    if o.get("ingress"):
        parts.append("ingress")
    af = o.get("ack-filter")
    parts.append({"enabled": "ack-filter",
                  "aggressive": "ack-filter-aggressive"}.get(af, "no-ack-filter"))
    if not o.get("split_gso", True):
        parts.append("no-split-gso")
    parts.append("rtt %gms" % ((o.get("rtt") or 0) / 1000.0))
    if o.get("raw"):
        parts.append("raw")
    else:
        parts.append("overhead %s" % o.get("overhead", 0))
        if o.get("mpu"):
            parts.append("mpu %s" % o["mpu"])
        if o.get("atm") and o["atm"] != "noatm":
            parts.append(o["atm"])
    return " ".join(p for p in parts if p)


# --------------------------------------------------------------------------
# the ingress (ifb0) plumbing
# --------------------------------------------------------------------------

def ifb_status(wan: str, ifb: str) -> dict:
    """Is the download path actually wired up? Three separate things have to
    be true, and reporting them separately is the difference between "it
    works" and "the qdisc exists but nothing is redirected into it"."""
    rc, out, _ = run([IP, "-j", "link", "show", ifb])
    exists, up = False, False
    if rc == 0:
        try:
            link = json.loads(out or "[]")
            if link:
                exists = True
                up = "UP" in (link[0].get("flags") or [])
        except json.JSONDecodeError:
            pass

    rc, out, _ = run([TC, "qdisc", "show", "dev", wan, "ingress"])
    has_ingress = "ingress" in out

    # tc prints "Egress Redirect to device ifb0" — not "dev ifb0". Matching
    # the wrong string here is why an earlier version reported no redirect
    # while packets were plainly flowing.
    rc, out, _ = run([TC, "filter", "show", "dev", wan, "parent", "ffff:"])
    redirected = ("device %s" % ifb) in out

    return {"dev": ifb, "exists": exists, "up": up,
            "ingress_qdisc": has_ingress, "redirect": redirected,
            "ready": exists and up and has_ingress and redirected}


def ifb_install(wan: str, ifb: str, cfg: dict) -> tuple[bool, str]:
    log = []
    steps = [
        ([IP, "link", "add", ifb, "type", "ifb"], True),
        ([IP, "link", "set", ifb, "up"], False),
        ([TC, "qdisc", "del", "dev", wan, "ingress"], True),
        ([TC, "qdisc", "add", "dev", wan, "handle", "ffff:", "ingress"], False),
        ([TC, "filter", "add", "dev", wan, "parent", "ffff:", "protocol", "all",
          "prio", "10", "matchall", "action", "mirred", "egress", "redirect",
          "dev", ifb], False),
    ]
    for cmd, soft in steps:
        rc, out, err = run(sudo(cmd))
        log.append("$ %s\n%s" % (" ".join(cmd), (err or out).strip()))
        if rc != 0 and not soft:
            return False, "\n".join(log)

    ingress_cfg = dict(cfg)
    ingress_cfg["ingress"] = True
    for cmd in cake_command(ifb, ingress_cfg, "rebuild"):
        rc, out, err = run(sudo(cmd))
        log.append("$ %s\n%s" % (" ".join(cmd), (err or out).strip()))
    return True, "\n".join(log)


def ifb_remove(wan: str, ifb: str) -> tuple[bool, str]:
    log = []
    # Scan every interface for a redirect into ifb0 rather than trusting the
    # WAN name we were handed — if the redirect was installed against a
    # different device, deleting only ifb0 leaves a filter pointing at a
    # device that no longer exists.
    devs = {wan}
    rc, out, _ = run([IP, "-j", "link", "show"])
    if rc == 0:
        try:
            devs |= {l.get("ifname") for l in json.loads(out or "[]") if l.get("ifname")}
        except json.JSONDecodeError:
            pass
    for d in sorted(x for x in devs if x and x != ifb):
        rc, out, _ = run([TC, "filter", "show", "dev", d, "parent", "ffff:"])
        if rc == 0 and ("device %s" % ifb) in out:
            for cmd in ([TC, "qdisc", "del", "dev", d, "ingress"],):
                rc2, o2, e2 = run(sudo(cmd))
                log.append("$ %s\n%s" % (" ".join(cmd), (e2 or o2).strip()))
    for cmd in ([TC, "qdisc", "del", "dev", ifb, "root"],
                [IP, "link", "del", ifb]):
        rc, out, err = run(sudo(cmd))
        log.append("$ %s\n%s" % (" ".join(cmd), (err or out).strip()))
    return True, "\n".join(log)


# --------------------------------------------------------------------------
# engine: polling, events, capture windows
# --------------------------------------------------------------------------

class Engine:
    def __init__(self, wan: str, ifb: str):
        self.wan, self.ifb = wan, ifb
        self.devs = {wan: DevState(wan), ifb: DevState(ifb)}
        self.subs: list[queue.Queue] = []
        self.subs_lock = threading.Lock()
        self.captures: list[dict] = []
        self.active_capture: dict | None = None
        self.cap_lock = threading.Lock()
        self.log: deque = deque(maxlen=60)
        self.stop = threading.Event()

    # -- pub/sub ----------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=40)
        with self.subs_lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.subs_lock:
            if q in self.subs:
                self.subs.remove(q)

    def publish(self, ev: dict) -> None:
        blob = json.dumps(ev)
        with self.subs_lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(blob)
            except queue.Full:
                pass    # a stalled browser must not stall the poller

    def note(self, text: str) -> None:
        self.log.appendleft({"t": time.time(), "text": text})

    # -- polling ----------------------------------------------------------
    def poll_loop(self) -> None:
        period = 1.0 / POLL_HZ
        while not self.stop.is_set():
            t0 = time.time()
            try:
                self.poll_once()
            except Exception as e:                      # never kill the thread
                self.publish({"type": "error", "text": repr(e)})
            self.stop.wait(max(0.0, period - (time.time() - t0)))

    def poll_once(self) -> None:
        now = time.time()
        qs, err = tc_json(["qdisc", "show"])
        roots: dict[str, dict] = {}
        if qs:
            for q in qs:
                d = q.get("dev")
                if d in self.devs and q.get("root"):
                    roots[d] = q
        samples = {d: st.ingest(roots.get(d), now) for d, st in self.devs.items()}
        self.publish({"type": "tick", "t": now, "devs": samples, "err": err})

    # -- capture windows --------------------------------------------------
    def capture_start(self, label: str) -> dict:
        with self.cap_lock:
            self.active_capture = {
                "label": label or "",
                "t0": time.time(),
                "start": {d: (st.snapshot() or {}) for d, st in self.devs.items()},
            }
            self.note("capture started: %s" % (label or "(unlabelled)"))
            return {"ok": True}

    def capture_stop(self) -> dict:
        with self.cap_lock:
            cap = self.active_capture
            self.active_capture = None
        if not cap:
            return {"ok": False, "error": "no capture running"}
        t1 = time.time()
        row = {"label": cap["label"], "t0": cap["t0"], "t1": t1,
               "secs": round(t1 - cap["t0"], 1), "devs": {}}
        for d, st in self.devs.items():
            a, b = cap["start"].get(d) or {}, st.snapshot() or {}
            if not b.get("present"):
                continue
            hist = [p for p in st.history() if cap["t0"] <= p[0] <= t1]
            bps = [p[1] for p in hist] or [0]
            backl = [p[2] for p in hist] or [0]
            pk = [p[4] for p in hist] or [0]
            av = [p[5] for p in hist] or [0]
            drain = [p[10] for p in hist] or [0]

            def d_top(k):
                return max(0, (b.get(k) or 0) - (a.get(k) or 0)) if a.get("present") else b.get(k) or 0

            row["devs"][d] = {
                "cfg_start": a.get("cfg_str", "—"),
                "cfg_end": b.get("cfg_str", "—"),
                # the mistake that silently spoils a run: the shaper changed
                # halfway through and the row averages two configurations
                "changed": a.get("cfg_str") != b.get("cfg_str") if a.get("present") else False,
                "avg_bps": sum(bps) / len(bps),
                "max_bps": max(bps),
                "avg_backlog": sum(backl) / len(backl),
                "max_backlog": max(backl),
                "avg_drain_ms": sum(drain) / len(drain),
                "max_drain_ms": max(drain),
                "avg_pk_us": sum(pk) / len(pk),
                "max_pk_us": max(pk),
                "avg_av_us": sum(av) / len(av),
                "bytes": d_top("bytes"),
                "packets": d_top("packets"),
                "drops": d_top("drops"),
                "overlimits": d_top("overlimits"),
                "requeues": d_top("requeues"),
                "marks": d_top("marks"),
                "ack_drops": d_top("ack_drops"),
            }
        self.captures.insert(0, row)
        del self.captures[MAX_CAPTURES:]
        self.note("capture stopped: %s (%.1fs)" % (row["label"] or "(unlabelled)", row["secs"]))
        return {"ok": True, "row": row}

    # -- applying ---------------------------------------------------------
    def apply(self, dev: str, cfg: dict, mode: str) -> dict:
        if dev not in self.devs:
            return {"ok": False, "error": "unknown device %r" % dev}
        if dev == self.ifb and not ifb_status(self.wan, self.ifb)["exists"]:
            return {"ok": False,
                    "error": "%s does not exist — build the download path first"
                             % self.ifb}
        cmds = cake_command(dev, cfg, mode)
        out_log, ok = [], True
        for cmd in cmds:
            rc, out, err = run(sudo(cmd))
            txt = (err or out).strip()
            out_log.append("$ %s%s" % (" ".join(cmd), ("\n" + txt) if txt else ""))
            # the `del` half of a rebuild fails harmlessly when there is no
            # qdisc to delete; a failing `add`/`replace` is real
            if rc != 0 and cmd[2] != "del":
                ok = False
                break
        self.note(("applied to %s: " % dev) + " ".join(cake_args(cfg)))
        self.poll_once()
        return {"ok": ok, "log": "\n".join(out_log),
                "command": " ".join(cmds[-1])}

    def clear(self, dev: str) -> dict:
        rc, out, err = run(sudo([TC, "qdisc", "del", "dev", dev, "root"]))
        self.note("cleared qdisc on %s" % dev)
        self.poll_once()
        return {"ok": rc == 0, "log": (err or out).strip() or "(no output)"}


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    engine: Engine = None       # type: ignore[assignment]

    def log_message(self, fmt, *args):      # keep the console for our own notes
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- GET --------------------------------------------------------------
    def do_GET(self) -> None:
        eng = self.engine
        path = self.path.split("?", 1)[0]
        qs = {}
        if "?" in self.path:
            for pair in self.path.split("?", 1)[1].split("&"):
                k, _, v = pair.partition("=")
                qs[k] = v

        if path == "/":
            self._send(200, PAGE_BYTES, "text/html; charset=utf-8")
        elif path == "/api/meta":
            self._json({
                "wan": eng.wan, "ifb": eng.ifb, "poll_hz": POLL_HZ,
                "host": os.uname().nodename,
                "rtt_presets": RTT_PRESETS, "overhead_keywords": OVERHEAD_KEYWORDS,
                "flowmodes": FLOWMODES, "diffserv": DIFFSERV,
                "ack_filter": ACK_FILTER, "tin_fields": TIN_FIELDS,
                "root": os.geteuid() == 0,
            })
        elif path == "/api/history":
            self._json({d: st.history() for d, st in eng.devs.items()})
        elif path == "/api/state":
            self._json({
                "devs": {d: st.snapshot() for d, st in eng.devs.items()},
                "ifb": ifb_status(eng.wan, eng.ifb),
                "captures": eng.captures,
                "capturing": bool(eng.active_capture),
                "log": list(eng.log),
            })
        elif path == "/api/ifb":
            self._json(ifb_status(eng.wan, eng.ifb))
        elif path == "/api/raw":
            dev = qs.get("dev") or eng.wan
            rc, out, err = run([TC, "-s", "qdisc", "show", "dev", dev])
            rc2, out2, _ = run([TC, "-s", "class", "show", "dev", dev])
            self._json({"text": out or err, "classes": out2.strip()})
        elif path == "/api/captures.csv":
            self._send(200, export_csv(eng).encode(), "text/csv")
        elif path == "/api/captures.json":
            self._send(200, json.dumps(eng.captures, indent=2).encode(),
                       "application/json")
        elif path == "/api/stream":
            self.stream()
        else:
            self._json({"error": "not found"}, 404)

    # -- POST -------------------------------------------------------------
    def do_POST(self) -> None:
        eng = self.engine
        path = self.path.split("?", 1)[0]
        body = self._body()

        if path == "/api/apply":
            self._json(eng.apply(body.get("dev", eng.wan),
                                 body.get("cfg") or {},
                                 body.get("mode", "apply")))
        elif path == "/api/preview":
            dev = body.get("dev", eng.wan)
            cmds = cake_command(dev, body.get("cfg") or {}, body.get("mode", "apply"))
            self._json({"commands": [" ".join(c) for c in cmds]})
        elif path == "/api/clear":
            self._json(eng.clear(body.get("dev", eng.wan)))
        elif path == "/api/ifb/install":
            ok, log = ifb_install(eng.wan, eng.ifb, body.get("cfg") or {})
            eng.note("download path built on %s -> %s" % (eng.wan, eng.ifb))
            eng.poll_once()
            self._json({"ok": ok, "log": log,
                        "status": ifb_status(eng.wan, eng.ifb)})
        elif path == "/api/ifb/remove":
            ok, log = ifb_remove(eng.wan, eng.ifb)
            eng.note("download path torn down")
            eng.poll_once()
            self._json({"ok": ok, "log": log,
                        "status": ifb_status(eng.wan, eng.ifb)})
        elif path == "/api/capture/start":
            self._json(eng.capture_start(body.get("label", "")))
        elif path == "/api/capture/stop":
            self._json(eng.capture_stop())
        elif path == "/api/capture/clear":
            eng.captures.clear()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    # -- SSE --------------------------------------------------------------
    def stream(self) -> None:
        eng = self.engine
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = eng.subscribe()
        try:
            while True:
                try:
                    blob = q.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")     # through proxies
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: " + blob.encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
            pass
        finally:
            eng.unsubscribe(q)


def export_csv(eng: Engine) -> str:
    cols = ["label", "seconds", "dev", "config", "changed_mid_run",
            "avg_mbit", "max_mbit", "avg_backlog_bytes", "max_backlog_bytes",
            "avg_drain_ms", "max_drain_ms", "avg_pk_delay_us", "max_pk_delay_us",
            "avg_av_delay_us", "bytes", "packets", "drops", "overlimits",
            "requeues", "ecn_marks", "ack_drops"]
    lines = [",".join(cols)]
    for r in eng.captures:
        for dev, d in r["devs"].items():
            row = [r["label"], r["secs"], dev, d["cfg_end"],
                   "yes" if d["changed"] else "",
                   round(d["avg_bps"] / 1e6, 3), round(d["max_bps"] / 1e6, 3),
                   round(d["avg_backlog"]), d["max_backlog"],
                   round(d["avg_drain_ms"], 2), round(d["max_drain_ms"], 2),
                   round(d["avg_pk_us"]), d["max_pk_us"], round(d["avg_av_us"]),
                   d["bytes"], d["packets"], d["drops"], d["overlimits"],
                   d["requeues"], d["marks"], d["ack_drops"]]
            lines.append(",".join('"%s"' % c if isinstance(c, str) and "," in c
                                  else str(c) for c in row))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>caketune</title>
<style>
  :root{
    --bg:#0e1116; --panel:#161b22; --panel2:#1c2230; --line:#2a3140;
    --fg:#e6edf3; --dim:#8b97a8; --accent:#4aa8ff; --up:#4aa8ff; --down:#c792ea;
    --ok:#3fb950; --warn:#d29922; --bad:#f85149; --pk:#ff8a5b; --av:#d29922;
    --sp:#3fb950; --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{padding:16px 18px;max-width:1680px;margin:0 auto}
  h1{font-size:15px;margin:0;letter-spacing:.12em;text-transform:uppercase}
  h1 span{color:var(--dim);letter-spacing:0;text-transform:none;font-weight:400;
          font-size:12px;margin-left:10px;font-family:var(--mono)}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
         padding:14px 16px;margin-bottom:14px}
  .head{display:flex;justify-content:space-between;align-items:baseline;
        gap:12px;margin-bottom:10px;flex-wrap:wrap}
  .head h2{font-size:12px;margin:0;letter-spacing:.07em;text-transform:uppercase;
           color:var(--dim);font-weight:600}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
  label{display:flex;flex-direction:column;gap:4px;font-size:10px;color:var(--dim);
        text-transform:uppercase;letter-spacing:.06em}
  label.cb{flex-direction:row;align-items:center;gap:6px;text-transform:none;
           font-size:12px;letter-spacing:0;color:var(--fg);cursor:pointer;
           background:var(--panel2);border:1px solid var(--line);border-radius:6px;
           padding:7px 10px}
  label.cb:hover{border-color:var(--dim)}
  label.cb input{min-width:0;accent-color:var(--accent)}
  input,select{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
        border-radius:6px;padding:7px 9px;font:13px var(--mono);min-width:80px}
  input:focus,select:focus{outline:1px solid var(--accent);border-color:var(--accent)}
  select{font-family:inherit;font-size:12px}
  button{background:var(--accent);color:#06121f;border:0;border-radius:6px;
         padding:9px 15px;font-weight:600;font-size:13px;cursor:pointer}
  button:disabled{opacity:.4;cursor:not-allowed}
  button.ghost{background:transparent;color:var(--dim);border:1px solid var(--line)}
  button.ghost:hover:not(:disabled){color:var(--fg);border-color:var(--dim)}
  button.ghost.on{color:var(--fg);border-color:var(--accent);background:var(--panel2)}
  button.small{padding:5px 10px;font-size:11px}
  button.danger{background:transparent;color:var(--bad);border:1px solid var(--line)}
  button.danger:hover{border-color:var(--bad)}
  .seg{display:flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
  .seg button{background:transparent;color:var(--dim);border:0;border-radius:0;
              padding:7px 12px;font:12px/1.2 var(--mono);white-space:nowrap}
  .seg button.on{background:var(--accent);color:#06121f;font-weight:700}
  .seg button:not(.on):hover{background:var(--panel2);color:var(--fg)}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  @media(max-width:1200px){.grid2{grid-template-columns:1fr}}
  .nums{display:flex;gap:20px;flex-wrap:wrap;margin:4px 0 10px;font:12px var(--mono)}
  .nums div{min-width:74px}
  .nums b{display:block;font:600 20px/1.25 var(--mono);color:var(--fg)}
  .nums span{color:var(--dim);font-size:10px;text-transform:uppercase;
             letter-spacing:.06em}
  .nums b.ok{color:var(--ok)} .nums b.warn{color:var(--warn)} .nums b.bad{color:var(--bad)}
  canvas{width:100%;display:block}
  .chartbox{margin-bottom:8px}
  .legend{display:flex;gap:12px;font:10px var(--mono);color:var(--dim);
          justify-content:flex-end}
  .legend i{display:inline-block;width:9px;height:9px;border-radius:2px;
            margin-right:4px;vertical-align:middle}
  table{width:100%;border-collapse:collapse;font:11.5px var(--mono)}
  th,td{padding:4px 7px;text-align:right;border-bottom:1px solid var(--line);
        white-space:nowrap}
  th{color:var(--dim);font-weight:500;font-size:10px;text-transform:uppercase;
     letter-spacing:.05em}
  th:first-child,td:first-child{text-align:left}
  td.k{color:var(--dim)}
  tbody tr:hover{background:var(--panel2)}
  tr.hot td{color:var(--fg)}
  .opts{display:flex;gap:5px;flex-wrap:wrap;margin:0 0 8px}
  .opts span{background:var(--panel2);border:1px solid var(--line);border-radius:4px;
             padding:2px 7px;font:11px var(--mono);color:var(--dim)}
  .opts span.on{color:var(--fg);border-color:#3d4b63}
  .opts span b{color:var(--fg);font-weight:600}
  pre{background:#0a0d12;border:1px solid var(--line);border-radius:6px;
      padding:10px 12px;font:11.5px/1.5 var(--mono);overflow:auto;margin:0;
      color:var(--dim);max-height:420px}
  pre b{color:var(--fg);font-weight:600}
  .cmd{background:#0a0d12;border:1px solid var(--line);border-radius:6px;
       padding:10px 12px;font:12px/1.6 var(--mono);color:var(--ok);
       word-break:break-all;white-space:pre-wrap;user-select:all}
  .err{color:var(--bad);font:11.5px var(--mono);white-space:pre-wrap;margin-top:8px}
  .hint{color:var(--dim);font-size:11.5px;margin-top:8px}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;
       margin-right:6px;background:var(--bad)}
  .dot.on{background:var(--ok)} .dot.part{background:var(--warn)}
  .pill{font:11px var(--mono);padding:2px 8px;border-radius:99px;
        border:1px solid var(--line);color:var(--dim)}
  .pill.on{color:var(--ok);border-color:rgba(63,185,80,.5)}
  .pill.off{color:var(--bad);border-color:rgba(248,81,73,.5)}
  .warnrow{color:var(--warn)}
  details summary{cursor:pointer;color:var(--dim);font:11px var(--mono);
                  text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
  details[open] summary{color:var(--fg)}
  .sub{color:var(--dim);font-size:11px;font-family:var(--mono)}
</style></head><body>
<div class="wrap">

  <div class="head" style="margin-bottom:12px">
    <h1>caketune <span id="hostline"></span></h1>
    <div class="row" style="gap:8px">
      <span class="pill" id="conn">connecting…</span>
      <span class="pill" id="tinfo"></span>
    </div>
  </div>

  <!-- ================= tuner ================= -->
  <div class="panel">
    <div class="head">
      <h2>Tune</h2>
      <div class="row" style="gap:8px">
        <div class="seg" id="devseg"></div>
        <button class="ghost small" id="loadcur">Load from device</button>
      </div>
    </div>

    <div class="row" style="margin-bottom:10px">
      <label>bandwidth <input id="bandwidth" size="9" style="width:110px" value="50mbit"></label>
      <label class="cb" title="no shaping — cake still does AQM and flow isolation">
        <input type="checkbox" id="unlimited"> unlimited</label>
      <label class="cb" title="estimate the rate from what the ingress link actually delivers">
        <input type="checkbox" id="autorate"> autorate-ingress</label>
      <label>rtt (ms) <input id="rtt_ms" type="number" step="0.1" min="0.01" style="width:90px" value="100"></label>
      <label>rtt preset <select id="rttpreset"></select></label>
      <label class="cb" title="cake's ingress mode: account for what was dropped, so the shaper hits the target rate as seen by the sender">
        <input type="checkbox" id="ingress"> ingress mode</label>
    </div>

    <div class="row" style="margin-bottom:10px">
      <label style="min-width:260px">ack-filter <select id="ackfilter"></select></label>
      <label style="min-width:260px">flow isolation <select id="flowmode"></select></label>
      <label style="min-width:240px">diffserv <select id="diffserv"></select></label>
      <label class="cb" title="look through NAT so per-host fairness sees the LAN address, not the router's">
        <input type="checkbox" id="nat"> nat</label>
      <label class="cb" title="clear DSCP on the way out">
        <input type="checkbox" id="wash"> wash</label>
      <label class="cb" title="split GSO superpackets so the shaper is accurate at low rates">
        <input type="checkbox" id="split_gso" checked> split-gso</label>
    </div>

    <div class="row" style="margin-bottom:10px">
      <div class="seg" id="ohseg">
        <button data-v="manual" class="on">manual overhead</button>
        <button data-v="keyword">link-layer keyword</button>
      </div>
      <label id="l_ohkw" style="min-width:320px;display:none">keyword
        <select id="oh_keyword"></select></label>
      <label id="l_oh">overhead <input id="overhead" type="number" style="width:90px" value="0"></label>
      <label id="l_mpu">mpu <input id="mpu" type="number" min="0" style="width:80px" value="0"></label>
      <label id="l_atm">framing <select id="atm">
        <option value="noatm">noatm</option><option value="atm">atm (53/48 cells)</option>
        <option value="ptm">ptm (65/64)</option></select></label>
      <label class="cb" title="add 4 bytes for a VLAN tag on top of the keyword">
        <input type="checkbox" id="ether_vlan"> ether-vlan (+4)</label>
      <label>memlimit <input id="memlimit" size="7" style="width:100px" placeholder="auto"></label>
      <label>fwmark <input id="fwmark" size="6" style="width:90px" placeholder="none"></label>
    </div>

    <div class="cmd" id="preview">…</div>
    <div class="row" style="margin-top:10px">
      <button id="apply">Apply</button>
      <button id="rebuild" class="ghost">Rebuild (reset counters)</button>
      <button id="clear" class="danger">Remove qdisc</button>
      <span class="hint" style="margin:0 0 0 auto;align-self:center">
        Apply = <code>tc qdisc replace</code>: counters and flow state survive, so you can
        turn a knob mid-test. Rebuild = <code>del</code> + <code>add</code>: fresh counters,
        and the only way back to an auto memlimit.</span>
    </div>
    <div class="err" id="applyerr"></div>
  </div>

  <!-- ================= ifb / download path ================= -->
  <div class="panel">
    <div class="head">
      <h2>Download path (ingress → ifb0)</h2>
      <div class="row" style="gap:8px">
        <button class="ghost small" id="ifbinstall">Build download shaper</button>
        <button class="danger small" id="ifbremove">Tear down</button>
      </div>
    </div>
    <div class="opts" id="ifbstate"></div>
    <div class="hint">Egress on the WAN shapes what you send. The download direction has no
      egress queue on the WAN to attach to, so packets arriving on it are mirrored into a
      virtual device — <b>ifb0</b> — and shaped there. Build uses the parameters above,
      forcing <code>ingress</code> on. Traffic counted here is what the router
      <i>received</i>. Per-host fairness wants <code>dual-dsthost</code> in this
      direction, not <code>dual-srchost</code> — the LAN hosts are the receivers here.</div>
    <pre id="ifblog" style="margin-top:10px;display:none"></pre>
  </div>

  <!-- ================= live ================= -->
  <div class="grid2" id="cards"></div>

  <!-- ================= capture ================= -->
  <div class="panel">
    <div class="head">
      <h2>Capture window</h2>
      <div class="row" style="gap:8px">
        <input id="caplabel" placeholder="e.g. ack-filter off, 50/10" style="width:280px;font-family:inherit">
        <button id="capbtn">Start capture</button>
        <button class="ghost small" id="capcsv">CSV</button>
        <button class="ghost small" id="capjson">JSON</button>
        <button class="danger small" id="capclear">Clear</button>
      </div>
    </div>
    <div class="hint" style="margin:0 0 10px">Bracket an external test: Start, run your
      libreqos / flent / browser test through the router, Stop. The row keeps the config that
      was in force and what the qdisc did during the window — and flags ⚠ if the config
      changed halfway through, which is the mistake that quietly ruins a comparison.</div>
    <div style="overflow-x:auto"><table id="captable"><thead></thead><tbody></tbody></table></div>
  </div>

  <div class="panel">
    <details><summary>Raw tc output</summary>
      <div class="row" style="margin-bottom:10px">
        <div class="seg" id="rawseg"></div>
        <button class="ghost small" id="rawrefresh">Refresh</button>
      </div>
      <pre id="rawout">—</pre>
    </details>
  </div>

  <div class="panel">
    <details><summary>Activity</summary><pre id="log">—</pre></details>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let META = null, DEV = null, HIST = {}, LATEST = {}, CAPTURING = false;
const MAXPTS = 1200;

const fmtBits = b => !b ? "0 " :
  b >= 1e9 ? (b/1e9).toFixed(2)+" G" : b >= 1e6 ? (b/1e6).toFixed(1)+" M" :
  b >= 1e3 ? (b/1e3).toFixed(0)+" k" : b.toFixed(0)+" ";
const fmtBytes = b => b == null ? "—" :
  b >= 1048576 ? (b/1048576).toFixed(1)+" MB" : b >= 1024 ? (b/1024).toFixed(1)+" KB" :
  b+" B";
const fmtUs = u => u == null ? "—" :
  u >= 1000 ? (u/1000).toFixed(u >= 10000 ? 0 : 1)+" ms" : u+" µs";
const fmtInt = n => n == null ? "—" : n.toLocaleString();
const esc = s => String(s == null ? "" : s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

// ---------------------------------------------------------------- charts
function drawChart(cv, series, opts) {
  opts = opts || {};
  const dpr = window.devicePixelRatio || 1;
  // pin the CSS height from the original attribute before touching the backing
  // store, otherwise each redraw halves the drawing on a retina display
  if (!cv._h) { cv._h = (+cv.getAttribute("height")) || 110; cv.style.height = cv._h + "px"; }
  const w = cv.clientWidth, h = cv._h;
  if (cv.width !== Math.round(w*dpr)) { cv.width = Math.round(w*dpr); cv.height = Math.round(h*dpr); }
  const g = cv.getContext("2d");
  g.setTransform(dpr,0,0,dpr,0,0);
  g.clearRect(0,0,w,h);
  const padL = 46, padR = 6, padT = 8, padB = 14;
  const iw = w-padL-padR, ih = h-padT-padB;

  let max = opts.min || 0;
  for (const s of series) for (const v of s.data) if (v > max) max = v;
  if (max <= 0) max = opts.min || 1;
  max *= 1.15;

  g.strokeStyle = "#2a3140"; g.fillStyle = "#8b97a8";
  g.font = "10px ui-monospace,monospace"; g.textAlign = "right";
  for (let i = 0; i <= 3; i++) {
    const y = padT + ih - ih*i/3;
    g.beginPath(); g.moveTo(padL,y); g.lineTo(w-padR,y); g.stroke();
    g.fillText((opts.fmt||fmtInt)(max*i/3), padL-6, y+3);
  }

  const n = Math.max(...series.map(s => s.data.length), 1);
  const step = iw / Math.max(n-1, 1);
  for (const s of series) {
    if (!s.data.length) continue;
    const off = n - s.data.length;
    g.strokeStyle = s.color; g.lineWidth = s.width || 1.6;
    g.beginPath();
    s.data.forEach((v,i) => {
      const x = padL + (i+off)*step, y = padT + ih - (v/max)*ih;
      i ? g.lineTo(x,y) : g.moveTo(x,y);
    });
    g.stroke();
    if (s.fill) {
      g.lineTo(padL + (s.data.length-1+off)*step, padT+ih);
      g.lineTo(padL + off*step, padT+ih);
      g.closePath(); g.fillStyle = s.fill; g.fill();
    }
  }
}

// ---------------------------------------------------------------- cards
function cardHTML(dev, role) {
  return `<div class="panel" data-card="${dev}">
    <div class="head">
      <h2><span class="dot" id="d_${dev}"></span>${dev} <span class="sub">— ${role}</span></h2>
      <div class="sub" id="kind_${dev}">—</div>
    </div>
    <div class="opts" id="opts_${dev}"></div>
    <div class="nums" id="nums_${dev}"></div>
    <div class="chartbox">
      <div class="legend"><span><i style="background:var(--accent)"></i>throughput</span></div>
      <canvas id="c1_${dev}" height="110"></canvas></div>
    <div class="chartbox">
      <div class="legend">
        <span><i style="background:var(--pk)"></i>pk_delay</span>
        <span><i style="background:var(--av)"></i>av_delay</span>
        <span><i style="background:var(--sp)"></i>sp_delay</span></div>
      <canvas id="c2_${dev}" height="110"></canvas></div>
    <div class="chartbox">
      <div class="legend">
        <span><i style="background:var(--down)"></i>backlog bytes</span>
        <span><i style="background:var(--warn)"></i>drain ms</span></div>
      <canvas id="c3_${dev}" height="110"></canvas></div>
    <div class="chartbox">
      <div class="legend">
        <span><i style="background:var(--bad)"></i>drops/s</span>
        <span><i style="background:var(--ok)"></i>marks/s</span>
        <span><i style="background:var(--accent)"></i>ack drops/s</span></div>
      <canvas id="c4_${dev}" height="110"></canvas></div>
    <div style="overflow-x:auto;margin-top:6px"><table id="tin_${dev}"><thead></thead><tbody></tbody></table></div>
  </div>`;
}

function renderCard(dev, s) {
  const dot = $("d_"+dev), kind = $("kind_"+dev);
  if (!s || !s.present) {
    dot.className = "dot"; kind.textContent = "no qdisc";
    $("opts_"+dev).innerHTML = ""; $("nums_"+dev).innerHTML = "";
    $("tin_"+dev).querySelector("thead").innerHTML = "";
    $("tin_"+dev).querySelector("tbody").innerHTML = "";
    return;
  }
  dot.className = "dot " + (s.kind === "cake" ? "on" : "part");
  kind.textContent = s.kind + " " + (s.handle||"") + (s.root ? " root" : "");

  const o = s.opts || {};
  const chip = (k,v,on) => `<span class="${on?'on':''}">${k} <b>${v}</b></span>`;
  let chips = "";
  if (s.kind === "cake") {
    chips += chip("bandwidth", o.bandwidth === "unlimited" ? "unlimited" :
                  fmtBits(o.bandwidth*8)+"bit", true);
    chips += chip("rtt", (o.rtt/1000)+" ms", true);
    const af = {enabled:"ack-filter", aggressive:"aggressive"}[o["ack-filter"]] || "off";
    chips += chip("ack-filter", af, o["ack-filter"] !== "disabled");
    chips += chip("flowmode", o.flowmode, true);
    chips += chip("diffserv", o.diffserv, true);
    chips += chip("nat", o.nat ? "on" : "off", o.nat);
    chips += chip("wash", o.wash ? "on" : "off", o.wash);
    chips += chip("ingress", o.ingress ? "on" : "off", o.ingress);
    chips += chip("split-gso", o.split_gso ? "on" : "off", o.split_gso);
    chips += o.raw ? chip("overhead", "raw", false)
                   : chip("overhead", o.overhead + (o.mpu ? " mpu "+o.mpu : ""), true);
    if (o.atm && o.atm !== "noatm") chips += chip("framing", o.atm, true);
    chips += chip("memory", fmtBytes(s.memory_used)+" / "+fmtBytes(s.memory_limit), true);
    if (s.capacity_estimate) chips += chip("capacity est", fmtBits(s.capacity_estimate*8)+"bit", true);
    chips += chip("net size", s.min_network_size+"–"+s.max_network_size, false);
    chips += chip("adj size", s.min_adj_size+"–"+s.max_adj_size, false);
    chips += chip("hdr offset", s.avg_hdr_offset, false);
  } else {
    for (const [k,v] of Object.entries(o)) chips += chip(k, esc(v), false);
  }
  $("opts_"+dev).innerHTML = chips;

  const drainCls = s.drain_ms == null ? "" :
    s.drain_ms < 10 ? "ok" : s.drain_ms < 40 ? "warn" : "bad";
  const num = (v,l,c) => `<div><b class="${c||''}">${v}</b><span>${l}</span></div>`;
  $("nums_"+dev).innerHTML =
    num(fmtBits(s.bps)+"bit/s", "throughput") +
    num(s.util == null ? "—" : s.util.toFixed(0)+"%", "of shaped rate") +
    num(fmtBytes(s.backlog), "backlog") +
    num(fmtInt(s.qlen), "backlog pkts") +
    num(s.drain_ms == null ? "—" : s.drain_ms.toFixed(1)+" ms", "drain time", drainCls) +
    num(fmtUs(s.pk_delay_us), "pk_delay") +
    num(fmtUs(s.av_delay_us), "av_delay") +
    num(fmtUs(s.sp_delay_us), "sp_delay") +
    num(fmtInt(s.sparse_flows)+"/"+fmtInt(s.bulk_flows), "sparse / bulk") +
    num(fmtInt(s.drops), "drops", s.drops ? "warn" : "") +
    num(fmtInt(s.marks), "ecn marks") +
    num(fmtInt(s.ack_drops), "ack drops", s.ack_drops ? "ok" : "") +
    num(fmtInt(s.overlimits), "overlimits") +
    num(fmtInt(s.requeues), "requeues");

  const h = HIST[dev] || [];
  const col = i => h.map(p => p[i]);
  drawChart($("c1_"+dev), [{data: col(1), color: "#4aa8ff", fill: "rgba(74,168,255,.12)"}],
            {fmt: v => fmtBits(v)+"bit"});
  drawChart($("c2_"+dev), [
    {data: col(4), color: "#ff8a5b"}, {data: col(5), color: "#d29922"},
    {data: col(6), color: "#3fb950"}], {fmt: fmtUs, min: 1000});
  drawChart($("c3_"+dev), [
    {data: col(2), color: "#c792ea", fill: "rgba(199,146,234,.10)"}], {fmt: fmtBytes});
  drawChart($("c4_"+dev), [
    {data: col(7), color: "#f85149"}, {data: col(8), color: "#3fb950"},
    {data: col(9), color: "#4aa8ff"}], {fmt: v => v.toFixed(0)+"/s", min: 1});

  // per-tin table: every field tc -s prints, transposed so tins are columns
  const t = $("tin_"+dev), tins = s.tins || [], names = s.tin_names || [];
  if (!tins.length) { t.querySelector("thead").innerHTML = ""; t.querySelector("tbody").innerHTML = ""; return; }
  t.querySelector("thead").innerHTML = "<tr><th></th>" +
    names.map((n,i) => `<th${i===s.busiest?' style="color:var(--fg)"':''}>${esc(n)}</th>`).join("") + "</tr>";
  const fmtOf = {rate: v => v == null ? "—" : fmtBits(v*8)+"bit",
                 us: fmtUs, bytes: fmtBytes, int: fmtInt};
  t.querySelector("tbody").innerHTML = META.tin_fields.map(([key,lab,typ]) =>
    `<tr><td class="k">${lab}</td>` + tins.map(tin =>
      `<td>${fmtOf[typ](tin[key])}</td>`).join("") + "</tr>").join("");
}

// ---------------------------------------------------------------- config form
function readCfg() {
  const oh_mode = $("ohseg").querySelector("button.on").dataset.v;
  const cfg = {
    bandwidth: $("bandwidth").value.trim() || "50mbit",
    unlimited: $("unlimited").checked,
    autorate: $("autorate").checked,
    diffserv: $("diffserv").value,
    flowmode: $("flowmode").value,
    nat: $("nat").checked,
    wash: $("wash").checked,
    ackfilter: $("ackfilter").value,
    split_gso: $("split_gso").checked,
    rtt_us: Math.max(1, Math.round(parseFloat($("rtt_ms").value || "100") * 1000)),
    oh_mode, oh_keyword: $("oh_keyword").value,
    overhead: parseInt($("overhead").value || "0", 10),
    mpu: parseInt($("mpu").value || "0", 10),
    atm: $("atm").value,
    ether_vlan: $("ether_vlan").checked,
    ingress: $("ingress").checked,
  };
  const ml = $("memlimit").value.trim(); if (ml) cfg.memlimit = ml;
  const fm = $("fwmark").value.trim(); if (fm) cfg.fwmark = fm;
  return cfg;
}

function loadFromDevice() {
  const s = LATEST[DEV];
  if (!s || !s.present || s.kind !== "cake") {
    $("applyerr").textContent = "no cake qdisc on " + DEV + " to read"; return;
  }
  $("applyerr").textContent = "";
  const o = s.opts;
  $("unlimited").checked = o.bandwidth === "unlimited";
  if (o.bandwidth !== "unlimited") $("bandwidth").value = Math.round(o.bandwidth*8/1000) + "kbit";
  $("rtt_ms").value = (o.rtt/1000);
  $("ackfilter").value = {enabled:"ack-filter", aggressive:"ack-filter-aggressive"}[o["ack-filter"]] || "no-ack-filter";
  $("flowmode").value = o.flowmode;
  $("diffserv").value = o.diffserv;
  $("nat").checked = !!o.nat;
  $("wash").checked = !!o.wash;
  $("ingress").checked = !!o.ingress;
  $("split_gso").checked = o.split_gso !== false;
  setOhMode("manual");
  $("overhead").value = o.raw ? 0 : (o.overhead || 0);
  $("mpu").value = o.mpu || 0;
  $("atm").value = o.atm || "noatm";
  $("ether_vlan").checked = false;
  $("memlimit").value = "";
  preview();
}

function setOhMode(v) {
  for (const b of $("ohseg").querySelectorAll("button")) b.classList.toggle("on", b.dataset.v === v);
  const kw = v === "keyword";
  $("l_ohkw").style.display = kw ? "" : "none";
  for (const id of ["l_oh","l_mpu","l_atm"]) $(id).style.display = kw ? "none" : "";
  preview();
}

let previewTimer = null;
function preview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    // the server builds the command, so what is shown is literally what runs
    const r = await post("/api/preview", {dev: DEV, cfg: readCfg(), mode: "apply"});
    $("preview").textContent = (r.commands || []).join("\n");
  }, 120);
}

async function post(url, body) {
  const r = await fetch(url, {method: "POST", headers: {"Content-Type":"application/json"},
                             body: JSON.stringify(body || {})});
  return r.json();
}

// ---------------------------------------------------------------- ifb
function renderIfb(st) {
  if (!st) return;
  const bit = (ok,label) => `<span class="${ok?'on':''}"><b style="color:${ok?'var(--ok)':'var(--bad)'}">${ok?'✓':'✗'}</b> ${label}</span>`;
  $("ifbstate").innerHTML =
    bit(st.exists, st.dev + " exists") + bit(st.up, st.dev + " up") +
    bit(st.ingress_qdisc, "ingress qdisc on " + META.wan) +
    bit(st.redirect, "matchall → mirred redirect") +
    `<span class="${st.ready?'on':''}">status <b style="color:${st.ready?'var(--ok)':'var(--warn)'}">${st.ready?'shaping downloads':'not wired up'}</b></span>`;
}

// ---------------------------------------------------------------- captures
function renderCaptures(rows) {
  const t = $("captable");
  if (!rows.length) {
    t.querySelector("thead").innerHTML = "";
    t.querySelector("tbody").innerHTML = `<tr><td class="k">no captures yet</td></tr>`;
    return;
  }
  const cols = ["when","label","secs","dev","config","avg","peak","avg backlog",
                "peak backlog","avg drain","peak drain","pk_delay peak","drops",
                "marks","ack drops","overlimits"];
  t.querySelector("thead").innerHTML = "<tr>" + cols.map(c => `<th>${c}</th>`).join("") + "</tr>";
  let html = "";
  for (const r of rows) {
    const when = new Date(r.t0*1000).toLocaleTimeString();
    for (const [dev,d] of Object.entries(r.devs)) {
      html += `<tr class="${d.changed?'warnrow':''}">
        <td>${when}</td><td>${esc(r.label)||'<span class="k">—</span>'}</td>
        <td>${r.secs}</td><td>${dev}</td>
        <td style="text-align:left">${d.changed?'⚠ ':''}${esc(d.cfg_end)}</td>
        <td>${fmtBits(d.avg_bps)}bit/s</td><td>${fmtBits(d.max_bps)}bit/s</td>
        <td>${fmtBytes(Math.round(d.avg_backlog))}</td><td>${fmtBytes(d.max_backlog)}</td>
        <td>${d.avg_drain_ms.toFixed(1)} ms</td><td>${d.max_drain_ms.toFixed(1)} ms</td>
        <td>${fmtUs(d.max_pk_us)}</td><td>${fmtInt(d.drops)}</td>
        <td>${fmtInt(d.marks)}</td><td>${fmtInt(d.ack_drops)}</td>
        <td>${fmtInt(d.overlimits)}</td></tr>`;
    }
  }
  t.querySelector("tbody").innerHTML = html;
}

// ---------------------------------------------------------------- boot
async function boot() {
  META = await (await fetch("/api/meta")).json();
  $("hostline").textContent = `${META.host} · wan ${META.wan} · ingress ${META.ifb} · ${META.poll_hz} Hz`;

  const opt = (v,d) => `<option value="${v}">${v}${d ? " — "+d : ""}</option>`;
  $("ackfilter").innerHTML = META.ack_filter.map(([v,d]) => opt(v,d)).join("");
  $("flowmode").innerHTML = META.flowmodes.map(([v,d]) => opt(v,d)).join("");
  $("diffserv").innerHTML = META.diffserv.map(([v,d]) => opt(v,d)).join("");
  $("oh_keyword").innerHTML = META.overhead_keywords.map(([v,d]) => opt(v,d)).join("");
  $("rttpreset").innerHTML = `<option value="">—</option>` +
    META.rtt_presets.map(([n,us]) => `<option value="${us}">${n} — ${us>=1000?(us/1000)+" ms":us+" µs"}</option>`).join("");
  $("diffserv").value = "diffserv3";
  $("flowmode").value = "triple-isolate";

  DEV = META.wan;
  $("devseg").innerHTML = [[META.wan,"upload / egress"],[META.ifb,"download / ingress"]]
    .map(([d,r]) => `<button data-dev="${d}" class="${d===DEV?'on':''}">${d} · ${r}</button>`).join("");
  $("devseg").onclick = e => {
    const b = e.target.closest("button"); if (!b) return;
    DEV = b.dataset.dev;
    for (const x of $("devseg").querySelectorAll("button")) x.classList.toggle("on", x === b);
    // ingress mode is what you almost always want on ifb0, and almost never on egress
    const down = DEV === META.ifb;
    $("ingress").checked = down;
    // dual-srchost is fair between the hosts *sending*, dual-dsthost between the
    // hosts *receiving* — which one is right flips with the direction. The change
    // is visible in the preview line below, not hidden.
    const fm = $("flowmode").value;
    if (down && fm === "dual-srchost") $("flowmode").value = "dual-dsthost";
    if (!down && fm === "dual-dsthost") $("flowmode").value = "dual-srchost";
    preview();
  };
  $("rawseg").innerHTML = [META.wan, META.ifb]
    .map((d,i) => `<button data-dev="${d}" class="${i?'':'on'}">${d}</button>`).join("");
  $("rawseg").onclick = e => {
    const b = e.target.closest("button"); if (!b) return;
    for (const x of $("rawseg").querySelectorAll("button")) x.classList.toggle("on", x === b);
    refreshRaw();
  };

  $("cards").innerHTML = cardHTML(META.wan, "upload · egress") +
                         cardHTML(META.ifb, "download · ingress");

  $("ohseg").onclick = e => { const b = e.target.closest("button"); if (b) setOhMode(b.dataset.v); };
  $("rttpreset").onchange = () => {
    if ($("rttpreset").value) $("rtt_ms").value = (+$("rttpreset").value)/1000;
    preview();
  };
  for (const id of ["bandwidth","unlimited","autorate","diffserv","flowmode","nat","wash",
                    "ackfilter","split_gso","rtt_ms","oh_keyword","overhead","mpu","atm",
                    "ether_vlan","memlimit","fwmark","ingress"]) {
    $(id).addEventListener("input", preview);
    $(id).addEventListener("change", preview);
  }

  $("apply").onclick   = () => doApply("apply");
  $("rebuild").onclick = () => doApply("rebuild");
  $("clear").onclick   = async () => {
    if (!confirm("Remove the root qdisc from " + DEV + "?")) return;
    const r = await post("/api/clear", {dev: DEV});
    $("applyerr").textContent = r.ok ? "" : (r.log || "failed");
  };
  $("loadcur").onclick = loadFromDevice;

  $("ifbinstall").onclick = async () => {
    $("ifblog").style.display = "block"; $("ifblog").textContent = "working…";
    const r = await post("/api/ifb/install", {cfg: readCfg()});
    $("ifblog").textContent = r.log; renderIfb(r.status);
  };
  $("ifbremove").onclick = async () => {
    if (!confirm("Tear down " + META.ifb + " and the redirect on " + META.wan + "?")) return;
    $("ifblog").style.display = "block"; $("ifblog").textContent = "working…";
    const r = await post("/api/ifb/remove", {});
    $("ifblog").textContent = r.log; renderIfb(r.status);
  };

  $("capbtn").onclick = async () => {
    if (!CAPTURING) { await post("/api/capture/start", {label: $("caplabel").value}); }
    else { await post("/api/capture/stop", {}); await refreshState(); }
    await refreshState();
  };
  $("capcsv").onclick  = () => location.href = "/api/captures.csv";
  $("capjson").onclick = () => location.href = "/api/captures.json";
  $("capclear").onclick = async () => { await post("/api/capture/clear", {}); refreshState(); };
  $("rawrefresh").onclick = refreshRaw;

  HIST = await (await fetch("/api/history")).json();
  await refreshState();
  preview();
  connect();
  setInterval(refreshState, 5000);      // ifb status + captures + log
  window.addEventListener("resize", () => { for (const d of Object.keys(LATEST)) renderCard(d, LATEST[d]); });
}

async function doApply(mode) {
  $("applyerr").textContent = "";
  const r = await post("/api/apply", {dev: DEV, cfg: readCfg(), mode});
  if (!r.ok) $("applyerr").textContent = r.error || r.log || "failed";
  else $("preview").textContent = r.command;
}

async function refreshState() {
  const st = await (await fetch("/api/state")).json();
  renderIfb(st.ifb);
  renderCaptures(st.captures || []);
  CAPTURING = st.capturing;
  $("capbtn").textContent = CAPTURING ? "Stop capture" : "Start capture";
  $("capbtn").className = CAPTURING ? "danger" : "";
  $("log").textContent = (st.log || []).map(l =>
    new Date(l.t*1000).toLocaleTimeString() + "  " + l.text).join("\n") || "—";
}

async function refreshRaw() {
  const dev = $("rawseg").querySelector("button.on").dataset.dev;
  const r = await (await fetch("/api/raw?dev=" + dev)).json();
  $("rawout").textContent = (r.text || "") + (r.classes ? "\n\n" + r.classes : "");
}

function connect() {
  const es = new EventSource("/api/stream");
  es.onopen = () => { $("conn").textContent = "live"; $("conn").className = "pill on"; };
  es.onerror = () => { $("conn").textContent = "reconnecting…"; $("conn").className = "pill off"; };
  es.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.type !== "tick") return;
    $("tinfo").textContent = new Date(msg.t*1000).toLocaleTimeString();
    for (const [dev,s] of Object.entries(msg.devs)) {
      LATEST[dev] = s;
      if (s.present) {
        (HIST[dev] = HIST[dev] || []).push([
          +s.t.toFixed(2), Math.round(s.bps), s.backlog, s.qlen,
          s.pk_delay_us||0, s.av_delay_us||0, s.sp_delay_us||0,
          +s.drops_ps.toFixed(2), +s.marks_ps.toFixed(2), +s.ackdrops_ps.toFixed(2),
          s.drain_ms == null ? 0 : +s.drain_ms.toFixed(2),
          s.sparse_flows, s.bulk_flows]);
        if (HIST[dev].length > MAXPTS) HIST[dev].splice(0, HIST[dev].length - MAXPTS);
      }
      renderCard(dev, s);
    }
  };
}
boot();
</script>
</body></html>
"""

PAGE_BYTES = PAGE.encode()


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8421)
    ap.add_argument("--wan", default="eth0", help="the shaped uplink (egress)")
    ap.add_argument("--ifb", default="ifb0", help="virtual device for ingress")
    args = ap.parse_args()

    eng = Engine(args.wan, args.ifb)
    Handler.engine = eng
    threading.Thread(target=eng.poll_loop, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"caketune on http://{args.host}:{args.port}  "
          f"(wan={args.wan} ingress={args.ifb}, polling at {POLL_HZ} Hz)")
    if os.geteuid() != 0:
        rc, _, _ = run(["sudo", "-n", "true"])
        if rc != 0:
            print("  warning: passwordless sudo is not available — "
                  "reading works, applying will fail")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        eng.stop.set()
        srv.server_close()


if __name__ == "__main__":
    main()
