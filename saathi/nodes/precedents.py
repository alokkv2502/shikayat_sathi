"""precedents worker: how did other consumers get similar complaints resolved? (Google Forums + Google News)"""
from __future__ import annotations

import re

from saathi import llm
from saathi.nodes.common import live, node
from saathi.state import PrecedentsOut, schema
from saathi.tools.serp import CreditLimitReached, SerpApiError, serp

RELEVANCE_SYSTEM = (
    "You judge search results about Indian consumer complaints. A result is relevant only if it is about this "
    "company and a similar problem. For relevant ones, say in a short phrase which channel resolved it (grievance "
    "officer, consumer helpline, consumer court, ombudsman, social media…) or '' if not stated."
)


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


@node("📰 Precedents")
def precedents(state: dict) -> dict:
    company, kw = state["company"], state.get("search_keywords") or "complaint"
    items, errors = [], []
    for engine, q in (("google_forums", f"{company} {kw} complaint resolved"),
                      ("google_news", f"{company} {kw} consumer court")):
        try:
            data = serp.search({"engine": engine, "q": q, "gl": "in", "hl": "en"}, live=live(state))
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
