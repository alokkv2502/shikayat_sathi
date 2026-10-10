"""Node wrapper: a failing tool call (search, fetch) records the error and the graph carries on; an unavailable AI
stops the run (llm.AIUnavailable) - there is no rule-based stand-in. Live SerpApi calls are counted."""
from __future__ import annotations

import sys
import time
import traceback
from functools import wraps

from langgraph.errors import GraphBubbleUp

from saathi import llm, timing
from saathi.tools.serp import CreditLimitReached, NoApiKey, SerpApiError, serp


def node(label: str):
    def deco(fn):
        @wraps(fn)
        def wrapper(state, *args, **kwargs):
            serp.thread_calls_reset()
            since, t0 = timing.mark(), time.perf_counter()
            try:
                with timing.span("node", fn.__name__):
                    out = fn(state, *args, **kwargs) or {}
            except llm.AIUnavailable:  # no silent degraded path: the run stops and the UI says the AI is unavailable
                timing.log_node(fn.__name__, round((time.perf_counter() - t0) * 1000), since, " ✗ AI unavailable")
                raise
            except GraphBubbleUp:  # interrupt() / control flow must reach LangGraph
                timing.log_node(fn.__name__, round((time.perf_counter() - t0) * 1000), since,
                                " ⏸ waiting for review")
                raise
            except (NoApiKey, CreditLimitReached) as e:
                out = {"trace": [f"{label}: skipped — {e}"], "errors": [f"{fn.__name__}: {e}"]}
            except SerpApiError as e:
                out = {"trace": [f"⚠ {label}: search failed — {e}"], "errors": [f"{fn.__name__}: {e}"]}
            except Exception as e:  # never let one agent sink the whole complaint
                print(f"[{fn.__name__}] {type(e).__name__}: {e}", file=sys.stderr)
                traceback.print_exc(limit=4, file=sys.stderr)
                out = {"trace": [f"⚠ {label}: failed — {type(e).__name__}: {str(e)[:160]}"],
                       "errors": [f"{fn.__name__}: {type(e).__name__}: {e}"]}
            if isinstance(out, dict) and serp.thread_calls():
                out["live_calls"] = serp.thread_calls()
            timing.log_node(fn.__name__, round((time.perf_counter() - t0) * 1000), since,
                            " ⚠ " + "; ".join(out["errors"])[:120] if out.get("errors") else "")
            return out
        return wrapper
    return deco


def live(state) -> bool:
    return bool(state.get("live", True)) and not serp.offline
