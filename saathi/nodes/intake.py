"""intake_planner: ONE LLM call understands the complaint - language, English translation, company, facts, the kind
of problem, the matching verified playbook (or none), follow-up questions and any danger - then code resolves the
official domain and plans the searches. There is no rule-based stand-in: without the LLM the run stops."""
from __future__ import annotations

from langgraph.types import Send

from saathi import llm
from saathi.nodes.common import live, node
from saathi.nodes.safety import merge_llm, trace_line
from saathi.playbooks import (CYBER_RE, ISSUE_QUERIES, SECTORS, TIERS, forum_query, missing_facts_for, playbook_menu,
                               tier_of)
from saathi.state import IntakeOut, schema
from saathi.tools.domains import resolve_official
from saathi.tools.serp import CreditLimitReached, NoApiKey, SerpApiError, serp

INTAKE_SYSTEM = (
    "You triage Indian grievances written in English, Hindi or Hinglish. Extract facts exactly as stated - "
    "never invent order IDs, dates or amounts (use '' if absent). ROUTING: first describe the kind of problem in "
    "your own words (domain_free_text). Then pick matched_playbook from the PLAYBOOKS list ONLY if one genuinely "
    "covers this complaint; if none does, set it to null and match_confidence to 0 - never force the nearest one "
    "(an airline refund is not a government scheme). 'other' is only for consumer services with no closer fit. "
    "police = a crime (cyber fraud or scams, theft, threats, stalking, assault, blackmail) or police refusing to "
    "register a complaint; legal = court cases, property, family, tenancy and other civil disputes. For police, "
    "company is ''. For legal, company is the court or ''. Set needs_clarification only if you cannot tell what or "
    "whom the complaint is about. Flag named schemes, policies, plans or products whose rules are "
    "not clear from the complaint in case_entities. Extract additional case-specific details into facts.details; "
    "never infer missing eligibility facts. Translate once: complaint_en is the whole complaint in plain English, "
    "and every facts field is written in English - the rest of the pipeline works from these, not the original. "
    "In follow_up_questions, ask (in the complainant's language) for up to 3 facts a strong letter needs that "
    "are missing - what they already tried, replies received, reference numbers. Safety: list in danger any "
    "IMMEDIATE risk to someone's life or safety the complaint describes; leave it empty otherwise."
)

MATCH_MIN = 0.6  # below this the playbook match is not trusted: research the route instead


def route_of(out: dict) -> dict:
    """Code decides what to do with the LLM's routing answer: use a verified playbook only on a confident match to
    a key that exists; otherwise research the route (provisionally on the generic consumer playbook)."""
    key = out.get("matched_playbook")
    conf = float(out.get("match_confidence") or 0)
    matched = key in SECTORS and conf >= MATCH_MIN
    out["sector"] = key if matched else "other"
    out["route"] = {"domain_free_text": out.get("domain_free_text") or "", "matched_playbook": key if matched else None,
                    "confidence": conf, "reason": out.get("route_reason") or "",
                    "mode": "matched" if matched else "research",
                    "question": out.get("clarifying_question") if out.get("needs_clarification") else ""}
    return out


@node("🧭 Intake")
def intake_planner(state: dict) -> dict:
    serp.new_budget()  # each complaint gets its own SERPAPI_MAX_CREDITS budget
    text, hint, city = state["complaint"], state.get("company_hint"), state.get("city")
    out = llm.json_call(INTAKE_SYSTEM, f"Complaint:\n{text}\n\nCompany (if given by user): {hint or ''}\n"
                                       f"City (if given): {city or ''}\n\nPLAYBOOKS:\n{playbook_menu()}",
                        schema(IntakeOut))
    out = route_of(llm.validated(IntakeOut, out).model_dump() | {"method": llm.label()})
    if hint:
        out["company"] = hint
    entities = list(dict.fromkeys(e.strip() for e in out.get("case_entities") or [] if e.strip()))
    route = out["route"]
    sector = out["sector"]  # a verified playbook, or provisional "other" until route research finds the real one
    facts = out["facts"]
    facts.setdefault("details", {})
    if city:
        facts["city"] = city
    tier = tier_of(sector)
    company = out["company"] if tier == "A" else ""  # B/C: no company to write to (the other party is not one)
    cfg = SECTORS[sector]
    issues = [i for i in out["issue_types"] if i in ISSUE_QUERIES] or ["other"]
    missing = missing_facts_for(sector, facts)
    trace = [f"🧭 Intake ({out.get('method')}): language={out['language']}, domain={sector} "
             f"(tier {tier}: {TIERS[tier].lower()}), "
             f"company={company or '?'}, issues={', '.join(issues)}"
             + (f"; to research: {', '.join(entities)}" if entities else "")
             + (f"; route: {route['mode']} ({route['confidence']:.1f})"
                + (f" — no playbook fits “{route['domain_free_text']}”" if route["mode"] == "research" else ""))
             + (f"; missing: {', '.join(m['fact'] for m in missing)}" if missing else "")]

    official = {"domain": None, "domains": [], "basis": None}
    if company:
        try:
            official = resolve_official(serp, company, live=live(state))
        except (NoApiKey, CreditLimitReached, SerpApiError) as e:  # intake must always produce a plan
            trace.append(f"⚠ Official-domain search failed: {e}")
        trace.append(f"🌐 Official domain: {official['domain'] or 'not resolved'}"
                     + (f" ({official['basis']})" if official.get("basis") else ""))
    d = official.get("domain")
    kw = out.get("search_keywords") or "complaint"
    plan = []
    if company:  # gap_check drops the grievance searches if the case turns out to be a government scheme
        terms = cfg["officer_terms"] or ["grievance officer"]
        plan += [{"worker": "grievance", "engine": "google", "q": f"site:{d} {t}"} for t in terms if d]
        plan += [{"worker": "grievance", "engine": "google", "q": f"{company} {terms[0]} email"}]
        plan += [{"worker": "precedents", "engine": "google_forums", "q": f"{company} {kw} complaint resolved"},
                 {"worker": "precedents", "engine": "google_news", "q": f"{company} {kw} consumer court"}]
    forum = forum_query(sector, city, text) if city else None
    if forum:
        plan.append({"worker": "local_forum", "engine": "google_maps", "q": forum[0]})
    if sector == "loan_app":
        plan.insert(0, {"worker": "registry", "engine": "LoanLens (RBI DLA directory)", "q": company})
    urgent = out.get("urgent_fraud_or_harassment") or (sector == "police" and bool(CYBER_RE.search(text)))
    safety = merge_llm(state.get("safety") or {}, out.get("danger") or [], llm.label())  # same LLM call
    if safety:
        trace.insert(0, trace_line(safety))
    return {"route": route, "language": out["language"], "complaint_en": (out.get("complaint_en") or "").strip() or text,
            "extra_questions": [q.strip() for q in out.get("follow_up_questions") or [] if q.strip()][:3],
            "sector": sector, "tier": tier, "company": company, "facts": facts,
            "issue_types": issues, "case_entities": entities, "missing_facts": missing, "search_plan": plan,
            "company_domain": d, "official_domains": official.get("domains") or [],
            "domain_basis": official.get("basis"),
            "urgent_note": cfg.get("urgent") if urgent else None,
            "verify_attempts": 0, "grievance_attempts": 0, "draft_attempts": 0, "critic_notes": [],
            "search_keywords": kw, "trace": trace} | ({"safety": safety} if safety else {})


def fan_out(state: dict) -> list[Send]:
    """The planner decides which workers run in parallel."""
    workers = {p["worker"] for p in state.get("search_plan", [])}
    sends = [Send(w, state) for w in ("grievance", "precedents", "registry") if w in workers]
    sends.append(Send("local_forum", state))  # always runs (notes a skip without a city), so fan-in is never empty
    sends.append(Send("gap_check", state))    # the eligibility / questions LLM call overlaps the searches
    return sends


@node("❓ Clarify")
def clarify(state: dict) -> dict:
    """Very unclear complaints: ask ONE question (from intake's LLM), add the answer, and run intake again.
    Resume with Command(resume={"answer": "..."}). Asked at most once."""
    from langgraph.types import interrupt

    q = (state.get("route") or {}).get("question") or "What is the complaint about, and against which organisation?"
    edits = interrupt({"stage": "clarify", "question": q, "safety": state.get("safety") or {}}) or {}
    answer = (edits.get("answer") or "").strip() if isinstance(edits.get("answer"), str) else ""
    return {"complaint": state["complaint"] + (f"\n\n{answer}" if answer else ""), "clarified": True,
            "trace": [f"❓ Clarify: asked “{q}”" + (f" → “{answer[:80]}”" if answer else " → no answer")]}


def route_after_intake(state: dict) -> str:
    return "clarify" if (state.get("route") or {}).get("question") and not state.get("clarified") else "case_research"
