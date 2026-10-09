"""Latency profile of one real complaint: time per node and per external call (SerpApi, page fetch, LLM, RAG).

    python eval/latency.py "Flipkart refused a refund ..." --company Flipkart --city Pune [--warm]

Runs in a fresh data directory (a copy of the legal index only), so every SerpApi search, page fetch and LLM call is
live - the cold path a new user sees. --warm then repeats the same complaint to show what the caches save.
Uses live SerpApi credits (capped by SERPAPI_MAX_CREDITS) and LLM calls. Writes eval/latency_<n>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("complaint")
    ap.add_argument("--company")
    ap.add_argument("--city")
    ap.add_argument("--warm", action="store_true", help="run a second time with the caches filled")
    ap.add_argument("--keep-cache", action="store_true", help="use the normal data dir (warm caches)")
    ap.add_argument("--data-dir", help="reuse a data dir (e.g. a previous run's temp dir)")
    a = ap.parse_args()
    if a.data_dir:
        os.environ["SAATHI_DATA_DIR"] = a.data_dir
    elif not a.keep_cache:  # must happen before config is imported
        tmp = Path(tempfile.mkdtemp(prefix="saathi-latency-"))
        if (ROOT / "data" / "qdrant").exists():
            shutil.copytree(ROOT / "data" / "qdrant", tmp / "qdrant", ignore=shutil.ignore_patterns(".lock"))
        os.environ["SAATHI_DATA_DIR"] = str(tmp)

    from langgraph.checkpoint.memory import InMemorySaver

    from saathi import graph as G
    from saathi import llm, timing
    from saathi.tools.serp import serp

    print(f"LLM: {llm.label()} · SerpApi key: {'yes' if serp.api_key else 'no'} · data: "
          f"{os.environ.get('SAATHI_DATA_DIR', 'default')}\n", file=sys.stderr)
    runs = []
    for label in ["cold", "warm"] if a.warm else ["cold"]:
        app = G.build(InMemorySaver())
        thread = G.new_thread()
        timing.reset()
        t0 = time.perf_counter()
        s = G.run(G.initial_state(a.complaint, company=a.company, city=a.city), thread, app=app)
        t_review = time.perf_counter() - t0          # until the first pause (details form, or the review)
        while "__interrupt__" in s:                   # submit the form empty, then approve the letter
            s = G.resume(thread, {}, app=app)
        total = time.perf_counter() - t0
        print(f"\n===== {label.upper()} RUN — {total:.1f}s total, {t_review:.1f}s until the first pause "
              f"(sector={s.get('sector')}, live SerpApi calls={s.get('live_calls', 0)}) =====")
        print(timing.report())
        print("\nOutcome: recommended=" + str((s.get("recommended") or {}).get("value")) + " · drafts="
              + str(s.get("draft_attempts")) + "\n" + "\n".join(x for x in s.get("trace") or []
                                                              if x.startswith(("🧐", "   •", "📰", "🛡️"))))
        runs.append({"run": label, "total_s": round(total, 2), "until_review_s": round(t_review, 2),
                     "sector": s.get("sector"), "live_serp_calls": s.get("live_calls", 0),
                     "spans": list(timing.SPANS)})
    n = len(list((ROOT / "eval").glob("latency_*.json"))) + 1
    out = ROOT / "eval" / f"latency_{n}.json"
    out.write_text(json.dumps({"complaint": a.complaint, "company": a.company, "city": a.city, "runs": runs},
                              indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
