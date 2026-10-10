"""escalation_planner: the escalation ladder is PROPOSED by the LLM from evidence and VALIDATED by code.

Evidence: web search (the queries intake wrote in its single call, plus the company's own grievance page) and the
indexed law (RAG). The LLM turns that evidence into an ordered ladder where every step names its source. Code then:
  * drops a step whose source is neither an official page we fetched (*.gov.in, *.nic.in, the company's own domain)
    nor a clause we retrieved;
  * keeps a link only if it is on an official host and appears in the evidence;
  * keeps a timeline only if its cited source states it - otherwise "check the timeline in <source>";
  * orders the ladder: the body's own grievance cell, then the sector's statutory forum / regulator / ombudsman,
    then commissions and courts, and only then general portals;
  * sends state matters to the state's portal - CPGRAMS only for central-government bodies.
If no sector-specific step survives, a general route is returned and labelled as such. A verified ladder is cached by
sector + state (+ company), so the next similar complaint gets it without searching.
"""
from __future__ import annotations

import json
import re

from config import DATA_DIR
from saathi import llm
from saathi.nodes.common import live, node
from saathi.rag.store import retrieve
from saathi.rag.ingest import DOCS
from saathi.state import EscalationPlan, schema
from saathi.tools.domains import NOT_OFFICIAL, host, registrable
from saathi.tools.fetch import fetch
from saathi.tools.serp import CreditLimitReached, NoApiKey, SerpApiError, serp

LADDER_DIR = DATA_DIR / "ladders"
LADDER_DIR.mkdir(parents=True, exist_ok=True)
GOVT = (".gov.in", ".nic.in")
MAX_OFFICIAL, MAX_OTHER, MAX_CLAUSES = 6, 2, 4
# The body's own cell, then the sector's statutory forum / regulator, then general grievance channels (helplines,
# state / central portals), and litigation (commissions, courts) last.
LEVEL_RANK = {"company": 0, "sector_forum": 1, "sector_regulator_or_ombudsman": 2, "state_portal": 3,
              "central_portal": 3, "other": 3, "court_or_commission": 4}
CPGRAMS = re.compile(r"cpgrams|pgportal", re.I)
DURATION = re.compile(r"\b(?:\d+|one|two|three|four|five|six|seven|ten|fifteen|thirty|forty[- ]five|sixty|ninety)"
                      r"\s+(?:working\s+)?(?:days?|weeks?|months?|hours?|years?)\b", re.I)
STATE_SIDE = {"state_government", "state_utility_or_psu"}

PLAN_SYSTEM = (
    "Propose the escalation ladder for this Indian complaint, using ONLY the SOURCES and CLAUSES given. Every step "
    "must name its source in source_url_or_clause (a SOURCE url, or a clause tag like [C2]); a step you cannot "
    "source must be left out. Start with the body's own grievance cell if a source describes it, then the sector's "
    "statutory forum, regulator or ombudsman, then consumer commissions or courts if they apply, and general "
    "public-grievance portals last. CPGRAMS is only for central-government bodies; for state bodies and state "
    "utilities use the state's own grievance portal if a source names it. Give a timeline only if the cited source "
    "states it. Include a step's url only if that exact link appears in the sources."
)
COMPANY_STEP = {"name": "The organisation's own grievance cell", "level": "company", "channel": "email / grievance page",
                "url": None, "phone": None, "timeline": None, "source": "verified contact search (see contacts)",
                "when_to_use": "First. Complain in writing and keep the complaint / docket number."}
FALLBACK_NOTE = "We couldn't find a sector-specific route. This is the general government route."


def cache_key(route: dict, company: str) -> str:
    def slug(x):
        return re.sub(r"[^a-z0-9]+", "_", (x or "").lower()).strip("_")
    parts = [slug(route.get("route_key") or route.get("domain_free_text"))[:30] or "general",
             slug(route.get("state")) or "india"]
    if company:
        parts.append(slug(company)[:30])
    return ":".join(parts)


def cached(key: str) -> dict | None:
    path = LADDER_DIR / (key.replace(":", "__") + ".json")
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
    return None


def store(key: str, plan: dict) -> None:
    (LADDER_DIR / (key.replace(":", "__") + ".json")).write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                                                                   encoding="utf-8")


def official(url: str, company_domains: list[str]) -> bool:
    return host(url).endswith(GOVT) or registrable(url) in company_domains


def search_queries(state: dict) -> list[str]:
    route = state.get("route") or {}
    qs = list(route.get("queries") or [])
    company = state.get("company") or ""
    if company:
        qs.append(f"{company} grievance redressal officer escalation")
    if not route.get("queries"):
        topic = route.get("domain_free_text") or (state.get("facts") or {}).get("issue") or ""
        qs.append(f"{topic} grievance redressal forum ombudsman {route.get('state') or 'India'}")
    return list(dict.fromkeys(q for q in qs if q.strip()))[:5]


def gather(state: dict, company_domains: list[str]) -> list[dict]:
    off, other, seen = [], [], set()
    for q in search_queries(state):
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
            text = (page.get("text") or "").strip()
            if len(text) < 200:  # JavaScript-rendered pages (common on state sites): use Google's excerpt too
                text = f"{text} {hit.get('snippet') or ''}".strip()
            if len(text) >= 80:
                bucket.append({"url": url, "title": page.get("title") or hit.get("title") or "",
                               "text": text[:6000], "official": is_off})
    return off + other


def law_clauses(state: dict) -> list[dict]:
    route = state.get("route") or {}
    q = (f"{route.get('domain_free_text') or ''} consumer grievance redressal forum ombudsman appeal complaint "
         f"{(state.get('facts') or {}).get('issue') or ''}")
    hits = retrieve(q[:400], tuple(DOCS), MAX_CLAUSES)
    return [{**h, "tag": f"C{i}"} for i, h in enumerate(hits, 1)]


def _timeline_in(timeline: str, text: str) -> bool:
    t = (timeline or "").lower().strip()
    if not t:
        return False
    if t in text:
        return True
    nums = re.findall(r"\d+|one|two|three|four|five|six|seven|ten|fifteen|thirty|forty[- ]five|sixty|ninety", t)
    return bool(nums) and all(re.search(rf"\b{re.escape(n)}\b", text) for n in nums)


def vouched_domains(evidence: list[dict]) -> set[str]:
    """Domains that an OFFICIAL page we fetched links to or names (e.g. a ministry page pointing to its grievance
    portal on a non-gov.in domain) - official by corroboration, not by a hand-made list."""
    out = set()
    for e in evidence:
        if e["official"]:
            for d in re.findall(r"(?:https?://|www\.)([a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,})|"
                                r"\b([a-z0-9-]{3,}(?:\.[a-z0-9-]+)*\.(?:in|com|org|net|co\.in))\b", e["text"].lower()):
                d = d[0] or d[1]
                if d and not d.endswith(GOVT):
                    out.add(registrable(d))
    return out


def validate(plan: dict, evidence: list[dict], clauses: list[dict], company_domains: list[str],
             authority_type: str) -> tuple[list[dict], list[str], list[dict]]:
    """The seatbelt: (steps that pass, why each dropped / changed, steps dropped only for want of a source)."""
    vouched = vouched_domains(evidence)
    for e in evidence:  # a page on a domain an official page vouches for counts as official
        if not e["official"] and registrable(e["url"]) in vouched:
            e["official"] = True
    company_domains = list(company_domains) + sorted(vouched)
    unsourced: list[dict] = []
    by_url = {e["url"]: e for e in evidence}
    by_tag = {f"[{c['tag']}]": c for c in clauses} | {c["tag"]: c for c in clauses}
    corpus = " ".join(e["text"] for e in evidence).lower() + " " + " ".join(by_url).lower()
    kept, notes = [], []
    for s in sorted(plan.get("steps") or [], key=lambda x: x.get("order") or 0):
        src = (s.get("source_url_or_clause") or "").strip()
        name = s.get("authority") or "step"
        if src in by_tag:
            c = by_tag[src]
            source, source_text = f"{c['citation']}", c["text"].lower()
        elif src in by_url and by_url[src]["official"]:
            source, source_text = src, by_url[src]["text"].lower()
        elif (alt := next((e for e in evidence if e["official"] and names_authority(name, e["text"])), None)):
            # The LLM cited the wrong page, but an official page we already have names this authority: use that.
            source, source_text = alt["url"], alt["text"].lower()
            notes.append(f"re-sourced “{name}” to the official page {registrable(alt['url'])} that names it")
        else:
            why = "not a page we fetched or a retrieved clause" if src not in by_url else \
                f"stated only by a non-official page ({registrable(src)})"
            notes.append(f"dropped “{name}”: its source is {why}")
            unsourced.append(s)
            continue
        if authority_type in STATE_SIDE and CPGRAMS.search(f"{name} {s.get('channel')} {s.get('url')}"):
            notes.append(f"dropped “{name}”: CPGRAMS is for central-government bodies; this is a state matter")
            continue
        url = (s.get("url") or "").strip()
        if url.lower() in ("null", "none", "n/a", "not specified", "-"):
            url = ""
        if url:
            full = url if url.startswith("http") else f"https://{url}"
            bare = re.sub(r"^https?://(www\.)?", "", full).rstrip("/").split("/")[0].lower()
            if not official(full, company_domains) or bare not in corpus:
                notes.append(f"removed link {url} from “{name}”: not an official link found in the sources")
                url = ""
        timeline = s.get("timeline")
        if timeline and not _timeline_in(timeline, source_text):
            notes.append(f"replaced timeline “{timeline}” of “{name}”: its source does not state it")
            timeline = f"check the timeline in {source}"
        when = s.get("when_to_use") or ""
        for m in DURATION.finditer(when):  # durations in the description need a source too
            if not _timeline_in(m.group(0), source_text):
                notes.append(f"removed “{m.group(0)}” from “{name}”: its source does not state it")
                when = when.replace(m.group(0), "the time set out in the source")
        kept.append({"name": name, "level": s.get("level") or "other", "channel": s.get("channel") or "",
                     "url": url or None, "phone": None, "when_to_use": when,
                     "timeline": timeline, "typical_wait": timeline or "", "source": source})
    kept.sort(key=lambda x: LEVEL_RANK.get(x["level"], 6))  # stable: the LLM's order within a level is kept
    return kept, notes, unsourced


VERIFIABLE = {"sector_forum", "sector_regulator_or_ombudsman", "state_portal", "central_portal"}
NAME_NOISE = {"the", "and", "for", "office", "grievance", "grievances", "complaint", "complaints", "portal", "state",
              "india", "indian", "government", "redressal", "cell", "via", "public", "consumer", "consumers"}


def names_authority(name: str, text: str) -> bool:
    """Does an official page really name this authority? (most of its distinctive words, or its acronym)"""
    text = text.lower()
    acr = re.findall(r"\(([A-Za-z-]{2,})\)|([A-Z]{3,})", name)
    if any((a or b).lower() in text for a, b in acr):
        return True
    words = [w for w in re.findall(r"[a-z]{4,}", name.lower()) if w not in NAME_NOISE]
    return bool(words) and sum(w in text for w in words) >= max(1, round(0.6 * len(words)))


def verify_by_search(state: dict, unsourced: list[dict], company_domains: list[str], limit: int = 3) -> list[dict]:
    """The LLM named a sector authority but its only source was unofficial: look the authority up once and accept it
    only if an OFFICIAL page we fetch names it. Returns new evidence pages (the steps' sources are updated)."""
    found = []
    for s in [x for x in unsourced if x.get("level") in VERIFIABLE][:limit]:
        try:
            res = serp.search({"engine": "google", "q": f"{s['authority']} official", "gl": "in", "hl": "en"},
                              live=live(state))
        except (NoApiKey, CreditLimitReached, SerpApiError):
            continue
        for hit in (res.get("organic_results") or [])[:5]:
            url = hit.get("link") or ""
            if not url.startswith("https://") or not official(url, company_domains):
                continue
            text = ((fetch(url).get("text") or "").strip() + " " + (hit.get("snippet") or "")).strip()
            if names_authority(s["authority"], text):
                found.append({"url": url, "title": hit.get("title") or "", "text": text[:6000], "official": True})
                s["source_url_or_clause"] = url
                break
    return found


def general_route(state: dict) -> list[dict]:
    """Last resort, labelled honestly - only public channels whose purpose is general grievances."""
    at = (state.get("route") or {}).get("authority_type")
    if at == "central_government":
        return [dict(COMPANY_STEP), {"name": "CPGRAMS (central public grievance portal)", "level": "central_portal",
                                     "channel": "pgportal.gov.in", "url": "https://pgportal.gov.in", "phone": None,
                                     "when_to_use": "If the department does not resolve it.", "timeline": None,
                                     "typical_wait": "", "source": "general government route"}]
    if at in STATE_SIDE:
        st = (state.get("route") or {}).get("state") or "your state"
        return [dict(COMPANY_STEP), {"name": f"Public grievance portal / CM helpline of {st}", "level": "state_portal",
                                     "channel": "the state's official grievance portal", "url": None, "phone": None,
                                     "when_to_use": "If the office does not resolve it.", "timeline": None,
                                     "typical_wait": "", "source": "general government route"}]
    return [dict(COMPANY_STEP)]


def plan_escalation(state: dict) -> dict:
    """{steps, fallback, note, notes, sources, key, cached} - a validated ladder for this complaint."""
    route = state.get("route") or {}
    company = state.get("company") or ""
    key = cache_key(route, company)
    hit = cached(key)
    if hit:
        return {**hit, "cached": True, "key": key}
    company_domains = [registrable(d) for d in [state.get("company_domain"), *(state.get("official_domains") or [])]
                       if d]
    evidence = gather(state, company_domains)
    clauses = law_clauses(state)
    sources = "\n\n".join(f"SOURCE: {e['url']} ({'official' if e['official'] else 'secondary'})\n{e['title']}\n"
                          f"{e['text']}" for e in evidence)
    law = "\n\n".join(f"[{c['tag']}] {c['citation']} - {c['title']}\n{c['text'][:900]}" for c in clauses)
    prompt = (f"COMPLAINT: {state.get('complaint_en') or state.get('complaint')}\nKIND OF PROBLEM: "
              f"{route.get('domain_free_text')}\nAGAINST: {company or '?'} ({route.get('authority_type')})\n"
              f"STATE: {route.get('state') or '?'}\n\nCLAUSES:\n{law or 'none'}\n\nSOURCES:\n{sources or 'none'}")
    plan = llm.validated(EscalationPlan, llm.json_call(PLAN_SYSTEM, prompt, schema(EscalationPlan))).model_dump()
    at = route.get("authority_type") or "unknown"
    steps, notes, unsourced = validate(plan, evidence, clauses, company_domains, at)
    extra = verify_by_search(state, unsourced, company_domains + sorted(vouched_domains(evidence)))
    if extra:  # re-check the whole ladder with the verified pages added
        evidence = evidence + extra
        steps, notes, _ = validate(plan, evidence, clauses, company_domains, at)
        notes = notes + [f"verified “{s['authority']}” on {registrable(s['source_url_or_clause'])} by a direct "
                         f"search" for s in unsourced if s["source_url_or_clause"] in {e["url"] for e in extra}]
    if not any(s["level"] == "company" for s in steps):
        steps.insert(0, dict(COMPANY_STEP))
    sector_steps = [s for s in steps if s["level"] != "company"]
    result = {"steps": steps, "fallback": not sector_steps, "note": plan.get("notes") or "", "notes": notes,
              "sources": sorted({s["source"] for s in sector_steps}), "key": key, "cached": False,
              "evidence_pages": len(evidence), "official_pages": sum(e["official"] for e in evidence),
              "evidence": [{"url": e["url"], "official": e["official"], "chars": len(e["text"])} for e in evidence],
              "clauses": [c["citation"] for c in clauses]}
    if result["fallback"]:
        result["steps"] = general_route(state)
        result["note"] = FALLBACK_NOTE
    else:
        store(key, {k: result[k] for k in ("steps", "fallback", "note", "notes", "sources")})
    return result


GENERIC = {"other", "public_service"}  # placeholder routes, not hand-checked sector ladders
PLANNER_BUDGET_S = 25                  # past this, the seed (or the labelled general route) is used


def needs_planner(state: dict) -> bool:
    """Every tier-A complaint. Police and court tiers keep their fixed, safety-reviewed routes."""
    from saathi.playbooks import tier_of
    return tier_of(state.get("sector") or "other") == "A"


def seeded(sector: str) -> bool:
    """A hand-checked seed ladder exists for this sector (the planner may only ADD to it)."""
    from saathi.playbooks import SECTORS
    return sector in SECTORS and sector not in GENERIC


def _distinct_words(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{5,}", name.lower()) if w not in NAME_NOISE}


def merge_with_seed(seed: list[dict], planned: list[dict]) -> tuple[list[dict], list[str]]:
    """Seed ladder + the planner's validated sector steps the seed does not already have (never replacing or
    removing a seed step). New steps go before litigation (commissions / courts)."""
    seed = [dict(s) for s in seed]
    added = []
    for p in planned:
        if p.get("level") == "company":
            continue
        words = _distinct_words(p["name"])
        dup = any(words & _distinct_words(f"{s['name']} {s.get('channel') or ''}") for s in seed + added)
        if words and not dup:
            added.append({**p, "added_by_planner": True})
    if not added:
        return seed, []
    cut = next((i for i, s in enumerate(seed) if re.search(r"commission|court", s["name"], re.I)), len(seed))
    return seed[:cut] + added + seed[cut:], [s["name"] for s in added]


@node("🪜 Escalation planner")
def escalation_planner(state: dict) -> dict:
    """Runs alongside the search workers, within PLANNER_BUDGET_S. Proposes the ladder from evidence (LLM),
    validates it (code). How it is used (merged into a seed, or on its own) is decided in drafter.escalation_for."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    from saathi import timing
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(timing.carry(plan_escalation), state)
    try:
        res = fut.result(timeout=PLANNER_BUDGET_S)
    except FutureTimeout:  # a late result still lands in the ladder cache for the next complaint
        ex.shutdown(wait=False)
        return {"ladder": {"timed_out": True, "steps": [], "fallback": True},
                "trace": [f"🪜 Escalation planner: over {PLANNER_BUDGET_S}s — using the "
                          + ("verified seed ladder" if seeded(state.get("sector") or "") else "fallback route")]}
    ex.shutdown(wait=False)
    sector_steps = [s for s in res["steps"] if s["level"] != "company"]
    head = ("cached ladder" if res.get("cached") else
            f"{len(sector_steps)} sourced step(s) from {res.get('official_pages', 0)} official page(s) / "
            f"{len(res.get('clauses') or [])} clause(s)")
    return {"ladder": res,
            "trace": [f"🪜 Escalation planner ({head}): " + " → ".join(s["name"] for s in res["steps"])
                      + (f" — {res['note']}" if res.get("fallback") else "")]
            + [f"   ✗ {n}" for n in (res.get("notes") or [])[:4]]}
