"""Route research: when no verified playbook fits a complaint, find the official route from sources.

The LLM is the brain, the code is the seatbelt:
  * the LLM reads the fetched pages and proposes the escalation steps (who, how, when) for this kind of complaint;
  * code keeps a step only if the page stating it is an OFFICIAL source (*.gov.in / *.nic.in or the company's own
    domain) that was actually fetched, and keeps its link only if that link is on an official host and appears in
    the fetched evidence. No source, no step; fewer than one surviving authority step, no route.
A route that passes is saved as a learned playbook (playbooks.register_playbook) with its sources, so the next
complaint of this kind is matched directly from intake's menu - a verified cache, not hardcoded knowledge.
"""
from __future__ import annotations

import re

from saathi import llm
from saathi.nodes.common import live
from saathi.playbooks import SECTORS, register_playbook
from saathi.rag.ingest import DOCS
from saathi.state import RouteResearch, schema
from saathi.tools.domains import NOT_OFFICIAL, host, registrable
from saathi.tools.fetch import fetch
from saathi.tools.serp import CreditLimitReached, NoApiKey, SerpApiError, serp

GOVT = (".gov.in", ".nic.in")
MAX_OFFICIAL, MAX_OTHER = 6, 2
ROUTE_SYSTEM = (
    "From the SOURCE pages only, work out the official grievance route in India for this kind of complaint: after "
    "the company's own grievance officer, which regulator, ombudsman, government portal or forum handles it, how to "
    "reach it, and when to go there. Every step must be stated by a source - put that source's URL in source_url; "
    "include a step's url only if that exact link appears in the sources. Prefer official government pages. Do not "
    "add steps from general knowledge. Choose law_docs only from INDEXED LAW, and only if they apply."
)
COMPANY_STEP = {"name": "Company grievance officer", "channel": "email / grievance page", "url": None, "phone": None,
                "when_to_use": "First. Complain in writing to the company's grievance officer (verified contact "
                               "shown with its source) and keep the complaint number.",
                "typical_wait": "30 days", "source": "verified contact search (each contact shown with its source)"}


def official(url: str, company_domains: list[str]) -> bool:
    return host(url).endswith(GOVT) or (registrable(url) in company_domains)


def queries(state: dict) -> list[str]:
    company = state.get("company") or ""
    topic = (state.get("route") or {}).get("domain_free_text") or (state.get("facts") or {}).get("issue") or ""
    qs = []
    if company:
        qs.append(f"{company} grievance officer appellate authority escalation")
    qs.append(f"{topic} complaint grievance portal site:gov.in")
    qs.append(f"{topic} consumer complaint regulator India how to escalate")
    return qs


def gather(state: dict, company_domains: list[str]) -> list[dict]:
    off, other, seen = [], [], set()
    for q in queries(state):
        try:
            res = serp.search({"engine": "google", "q": q, "gl": "in", "hl": "en"}, live=live(state))
        except (NoApiKey, CreditLimitReached, SerpApiError):
            continue
        for hit in (res.get("organic_results") or [])[:5]:
            url = hit.get("link") or ""
            if not url.startswith("https://") or url in seen or registrable(url) in NOT_OFFICIAL:
                continue
            is_off = official(url, company_domains)
            bucket, cap = (off, MAX_OFFICIAL) if is_off else (other, MAX_OTHER)
            if len(bucket) >= cap:
                continue
            seen.add(url)
            page = fetch(url)
            text = (page.get("text") or "").strip() or hit.get("snippet") or ""
            if len(text) >= 80:
                bucket.append({"url": url, "title": page.get("title") or hit.get("title") or "",
                               "text": text[:6000], "official": is_off})
    return off + other


def checked_steps(steps: list[dict], evidence: list[dict], company_domains: list[str]) -> tuple[list[dict], list[str]]:
    """The seatbelt. Returns (steps that pass, reasons for each one dropped)."""
    by_url = {e["url"]: e for e in evidence}
    corpus = " ".join(e["text"] for e in evidence) + " " + " ".join(by_url)
    kept, dropped = [], []
    for s in steps:
        src = s.get("source_url") or ""
        if src not in by_url:
            dropped.append(f"{s.get('name')}: its source was not a page we fetched")
            continue
        if not by_url[src]["official"]:
            dropped.append(f"{s.get('name')}: stated only by a non-official page ({registrable(src)})")
            continue
        url = (s.get("url") or "").strip()
        if url:
            bare = re.sub(r"^https?://(www\.)?", "", url).rstrip("/").split("/")[0]
            if not official(url if url.startswith("http") else f"https://{url}", company_domains) or \
                    bare not in corpus:
                dropped.append(f"{s.get('name')}: link {url} is not an official link found in the sources")
                url = ""
        kept.append({"name": s["name"], "channel": s.get("channel") or "", "url": url or None, "phone": None,
                     "when_to_use": s.get("when_to_use") or "", "typical_wait": s.get("typical_wait") or "",
                     "source": src})
    return kept, dropped


def research_route(state: dict) -> dict:
    """{} when research is not possible or nothing passes the checks; else state updates for the new route."""
    route = state.get("route") or {}
    company_domains = [registrable(d) for d in [state.get("company_domain"), *(state.get("official_domains") or [])]
                       if d]
    evidence = gather(state, company_domains)
    if not any(e["official"] for e in evidence):
        return {"trace": ["🧭 Route research: no official source found — using the generic consumer route"]}
    law = "\n".join(f"- {k}: {d['title']}" for k, d in DOCS.items())
    sources = "\n\n".join(f"SOURCE: {e['url']} ({'official' if e['official'] else 'secondary'})\n{e['title']}\n"
                          f"{e['text']}" for e in evidence)
    out = llm.json_call(ROUTE_SYSTEM, f"COMPLAINT: {state.get('complaint_en') or state['complaint']}\n"
                                      f"KIND OF PROBLEM: {route.get('domain_free_text')}\n"
                                      f"COMPANY: {state.get('company') or ''}\n\nINDEXED LAW:\n{law}\n\n"
                                      f"SOURCES:\n{sources}", schema(RouteResearch))
    rr = llm.validated(RouteResearch, out).model_dump()
    steps, dropped = checked_steps(rr["steps"], evidence, company_domains)
    if not steps:
        return {"trace": ["🧭 Route research: no step passed the source checks — using the generic consumer route"]
                + [f"   ✗ {d}" for d in dropped[:5]]}
    consumer = rr["complaint_type"] == "consumer_service"
    law_docs = [k for k in rr["law_docs"] if k in DOCS] or (["cpa", "ccr"] if consumer else [])
    first = dict(COMPANY_STEP)
    if consumer:  # same requested reply window and consumer-commission step as the verified "other" playbook
        first["reply_within"] = SECTORS["other"]["escalation"][0]["reply_within"]
    escalation = [first] + steps + ([dict(SECTORS["other"]["escalation"][-1])] if consumer else [])
    key = register_playbook(rr["playbook_key"], {
        "label": rr["label"], "covers": rr["covers"], "regulator": rr["regulator"], "law_docs": law_docs,
        "basis_query": rr["covers"], "escalation": escalation, "complaint_type": rr["complaint_type"],
        "sources": sorted({s["source"] for s in steps})})
    return {"sector": key, "tier": "A", "route": {**route, "mode": "learned", "matched_playbook": key},
            "trace": [f"🧭 Route research: learned “{rr['label']}” → {' → '.join(s['name'] for s in escalation)} "
                      f"({len(steps)} step(s) from {len({s['source'] for s in steps})} official source(s); saved "
                      f"as playbook '{key}')"] + [f"   ✗ dropped {d}" for d in dropped[:5]]}
