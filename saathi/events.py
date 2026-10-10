"""Live event log, one JSONL file per complaint (data/runs/<thread_id>.jsonl), read by the app's Live view.

Written by hooks that already see everything - the node wrapper (step started / finished / waiting / failed),
timing.span (every search, page fetch, LLM call and law retrieval) and graph.run (run started / paused / finished).
Appending a line takes well under a millisecond, so watching a run never slows it down; the Live view only reads.
The log holds the complaint text, so it stays local, like data/cases/."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from config import DATA_DIR

RUNS_DIR = DATA_DIR / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()


def path(thread_id: str) -> Path:
    return RUNS_DIR / f"{thread_id}.jsonl"


def emit(thread_id: str | None, kind: str, **data) -> None:
    """kind: run_start | run_paused | run_finished | run_failed | node_start | node_end | node_waiting |
    node_failed | call. Never raises - transparency must not break a complaint."""
    if not thread_id:
        return
    try:
        line = json.dumps({"t": round(time.time(), 3), "kind": kind, **data}, ensure_ascii=False, default=str)
        with _lock, path(thread_id).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def preview(value, depth: int = 0, *, text: int = 400, items: int = 8):
    """A JSON-safe, size-limited copy of a node's output for the Live view: long text is cut, long lists show their
    first items and a count, deep nesting is summarised. The full state stays in the checkpoint."""
    if isinstance(value, str):
        return value if len(value) <= text else value[:text] + f"… (+{len(value) - text} chars)"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 3:
        return f"<{type(value).__name__}, {len(value)} items>" if hasattr(value, "__len__") else str(value)[:80]
    if isinstance(value, dict):
        keys = list(value)
        out = {str(k): preview(value[k], depth + 1, text=text, items=items) for k in keys[:20]}
        if len(keys) > 20:
            out["…"] = f"+{len(keys) - 20} more keys"
        return out
    if isinstance(value, (list, tuple, set)):
        xs = list(value)
        out = [preview(x, depth + 1, text=text, items=items) for x in xs[:items]]
        return out + ([f"… +{len(xs) - items} more"] if len(xs) > items else [])
    return preview(str(value), depth, text=text, items=items)


def read(thread_id: str) -> list[dict]:
    p = path(thread_id)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue  # a line being written right now
    return out


# --------------------------------------------------------------------------- reading a run, for the Live view

CALL_ICON = {"serp": "🔎", "fetch": "📄", "llm": "🤖", "rag": "📚", "init": "⚙️"}
CALL_NAME = {"serp": "web search", "fetch": "read page", "llm": "AI call", "rag": "law lookup", "init": "setup"}
STAGE_NAME = {"clarify": "one quick question", "details": "the details form", "review": "your review of the letter"}


def summarize(evts: list[dict], now: float | None = None) -> dict:
    """Fold the event log into the Live view's picture: run status, each node's state, counters, active time.
    status: starting | running | paused | finished | failed. Node state: running | done | warn | waiting | failed
    (a node that never ran is absent)."""
    now = now or time.time()
    nodes: dict[str, dict] = {}
    calls: dict[str, list[int]] = {}  # kind -> [total, cached]
    status, stage, error, active, started = "starting", "", "", 0.0, None
    for e in evts:
        k, t = e.get("kind", ""), e.get("t", now)
        if k == "run_start":
            status, started, stage, error = "running", t, "", ""
        elif k in ("run_paused", "run_finished", "run_failed"):
            status = {"run_paused": "paused", "run_finished": "finished", "run_failed": "failed"}[k]
            stage, error = e.get("stage") or "", e.get("error") or ""
            if started is not None:
                active, started = active + t - started, None
        elif k.startswith("node_") and e.get("node"):
            n = nodes.setdefault(e["node"], {"state": "running", "runs": 0, "ms": 0, "errors": [], "label": "",
                                             "since": t, "history": []})
            if k == "node_start" or not n["history"]:
                n["history"].append({"started": t, "trace": [], "errors": [], "output": None, "asked": None,
                                     "calls": [], "ms": None, "state": "running"})
            run = n["history"][-1]
            if k == "node_start":
                n.update(state="running", since=t, label=e.get("label") or n["label"])
                n["runs"] += 1
            elif k == "node_end":
                n["ms"] += e.get("ms") or 0
                n.update(state="warn" if e.get("errors") else "done", errors=e.get("errors") or [])
                run.update(trace=e.get("trace") or [], errors=e.get("errors") or [], output=e.get("output"),
                           ms=e.get("ms"), state=n["state"])
            elif k == "node_waiting":
                n["state"] = "waiting"
                run.update(asked=e.get("asked"), ms=e.get("ms"), state="waiting")
            elif k == "node_failed":
                n.update(state="failed", errors=[e.get("error") or "failed"])
                run.update(errors=n["errors"], ms=e.get("ms"), state="failed")
        elif k == "call":
            c = calls.setdefault(e.get("call") or "other", [0, 0])
            c[0] += 1
            c[1] += bool(e.get("cached"))
            owner = nodes.get(e.get("node") or "")
            if owner and owner["history"]:
                owner["history"][-1]["calls"].append({k2: e.get(k2) for k2 in ("call", "label", "ms", "cached")})
    if started is not None:
        active += now - started
    if status in ("failed", "finished", "paused"):  # nothing is still running once the run has stopped
        for n in nodes.values():
            if n["state"] == "running":
                n["state"] = "failed" if status == "failed" else "done"
                n["history"][-1]["state"] = n["state"]
    return {"status": status, "stage": stage, "error": error, "active_s": round(active, 1), "nodes": nodes,
            "calls": calls, "current": [k for k, v in nodes.items() if v["state"] in ("running", "waiting")],
            "started": evts[0]["t"] if evts else None}


def feed(evts: list[dict], limit: int = 250) -> list[tuple[float, str]]:
    """The activity feed: (seconds since the complaint started, line), oldest first."""
    if not evts:
        return []
    t0, out = evts[0].get("t", 0), []
    for e in evts:
        k, off = e.get("kind"), e.get("t", t0) - t0
        if k == "run_start":
            out.append((off, "▶ Continuing with your answers" if e.get("resumed") else "▶ Started"))
        elif k == "run_paused":
            out.append((off, f"⏸ Waiting for you: {STAGE_NAME.get(e.get('stage'), 'your input')}"))
        elif k == "run_finished":
            out.append((off, "✅ Finished"))
        elif k == "run_failed":
            out.append((off, f"✗ Stopped: {e.get('error') or 'error'}"))
        elif k == "node_start":
            out.append((off, f"→ {e.get('label') or e.get('node')}"))
        elif k == "node_end":
            out.append((off, f"✓ {e.get('node')} done in {(e.get('ms') or 0) / 1000:.1f}s"))
            out += [(off, f"   {line}") for line in e.get("trace") or []]
        elif k == "node_failed":
            out.append((off, f"✗ {e.get('node')}: {e.get('error') or 'failed'}"))
        elif k == "call":
            out.append((off, f"   {CALL_ICON.get(e.get('call'), '·')} {CALL_NAME.get(e.get('call'), e.get('call'))}"
                             f" · {(e.get('ms') or 0) / 1000:.1f}s{' (cached)' if e.get('cached') else ''}"
                             f" · {e.get('label') or ''}"))
    return out[-limit:]
