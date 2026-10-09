"""Latency instrumentation: every node, SerpApi call, page fetch, LLM call and RAG step records a span
{kind, label, node, ms, cached, start}. Spans are attributed to the node running on the current thread (parallel
workers run on separate threads). Off-thread cost is cheap: one perf_counter pair and a list append under a lock.

    from saathi import timing
    timing.reset(); ...run the graph...; print(timing.report())

Terminal log (logger "saathi.timing", stderr), controlled by SAATHI_TIMING:
    nodes    (default) one line per finished node with its serp / fetch / llm / rag split, and a summary table
             each time a run pauses for review or ends
    calls    also one line per external call (live or cached)
    off      nothing
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import defaultdict
from contextlib import contextmanager

_lock = threading.Lock()
_local = threading.local()
SPANS: list[dict] = []
T0 = [time.perf_counter()]
MAX_SPANS = 5000  # the app is long-running: keep only recent spans

LEVEL = os.getenv("SAATHI_TIMING", "nodes").lower()
log = logging.getLogger("saathi.timing")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d ⏱ %(message)s", "%H:%M:%S"))
    log.addHandler(_h)
    log.propagate = False  # don't double-print through Streamlit's / the root logger
log.setLevel(logging.DEBUG if LEVEL == "calls" else logging.INFO if LEVEL != "off" else logging.CRITICAL + 1)


def mark() -> int:
    """Position in SPANS - pass to log_run() to summarise only what happened after it."""
    with _lock:
        return len(SPANS)


def _split(spans: list[dict]) -> str:
    parts = []
    for kind in ("serp", "fetch", "llm", "rag"):
        xs = [s for s in spans if s["kind"] == kind]
        if xs:
            live = sum(not s.get("cached") for s in xs)
            parts.append(f"{kind} {len(xs)}×{sum(s['ms'] for s in xs) / 1000:.1f}s"
                         + (f" ({len(xs) - live} cached)" if live < len(xs) else ""))
    init = [s for s in spans if s["kind"] == "init"]  # nested inside the first rag call - not added on top
    if init:
        parts.append(f"incl. one-off setup {sum(s['ms'] for s in init) / 1000:.1f}s")
    return " · ".join(parts) or "no external calls"


def log_node(name: str, ms: int, since: int, note: str = "") -> None:
    """One line for a finished node: its time and the external calls it made (attributed by thread)."""
    if not log.isEnabledFor(logging.INFO):
        return
    with _lock:
        mine = [s for s in SPANS[since:] if s["node"] == name and s["kind"] != "node"]
    log.info(f"{name:<15} {ms / 1000:6.2f}s   {_split(mine)}{note}")


def log_run(since: int, label: str = "run") -> None:
    """Summary table of everything recorded since mark(): per node, then the slowest calls."""
    if not log.isEnabledFor(logging.INFO):
        return
    with _lock:
        spans = list(SPANS[since:])
    if spans:
        log.info(f"{label} breakdown\n" + report(spans))


def reset() -> None:
    with _lock:
        SPANS.clear()
    T0[0] = time.perf_counter()


def current_node() -> str:
    return getattr(_local, "node", "-")


def carry(fn):
    """Wrap fn so that, run on a pool thread, its spans are attributed to the node that submitted it."""
    node = current_node()

    def run(*a, **kw):
        prev = getattr(_local, "node", None)
        _local.node = node
        try:
            return fn(*a, **kw)
        finally:
            _local.node = prev
    return run


@contextmanager
def span(kind: str, label: str = "", *, node: str | None = None, **extra):
    """Time a block. kind: node | serp | fetch | llm | rag | other. Set extra['cached'] inside via the yielded dict."""
    rec = {"kind": kind, "label": label, "node": node or current_node(), **extra}
    prev = getattr(_local, "node", None)
    if kind == "node":
        _local.node = label
    start = time.perf_counter()
    try:
        yield rec
    finally:
        end = time.perf_counter()
        if kind == "node":
            _local.node = prev
        rec.update(ms=round((end - start) * 1000), start=round((start - T0[0]) * 1000))
        with _lock:
            SPANS.append(rec)
            if len(SPANS) > MAX_SPANS:
                del SPANS[: len(SPANS) - MAX_SPANS]
        if kind != "node" and log.isEnabledFor(logging.DEBUG):
            log.debug(f"  ↳ {rec['node']:<13} {kind:<5} {rec['ms'] / 1000:6.2f}s "
                      f"{'(cached) ' if rec.get('cached') else ''}{label[:100]}")


def report(spans: list[dict] | None = None) -> str:
    spans = list(SPANS if spans is None else spans)
    if not spans:
        return "(no spans recorded)"
    base = min(s["start"] for s in spans)
    wall = max(s["start"] + s["ms"] for s in spans) - base
    nodes = [s for s in spans if s["kind"] == "node"]
    calls = [s for s in spans if s["kind"] != "node"]
    lines = [f"Wall time: {wall / 1000:.1f}s", "", "Per node (in order; parallel workers overlap):",
             f"  {'node':<16}{'start':>8}{'time':>9}   {'serp':>11} {'fetch':>11} {'llm':>11} {'rag':>9}"]
    for n in sorted(nodes, key=lambda s: s["start"]):
        mine = [c for c in calls if c["node"] == n["label"] and n["start"] <= c["start"] <= n["start"] + n["ms"]]

        def agg(kind):
            xs = [c for c in mine if c["kind"] == kind]
            return f"{len(xs)}×{sum(c['ms'] for c in xs) / 1000:.1f}s" if xs else "-"
        lines.append(f"  {n['label']:<16}{(n['start'] - base) / 1000:>7.1f}s{n['ms'] / 1000:>8.1f}s   "
                     f"{agg('serp'):>11} {agg('fetch'):>11} {agg('llm'):>11} {agg('rag'):>9}")
    lines += ["", "External calls by kind (live = not served from cache):"]
    by = defaultdict(list)
    for c in calls:
        by[c["kind"]].append(c)
    for kind, xs in sorted(by.items(), key=lambda kv: -sum(c["ms"] for c in kv[1])):
        live = [c for c in xs if not c.get("cached")]
        lines.append(f"  {kind:<6} {len(xs):>3} calls ({len(live)} live)  total {sum(c['ms'] for c in xs) / 1000:6.1f}s"
                     f"  max {max(c['ms'] for c in xs) / 1000:5.1f}s")
    lines += ["", "Slowest 12 calls:"]
    for c in sorted(calls, key=lambda c: -c["ms"])[:12]:
        lines.append(f"  {c['ms'] / 1000:6.1f}s  {c['kind']:<6} {c['node']:<15} {'(cached) ' if c.get('cached') else ''}"
                     f"{c['label'][:90]}")
    return "\n".join(lines)
