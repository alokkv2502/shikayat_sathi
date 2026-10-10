"""case_research: build a source-grounded CaseBrief for the schemes, policies and products named in a complaint.

Nothing about any particular scheme is coded here. For each named entity it searches the web (official hosts first:
*.gov.in / *.nic.in and the company's own resolved domains), fetches the top pages and has the LLM extract a
CaseBrief from that text only. The brief's grievance-channel URL is kept only if it appears in the fetched evidence,
and only source URLs that were actually fetched survive. Briefs are cached on disk under every name the entity goes
by (as written, official subject, aliases), so a second complaint about the same scheme costs no searches.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from config import CACHE_DIR
from saathi import llm
from saathi.nodes.common import live, node
from saathi.state import CaseBrief, schema
from saathi.tools.domains import NOT_OFFICIAL, host, registrable
from saathi.tools.fetch import fetch
from saathi.tools.serp import CreditLimitReached, NoApiKey, SerpApiError, serp

BRIEF_CACHE = CACHE_DIR / "case_research"
BRIEF_CACHE.mkdir(parents=True, exist_ok=True)
GOVT_SUFFIXES = (".gov.in", ".nic.in")
MAX_ENTITIES, MAX_OFFICIAL_PAGES, MAX_OTHER_PAGES = 2, 6, 2

RESEARCH_SYSTEM = (
    "You build a source-grounded case brief about a named scheme, policy, plan or product from Indian official "
    "sources. Use ONLY the SOURCE texts supplied. Every fact must be supported by at least one listed source URL; "
    "list those URLs in `sources`. Leave a field empty rather than guessing. Distinguish what the individual "
    "complainant receives from what an employer, seller or intermediary receives. Do not judge this complainant's "
    "eligibility. Write each eligibility or payment condition as one separate, checkable statement. Set "
    "complaint_type to govt_scheme for a public scheme or benefit run by a government department or agency; "
    "otherwise pick the matching service sector (consumer_service for shopping, travel and other consumer "
    "services). Include a URL in official_grievance_channel only if that URL appears in the sources."
)


def normalise(name: str) -> str:
    """Cache key for an entity name: 'AB-XYZ', 'ab xyz' and 'AB XYZ' share one brief."""
    return re.sub(r"[^0-9a-zऀ-ॿ]+", "", (name or "").casefold())


def _cache_file(name: str) -> Path:
    return BRIEF_CACHE / f"{hashlib.sha256(normalise(name).encode('utf-8')).hexdigest()[:24]}.json"


def cached_brief(entities: list[str]) -> dict | None:
    for name in entities:
        path = _cache_file(name)
        if normalise(name) and path.exists():
            try:
                return CaseBrief.model_validate_json(path.read_text(encoding="utf-8")).model_dump()
            except (ValueError, UnicodeDecodeError):
                continue
    return None


def store_brief(entities: list[str], brief: dict, where: str = "") -> None:
    text = json.dumps(brief, ensure_ascii=False, indent=1)
    for name in {*entities, *(f"{n}|{where}" for n in [brief.get("subject") or "", *(brief.get("aliases") or [])]
                              if n)}:
        if normalise(name):
            _cache_file(name).write_text(text, encoding="utf-8")


def is_official(url: str, company_domains: list[str]) -> bool:
    h = host(url)
    return h.endswith(GOVT_SUFFIXES) or registrable(url) in company_domains


def research_queries(entity: str, company_domains: list[str], state_name: str = "") -> list[str]:
    """Official hosts first, then the open web (secondary sources are used only to fill gaps). The complainant's
    state goes into the searches, so a state scheme is researched for THAT state, not another one."""
    where = f" {state_name}" if state_name else ""
    queries = [f'site:{d} "{entity}"' for d in company_domains[:1]]
    queries.append(f'"{entity}"{where} eligibility benefits grievance site:gov.in')
    queries.append(f'"{entity}"{where} eligibility conditions how to claim grievance')
    return queries


def research_sources(state: dict, entities: list[str]) -> list[dict]:
    company_domains = [d for d in [state.get("company_domain"), *(state.get("official_domains") or [])] if d]
    company_domains = list(dict.fromkeys(registrable(d) for d in company_domains))
    official: list[dict] = []
    other: list[dict] = []
    seen: set[str] = set()
    for entity in entities[:MAX_ENTITIES]:
        for query in research_queries(entity, company_domains, (state.get("route") or {}).get("state") or ""):
            if len(official) >= MAX_OFFICIAL_PAGES:
                break
            try:
                result = serp.search({"engine": "google", "q": query, "gl": "in", "hl": "en"}, live=live(state))
            except (NoApiKey, CreditLimitReached, SerpApiError):
                continue
            for hit in (result.get("organic_results") or [])[:5]:
                url = hit.get("link") or ""
                if not url.startswith("https://") or url in seen or registrable(url) in NOT_OFFICIAL:
                    continue
                is_off = is_official(url, company_domains)
                bucket, cap = (official, MAX_OFFICIAL_PAGES) if is_off else (other, MAX_OTHER_PAGES)
                if len(bucket) >= cap:
                    continue
                seen.add(url)
                page = fetch(url)
                text = (page.get("text") or "").strip() or hit.get("snippet") or ""
                if len(text) < 80:
                    continue
                bucket.append({"url": url, "title": page.get("title") or hit.get("title") or "",
                               "text": text[:7000], "official": is_off})
    # Secondary (non-official) pages only fill gaps when official ones are scarce.
    return official + (other if len(official) < 3 else [])


def grounded(brief: dict, evidence: list[dict]) -> dict:
    """Keep only fetched source URLs, and only a grievance URL that appears in the evidence."""
    fetched = {e["url"] for e in evidence}
    brief["sources"] = [u for u in dict.fromkeys(brief.get("sources") or []) if u in fetched]
    corpus = " ".join(e["text"] for e in evidence) + " " + " ".join(fetched)
    channel = brief.get("official_grievance_channel") or ""
    for url in re.findall(r"https?://[^\s)\]]+", channel):
        bare = re.sub(r"^https?://(www\.)?", "", url).rstrip("/.,;")
        if bare.split("/")[0] not in corpus:
            channel = channel.replace(url, "")
    channel = re.sub(r"\s+(?:or|and|,)\s*(?=$|[,.;])", "", re.sub(r"\s{2,}", " ", channel)).strip(" :-,;")
    brief["official_grievance_channel"] = channel
    return brief


@node("🔎 Case research")
def case_research(state: dict) -> dict:
    """Research the named scheme / policy / product. (When no verified playbook fits, the escalation route is
    planned by escalation_planner, alongside the search workers.)"""
    return entity_research(state)


def entity_research(state: dict) -> dict:
    entities = list(dict.fromkeys(x.strip() for x in state.get("case_entities") or [] if x and x.strip()))
    if not entities:
        return {"case_brief": {}, "trace": ["🔎 Case research: no named scheme, policy or product to research"]}

    where = (state.get("route") or {}).get("state") or ""  # state schemes differ by state
    brief = cached_brief([f"{e}|{where}" for e in entities])
    if brief:
        return {"case_brief": brief, "trace": [f"🔎 Case research: cached brief for {brief['subject']} "
                                               f"({len(brief['sources'])} source(s), no searches)"]}

    evidence = research_sources(state, entities)
    if not evidence:  # nothing is assumed about the entity without a source
        return {"case_brief": {}, "trace": [f"🔎 Case research: {entities[0]} — no sources found; nothing assumed"]}
    sources = "\n\n".join(f"SOURCE: {e['url']} ({'official' if e['official'] else 'secondary'})\n"
                          f"{e['title']}\n{e['text']}" for e in evidence)
    prompt = (f"ENTITIES NAMED IN THE COMPLAINT: {entities}\nCOMPANY / BODY NAMED: {state.get('company') or ''}\n"
              f"COMPLAINT: {state.get('complaint_en') or state['complaint']}\n\nSOURCES:\n{sources}")
    brief = grounded(llm.validated(CaseBrief, llm.json_call(RESEARCH_SYSTEM, prompt, schema(CaseBrief))).model_dump(),
                     evidence)
    if brief["sources"]:
        store_brief([f"{e}|{where}" for e in entities], brief, where)
        n_off = sum(e["official"] for e in evidence)
        return {"case_brief": brief,
                "trace": [f"🔎 Case research: {brief['subject']} — {len(brief['conditions'])} condition(s) from "
                          f"{len(brief['sources'])} source(s) ({n_off} official page(s) read)"]}
    return {"case_brief": {}, "trace": [f"🔎 Case research: {entities[0]} — the fetched pages did not support a "
                                        f"brief; nothing assumed"]}
