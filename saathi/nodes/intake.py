"""intake_planner: ONE LLM call understands the complaint - language, English translation, company, facts, the kind
of problem, the matching verified playbook (or none), follow-up questions and any danger - then code resolves the
official domain and plans the searches. There is no rule-based stand-in: without the LLM the run stops."""
from __future__ import annotations

import re

from langgraph.types import Send

from saathi import llm
from saathi.nodes.common import live, node
from saathi.letter import default_language
from saathi.nodes.safety import emergencies_end, merge_llm, trace_line
from saathi.playbooks import SECTORS, TIERS, forum_query, merge_missing, playbook_menu, tier_of
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
    "are missing - what they already tried, replies received, reference numbers. Safety: assess the risk to anyone's "
    "life or safety (tier and flags); an emergency happening now ends the process at an emergency screen. For the "
    "escalation planner: give the Indian state, who the complaint is against (authority_type) and 2-4 web "
    "searches (route_queries) that would find the official grievance route - you are not deciding the route, "
    "only where to look. Also write legal_query (an English search for the law that applies) and legal_queries "
    "(3-6 searches, one per specific right, duty, deadline, liability rule or escalation route that could decide "
    "this case), and list the facts "
    "THIS letter still needs in missing_facts; mark cyber fraud / online scams with the issue type cyber_fraud. "
    "COMPANY AND PLACE - you decide them from the complaint (the user's Company / City fields are only hints; prefer "
    "them when given): company is the specific organisation's official name as on its own website, with its common "
    "short form in brackets if it has one - e.g. 'State Bank of India (SBI)', 'Jaipur Vidyut Vitran Nigam Limited "
    "(JVVNL)'; expand abbreviations, and use the city or state to identify a local utility, DISCOM, municipal body, "
    "development authority or university. Never a description ('electricity board', 'my builder', 'the bank'). If a "
    "complaint against a company or service provider names none and you cannot identify it, company is '' and set "
    "needs_clarification to ask which organisation. facts.city is the complainant's city or district (or where it "
    "happened) if stated or clear, in English; else ''."
)

MATCH_MIN = 0.6  # below this the playbook match is not trusted: research the route instead
INTAKE_THINKING = "medium"  # intake is on the critical path: every other step waits for it (see README · Speed)
WHICH_ORGANISATION = {
    "en": "Which company or organisation is this complaint against? Please give its name (and the city, if it is "
          "a local body).",
    "hinglish": "Yeh shikayat kis company ya sanstha ke khilaf hai? Uska naam batayein (local body ho to shehar bhi).",
    "hi": "यह शिकायत किस कंपनी या संस्था के खिलाफ है? कृपया उसका नाम बताएं (स्थानीय निकाय हो तो शहर भी)।",
}


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
                    "question": out.get("clarifying_question") if out.get("needs_clarification") else "",
                    # inputs for the escalation planner (same LLM call - no extra latency)
                    "route_key": out.get("route_key") or "", "state": (out.get("state") or "").strip(), "authority_type": out.get("authority_type") or "unknown",
                    "queries": [q.strip() for q in out.get("route_queries") or [] if q.strip()][:4]}
    return out


ELSEWHERE_REPORT = re.compile(r"cyber|portal|police|\bFIR\b|acknowledg|1930|thana", re.I)
LEARNED_WORDS = re.compile(r"learn|notic|came to know|found out|discover|aware|saw|statement|pata chal|पता|दिखा|देखा",
                           re.I)


def check_extracted_facts(facts: dict, text: str) -> None:
    """Code checks intake's reading of the facts that decide liability and deadlines (in place):
    - a complaint / reference number written next to a cyber-portal or police report is that report's
      acknowledgement, not the company's complaint number - it moves to details.cyber_ack;
    - a 'first learned of it' date is kept only if the complaint talks about learning of / noticing it - the
      model must not assume it is the date it happened."""
    for key in ("complaint_no", "reference_id"):
        v = str(facts.get(key) or "").strip()
        digits = re.sub(r"\D", "", v)
        if not digits:
            continue
        for sentence in re.split(r"(?<=[.!?।])\s+", text):
            if digits in re.sub(r"\D", "", sentence) and ELSEWHERE_REPORT.search(sentence):
                facts["details"].setdefault("cyber_ack", v)
                facts[key] = ""
                break
    if facts.get("first_learned_on") and not LEARNED_WORDS.search(text):
        facts["first_learned_on"] = ""


@node("🧭 Intake")
def intake_planner(state: dict) -> dict:
    serp.new_budget()  # each complaint gets its own SERPAPI_MAX_CREDITS budget
    text, hint, city = state["complaint"], state.get("company_hint"), state.get("city")
    out = llm.json_call(INTAKE_SYSTEM, f"Complaint:\n{text}\n\nCompany (if given by user): {hint or ''}\n"
                                       f"City (if given): {city or ''}\n\nPLAYBOOKS:\n{playbook_menu()}",
                        schema(IntakeOut), thinking=INTAKE_THINKING)
    out = route_of(llm.validated(IntakeOut, out).model_dump() | {"method": llm.label()})
    if hint:
        out["company"] = hint
    entities = list(dict.fromkeys(e.strip() for e in out.get("case_entities") or [] if e.strip()))
    route = out["route"]
    sector = out["sector"]  # a verified playbook, or provisional "other" until route research finds the real one
    facts = out["facts"]
    facts.setdefault("details", {})
    check_extracted_facts(facts, text)
    # The city: the user's field if given, else the one intake read from the complaint. Every later step
    # (local office search, case research, the letter) uses state["city"].
    city_source = state.get("city_source") or ("your details" if city else
                                               "the complaint" if (facts.get("city") or "").strip() else "")
    city = (city or facts.get("city") or "").strip() or None
    if city:
        facts["city"] = city
    tier = tier_of(sector)
    company = out["company"] if tier == "A" else ""  # B/C: no company to write to (the other party is not one)
    if tier == "A" and not company and sector != "govt_scheme" and not route["question"] \
            and not state.get("clarified"):
        # Without the organisation there is nothing to search for: ask once, before any search runs.
        route["question"] = WHICH_ORGANISATION.get(out["language"], WHICH_ORGANISATION["en"])
    cfg = SECTORS[sector]
    issues = out["issue_types"] or ["other"]  # validated against IssueT by the schema
    extra_missing = [m for m in out.get("missing_facts") or []]
    missing = merge_missing(sector, facts, extra_missing)
    trace = [f"🧭 Intake ({out.get('method')}): language={out['language']}, domain={sector} "
             f"(tier {tier}: {TIERS[tier].lower()}), "
             f"company={company or '?'}" + (" (from the complaint)" if company and not hint else "")
             + (f", city={city} (from {city_source})" if city else "") + f", issues={', '.join(issues)}"
             + (f"; to research: {', '.join(entities)}" if entities else "")
             + (f"; route: {route['mode']} ({route['confidence']:.1f})"
                + (f" — no playbook fits “{route['domain_free_text']}”" if route["mode"] == "research" else ""))
             + (f"; missing: {', '.join(m['fact'] for m in missing)}" if missing else "")]

    safety = merge_llm(state.get("safety") or {}, out.get("safety"), llm.label())  # same LLM call
    if safety != state.get("safety"):
        trace.insert(0, trace_line(safety))
    ends = emergencies_end({**state, "safety": safety})  # the LLM found an emergency now: no searches, no letter
    if ends:
        route["question"] = ""
    official = {"domain": None, "domains": [], "basis": None}
    if company and not ends:
        try:
            official = resolve_official(serp, company, live=live(state))
        except (NoApiKey, CreditLimitReached, SerpApiError) as e:  # intake must always produce a plan
            trace.append(f"⚠ Official-domain search failed: {e}")
        trace.append(f"🌐 Official domain: {official['domain'] or 'not resolved'}"
                     + (f" ({official['basis']})" if official.get("basis") else ""))
    d = official.get("domain")
    kw = out.get("search_keywords") or "complaint"
    plan = []
    if company and not ends:  # gap_check drops the grievance searches if the case turns out to be a government scheme
        terms = cfg["officer_terms"] or ["grievance officer"]
        plan += [{"worker": "grievance", "engine": "google", "q": f"site:{d} {t}"} for t in terms if d]
        plan += [{"worker": "grievance", "engine": "google", "q": f"{company} {terms[0]} email"}]
        plan += [{"worker": "precedents", "engine": "google_forums", "q": f"{company} {kw} complaint resolved"},
                 {"worker": "precedents", "engine": "google_news", "q": f"{company} {kw} consumer court"}]
    forum = forum_query(sector, city, "cyber_fraud" in issues) if city else None
    if forum:
        plan.append({"worker": "local_forum", "engine": "google_maps", "q": forum[0]})
    if sector == "loan_app":
        plan.insert(0, {"worker": "registry", "engine": "LoanLens (RBI DLA directory)", "q": company})
    urgent = out.get("urgent_fraud_or_harassment") or (sector == "police" and "cyber_fraud" in issues)
    return {"route": route, "language": out["language"], "letter_language": state.get("letter_language") or default_language(out["language"]),
            "complaint_en": (out.get("complaint_en") or "").strip() or text,
            "extra_questions": [q.strip() for q in out.get("follow_up_questions") or [] if q.strip()][:3],
            "legal_query": (out.get("legal_query") or "").strip(), "extra_missing": extra_missing,
            "legal_queries": [q.strip() for q in out.get("legal_queries") or [] if q.strip()][:6],
            "sector": sector, "tier": tier, "company": company, "facts": facts, "city": city, "city_source": city_source,
            "issue_types": issues, "case_entities": entities, "missing_facts": missing, "search_plan": plan,
            "company_domain": d, "official_domains": official.get("domains") or [],
            "domain_basis": official.get("basis"),
            "urgent_note": cfg.get("urgent") if urgent else None,
            "verify_attempts": 0, "grievance_attempts": 0, "draft_attempts": 0, "critic_notes": [],
            "search_keywords": kw, "trace": trace, "safety": safety}


def fan_out(state: dict) -> list[Send]:
    """The planner decides which workers run in parallel."""
    workers = {p["worker"] for p in state.get("search_plan", [])}
    sends = [Send(w, state) for w in ("grievance", "precedents", "registry") if w in workers]
    sends.append(Send("local_forum", state))  # always runs (notes a skip without a city), so fan-in is never empty
    sends.append(Send("gap_check", state))    # the eligibility / questions LLM call overlaps the searches
    from saathi.nodes.escalation import needs_planner
    if needs_planner(state):                   # ladder from evidence, alongside the searches (not before them)
        sends.append(Send("escalation_planner", state))
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
    if emergencies_end(state):
        return "emergency_response"
    return "clarify" if (state.get("route") or {}).get("question") and not state.get("clarified") else "case_research"
