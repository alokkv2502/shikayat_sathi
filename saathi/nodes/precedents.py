"""precedents worker: how did others get similar complaints resolved? (Google Forums + Google News)

The subject searched is the company - or, for a government scheme, the scheme itself (from case research), since
the "company" there is a ministry or the complainant's employer. A result must name the subject: the company's
first distinctive word, or two distinctive words (or an acronym) of the scheme's name."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, wait

from saathi import llm, timing
from saathi.nodes.common import live, node
from saathi.state import PrecedentsOut, schema
from saathi.tools.serp import CreditLimitReached, SerpApiError, serp

PRECEDENTS_BUDGET_S = 40
RELEVANCE_SYSTEM = (
    "You judge search results about Indian consumer and citizen complaints. A result is relevant only if it is about "
    "this subject (a company or a government scheme) and a similar problem. For relevant ones, say in a short "
    "phrase which channel resolved it (grievance officer, consumer helpline, consumer court, ombudsman, CPGRAMS, "
    "social media…) or '' if not stated."
)


# Words that don't identify anyone: a result containing only these says nothing about the subject.
GENERIC_NAME_WORDS = {"the", "and", "for", "india", "indian", "government", "govt", "centre", "center", "central",
                      "union", "state", "ministry", "department", "limited", "ltd", "pvt", "private", "company",
                      "scheme", "yojana", "pradhan", "mantri", "national", "plan", "policy", "programme", "program"}
SCHEME_SECTORS = {"govt_scheme"}
# Where a resolved case would be reported: consumer courts for companies, grievance redressal for the government.
VENUE = {"govt_scheme": "grievance redressal", "public_service": "grievance redressal"}


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _tokens(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(w) >= 3 and w not in GENERIC_NAME_WORDS]


def search_subject(state: dict) -> tuple[str, list[str], bool]:
    """(what to search for, the names a result must mention, is it a scheme)."""
    brief = state.get("case_brief") or {}
    if state.get("sector") in SCHEME_SECTORS:
        name = brief.get("subject") or next((e for e in state.get("case_entities") or [] if e), "")
        if name:
            return name, [name, *(brief.get("aliases") or [])], True
    company = (state.get("company") or "").strip()
    return company, ([company] if _tokens(company) else []), False


def mentions(text: str, names: list[str], scheme: bool) -> bool:
    flat = _compact(text)
    for name in names:
        toks = _tokens(name)
        if scheme and toks and sum(t in flat for t in toks) >= min(2, len(toks)):
            return True
        if not scheme and toks and toks[0] in flat:
            return True
    return False


@node("📰 Precedents")
def precedents(state: dict) -> dict:
    subject, names, scheme = search_subject(state)
    if not names:  # police / legal matters, or no nameable company: nothing specific to look for
        return {"precedents": [], "trace": ["📰 Precedents: skipped — no company or scheme to search for"]}
    kw = state.get("search_keywords") or "complaint"
    for n in [subject, *names, state.get("company") or ""]:
        if n:
            kw = re.sub(rf"\b{re.escape(n)}\b", "", kw, flags=re.I)
    kw = " ".join(kw.split()[:8]) or "complaint"
    items, errors = [], []
    searches = [("google_forums", f"{subject} {kw} complaint resolved"),
                ("google_news", f"{subject} {kw} {VENUE.get(state.get('sector'), 'consumer court')}")]
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
    items = [i for i in items if i["url"] and mentions(f"{i['title']} {i['snippet']}", names, scheme)]
    method = "nothing to judge"
    if items:
        listing = "\n".join(f"[{n}] {i['title']} — {i['snippet'][:250]}" for n, i in enumerate(items))
        res = llm.json_call(RELEVANCE_SYSTEM, f"Subject: {subject}\nProblem: {state['facts'].get('issue')}\n\n"
                                              f"{listing}", schema(PrecedentsOut))
        judged = {r.index: r for r in llm.validated(PrecedentsOut, res).results}
        items = [i | {"how_resolved": judged[n].how_resolved} for n, i in enumerate(items)
                 if n in judged and judged[n].relevant]
        method = llm.label()
    return {"precedents": items, "errors": errors,
            "trace": [f"📰 Precedents: {len(items)} relevant forum/news item(s) ({method})"
                      + (f"; {len(errors)} search(es) failed" if errors else "")]}
