"""precedents worker: how did other consumers get similar complaints resolved? (Google Forums + Google News)"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, wait

from saathi import llm, timing
from saathi.nodes.common import live, node
from saathi.state import PrecedentsOut, schema
from saathi.tools.serp import CreditLimitReached, SerpApiError, serp

PRECEDENTS_BUDGET_S = 8
RELEVANCE_SYSTEM = (
    "You judge search results about Indian consumer complaints. A result is relevant only if it is about this "
    "company and a similar problem. For relevant ones, say in a short phrase which channel resolved it (grievance "
    "officer, consumer helpline, consumer court, ombudsman, social media…) or '' if not stated."
)


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


@node("📰 Precedents")
def precedents(state: dict) -> dict:
    company = state["company"]
    kw = re.sub(rf"\b{re.escape(company)}\b", "", state.get("search_keywords") or "complaint", flags=re.I).strip()
    items, errors = [], []
    searches = [("google_forums", f"{company} {kw or 'complaint'} complaint resolved"),
                ("google_news", f"{company} {kw or 'complaint'} consumer court")]
    # Nice-to-have, so time-boxed: both searches run in parallel and anything slower than PRECEDENTS_BUDGET_S is
    # skipped (google_forums measured 10-40s). A late result still lands in the cache for the next complaint.
    ex = ThreadPoolExecutor(max_workers=len(searches))
    futures = {ex.submit(timing.carry(serp.search), {"engine": e, "q": q, "gl": "in", "hl": "en"},
                         live=live(state)): (e, q) for e, q in searches}
    done, late = wait(futures, timeout=PRECEDENTS_BUDGET_S)
    ex.shutdown(wait=False)
    errors += [f"precedents: {futures[f][1]}: skipped after {PRECEDENTS_BUDGET_S}s" for f in late]
    for fut in [f for f in futures if f in done]:
        engine, q = futures[fut]
        try:
            data = fut.result()
        except (SerpApiError, CreditLimitReached) as e:
            errors.append(f"precedents: {q}: {e}")
            continue
        rows = data.get("organic_results") if engine == "google_forums" else data.get("news_results")
        for r in (rows or [])[:6]:
            src = r.get("source")
            items.append({"kind": "forum" if engine == "google_forums" else "news", "title": r.get("title") or "",
                          "url": r.get("link"), "source": src.get("name") if isinstance(src, dict) else src,
                          "date": r.get("iso_date") or r.get("date") or r.get("displayed_meta"),
                          "snippet": r.get("snippet") or ""})
    first = _compact(company.split()[0]) if company.split() else ""
    items = [i for i in items if i["url"] and first and first in _compact(f"{i['title']} {i['snippet']}")]
    method = "rules"
    if items:
        listing = "\n".join(f"[{n}] {i['title']} — {i['snippet'][:250]}" for n, i in enumerate(items))
        res = llm.json_call(RELEVANCE_SYSTEM, f"Company: {company}\nProblem: {state['facts'].get('issue')}\n\n"
                                              f"{listing}", schema(PrecedentsOut))
        if res:
            try:
                judged = {r.index: r for r in PrecedentsOut.model_validate(res).results}
                items = [i | {"how_resolved": judged[n].how_resolved} for n, i in enumerate(items)
                         if n in judged and judged[n].relevant]
                method = llm.label()
            except Exception:
                pass
    return {"precedents": items, "errors": errors,
            "trace": [f"📰 Precedents: {len(items)} relevant forum/news item(s) ({method})"
                      + (f"; {len(errors)} search(es) failed" if errors else "")]}
