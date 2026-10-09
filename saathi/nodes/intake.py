"""intake_planner: detect language, classify sector, extract company + facts, list missing facts, resolve the
official domain and plan the searches. The regulator route itself comes from playbooks.py, not the model."""
from __future__ import annotations

import re

from langgraph.types import Send

from saathi import llm
from saathi.nodes.common import live, node
from saathi.nodes.safety import merge_llm, trace_line
from saathi.playbooks import (CYBER_RE, ISSUE_QUERIES, SECTORS, TIERS, classify_rules, detect_language,
                               forum_query, missing_facts_for, tier_of)
from saathi.state import IntakeOut, schema
from saathi.tools.domains import resolve_official
from saathi.tools.serp import CreditLimitReached, NoApiKey, SerpApiError, serp

INTAKE_SYSTEM = (
    "You triage Indian grievances written in English, Hindi or Hinglish. Extract facts exactly as stated - "
    "never invent order IDs, dates or amounts (use '' if absent). Domains (sector): ecommerce (online shopping, food "
    "delivery, quick commerce), bank (banks, NBFCs, UPI/wallets, cards - service failures and disputed debits), "
    "telecom (mobile, broadband, DTH), insurance, loan_app (digital lending apps, recovery harassment), govt_scheme "
    "(public schemes, benefits, pensions, scholarships), public_service (municipal services, utilities, certificates, "
    "passports, government offices), police (a crime: cyber fraud or scams, theft, threats, stalking, assault, "
    "blackmail, or police refusing to register a complaint), legal (court cases and case status, property, family, "
    "tenancy and other civil disputes), other (travel, cabs, edtech, real estate…). For police, company is ''. For "
    "legal, company is the court or ''. Flag named schemes, policies, plans or products whose rules are "
    "not clear from the complaint in case_entities. Extract additional case-specific details into facts.details; "
    "never infer missing eligibility facts. Translate once: complaint_en is the whole complaint in plain English, "
    "and every facts field is written in English - the rest of the pipeline works from these, not the original. "
    "In follow_up_questions, ask (in the complainant's language) for up to 3 facts a strong letter needs that "
    "are missing - what they already tried, replies received, reference numbers. Safety: list in danger any "
    "IMMEDIATE risk to someone's life or safety the complaint describes; leave it empty otherwise."
)

BRANDS = ["Flipkart", "Amazon", "Myntra", "Meesho", "Ajio", "Nykaa", "Snapdeal", "Swiggy", "Zomato", "Blinkit",
          "Zepto", "BigBasket", "JioMart", "Tata CLiQ", "Jio", "Airtel", "Vodafone Idea", "Vi", "BSNL", "Tata Play",
          "HDFC Bank", "ICICI Bank", "SBI", "State Bank of India", "Axis Bank", "Kotak Mahindra Bank", "Yes Bank",
          "Punjab National Bank", "Bank of Baroda", "Canara Bank", "IDFC First Bank", "Bajaj Finance", "Paytm",
          "PhonePe", "Google Pay", "LIC", "Star Health", "HDFC ERGO", "ICICI Lombard", "Bajaj Allianz", "Niva Bupa",
          "Care Health", "KreditBee", "Moneyview", "Navi", "Fibe", "Kissht", "IndiGo", "Air India", "MakeMyTrip",
          "IRCTC", "Ola", "Uber", "Rapido", "Urban Company"]

# Rule-based fallback for spotting things worth researching (the LLM intake lists them itself): a few words before
# a marker noun ("… Super Star policy", "… Awas Yojana"), and hyphenated acronyms ("AB-XYZ", "PM-ABC").
ENTITY_MARKER_RE = re.compile(r"\b(scheme|yojana|abhiyan|mission|policy|plan)\b", re.I)
ACRONYM_RE = re.compile(r"\b[A-Z]{2,}(?:-[A-Z]{2,})+\b")
ENTITY_NOISE = {"my", "the", "a", "an", "this", "that", "for", "about", "under", "rejected", "denied",
                "claim", "payment", "benefit", "not", "received", "missing", "of", "in", "on", "to", "from"}


def case_entities(text: str) -> list[str]:
    found = list(dict.fromkeys(ACRONYM_RE.findall(text)))[:3]
    for match in ENTITY_MARKER_RE.finditer(text):
        start = max(text.rfind(mark, 0, match.start()) for mark in (".", "!", "?", ";", "\n")) + 1
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9&/'-]*", text[start:match.start()])[-4:]
        while words and words[0].casefold() in ENTITY_NOISE:
            words.pop(0)
        if words:
            entity = " ".join(words + [match.group(1)])
            if entity.casefold() not in {item.casefold() for item in found}:
                found.append(entity)
        if len(found) == 3:
            break
    return found


def rule_intake(text: str, company: str | None) -> dict:
    sector, issues = classify_rules(text + " " + (company or ""))
    if not company:
        hits = [b for b in BRANDS if re.search(rf"\b{re.escape(b)}\b", text, re.I)]
        company = max(hits, key=len) if hits else ""
    amt = re.search(r"(?:₹|rs\.?|inr|rupees?)\s?([\d,]+)", text, re.I)
    oid = re.search(r"\border\s*(?:id|no\.?|number|#)?\s*[:#-]?\s*([A-Z]{1,4}\d[A-Z0-9-]{5,})", text, re.I)
    ref = re.search(r"\b(?:ref(?:erence)?|txn|transaction|utr|policy|claim|complaint)\s*(?:id|no\.?|number|#)?"
                    r"\s*[:#-]?\s*([A-Z0-9][A-Z0-9-]{5,})", text, re.I)
    date = re.search(r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
                     r"(?:\s+\d{4})?|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", text, re.I)
    ps = re.search(r"([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,2}) (?:police station|thana|PS)\b", text)
    kw = {"refund": "refund", "defective": "defective product", "unauthorised_charge": "unauthorised charge",
          "harassment": "harassment", "claim_rejection": "claim rejected"}
    entities = case_entities(text)
    return {"language": detect_language(text), "sector": sector, "company": company, "issue_types": issues,
            "case_entities": entities,
            "facts": {"product_or_service": "", "issue": re.sub(r"\s+", " ", text)[:200], "what_user_wants": "",
                      "order_id": oid.group(1) if oid else "", "reference_id": ref.group(1) if ref else "",
                      "amount": amt.group(1).replace(",", "") if amt else "", "date": date.group(0) if date else "",
                      "city": "", "place": "", "police_station": ps.group(1).strip() if ps else "", "details": {}},
            "urgent_fraud_or_harassment": "harassment" in issues or bool(re.search(r"fraud|scam|threat|धमकी", text,
                                                                                   re.I)),
            "search_keywords": kw.get(issues[0], "complaint"), "method": "rules",
            "complaint_en": text, "follow_up_questions": []}  # no LLM: no translation (RAG drops Devanagari)


@node("🧭 Intake")
def intake_planner(state: dict) -> dict:
    serp.new_budget()  # each complaint gets its own SERPAPI_MAX_CREDITS budget
    text, hint, city = state["complaint"], state.get("company_hint"), state.get("city")
    out = llm.json_call(INTAKE_SYSTEM, f"Complaint:\n{text}\n\nCompany (if given by user): {hint or ''}\n"
                                       f"City (if given): {city or ''}", schema(IntakeOut))
    if out:
        try:
            out = IntakeOut.model_validate(out).model_dump() | {"method": llm.label()}
        except Exception:
            out = None
    out = out or rule_intake(text, hint)
    if hint:
        out["company"] = hint
    entities = list(dict.fromkeys(e.strip() for e in (out.get("case_entities") or []) + case_entities(text)
                                  if e.strip()))
    sector = out["sector"]  # may be re-framed by gap_check from the researched case brief
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
    return {"language": out["language"], "complaint_en": (out.get("complaint_en") or "").strip() or text,
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
