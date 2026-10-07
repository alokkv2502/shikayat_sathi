"""intake_planner: detect language, classify sector, extract company + facts, list missing facts, resolve the
official domain and plan the searches. The regulator route itself comes from playbooks.py, not the model."""
from __future__ import annotations

import re

from langgraph.types import Send

from saathi import llm
from saathi.nodes.common import live, node
from saathi.playbooks import ISSUE_QUERIES, REQUIRED_FACTS, SECTORS, classify_rules, detect_language
from saathi.state import IntakeOut, schema
from saathi.tools.domains import resolve_official
from saathi.tools.serp import CreditLimitReached, NoApiKey, SerpApiError, serp

INTAKE_SYSTEM = (
    "You triage Indian consumer complaints written in English, Hindi or Hinglish. Extract facts exactly as stated - "
    "never invent order IDs, dates or amounts (use '' if absent). Sectors: ecommerce (online shopping, food "
    "delivery, quick commerce), bank (banks, NBFCs, UPI/wallets, cards), telecom (mobile, broadband, DTH), "
    "insurance, loan_app (digital lending apps, recovery harassment), other (travel, cabs, edtech, real estate…)."
)

BRANDS = ["Flipkart", "Amazon", "Myntra", "Meesho", "Ajio", "Nykaa", "Snapdeal", "Swiggy", "Zomato", "Blinkit",
          "Zepto", "BigBasket", "JioMart", "Tata CLiQ", "Jio", "Airtel", "Vodafone Idea", "Vi", "BSNL", "Tata Play",
          "HDFC Bank", "ICICI Bank", "SBI", "State Bank of India", "Axis Bank", "Kotak Mahindra Bank", "Yes Bank",
          "Punjab National Bank", "Bank of Baroda", "Canara Bank", "IDFC First Bank", "Bajaj Finance", "Paytm",
          "PhonePe", "Google Pay", "LIC", "Star Health", "HDFC ERGO", "ICICI Lombard", "Bajaj Allianz", "Niva Bupa",
          "Care Health", "KreditBee", "Moneyview", "Navi", "Fibe", "Kissht", "IndiGo", "Air India", "MakeMyTrip",
          "IRCTC", "Ola", "Uber", "Rapido", "Urban Company"]


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
    kw = {"refund": "refund", "defective": "defective product", "unauthorised_charge": "unauthorised charge",
          "harassment": "harassment", "claim_rejection": "claim rejected"}
    return {"language": detect_language(text), "sector": sector, "company": company, "issue_types": issues,
            "facts": {"product_or_service": "", "issue": re.sub(r"\s+", " ", text)[:200], "what_user_wants": "",
                      "order_id": oid.group(1) if oid else "", "reference_id": ref.group(1) if ref else "",
                      "amount": amt.group(1).replace(",", "") if amt else "", "date": date.group(0) if date else "",
                      "city": ""},
            "urgent_fraud_or_harassment": "harassment" in issues or bool(re.search(r"fraud|scam|threat|धमकी", text,
                                                                                   re.I)),
            "search_keywords": kw.get(issues[0], "complaint"), "method": "rules"}


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
    facts = out["facts"]
    if city:
        facts["city"] = city
    sector, company = out["sector"], out["company"]
    cfg = SECTORS[sector]
    issues = [i for i in out["issue_types"] if i in ISSUE_QUERIES] or ["other"]
    missing = [{"fact": f, "placeholder": ph} for f, ph in REQUIRED_FACTS[sector]
               if not (facts.get(f) or (f == "reference_id" and facts.get("order_id")))]
    trace = [f"🧭 Intake ({out.get('method')}): language={out['language']}, sector={sector}, "
             f"company={company or '?'}, issues={', '.join(issues)}"
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
    if company:
        plan += [{"worker": "grievance", "engine": "google", "q": f"site:{d} {t}"} for t in cfg["officer_terms"]
                 if d]
        plan += [{"worker": "grievance", "engine": "google", "q": f"{company} {cfg['officer_terms'][0]} email"}]
        plan += [{"worker": "precedents", "engine": "google_forums", "q": f"{company} {kw} complaint resolved"},
                 {"worker": "precedents", "engine": "google_news", "q": f"{company} {kw} consumer court"}]
    if city:
        plan.append({"worker": "local_forum", "engine": "google_maps",
                     "q": f"District Consumer Disputes Redressal Commission {city}"})
    if sector == "loan_app":
        plan.insert(0, {"worker": "registry", "engine": "LoanLens (RBI DLA directory)", "q": company})
    return {"language": out["language"], "sector": sector, "company": company, "facts": facts,
            "issue_types": issues, "missing_facts": missing, "search_plan": plan,
            "company_domain": d, "official_domains": official.get("domains") or [],
            "domain_basis": official.get("basis"),
            "urgent_note": cfg.get("urgent") if out.get("urgent_fraud_or_harassment") else None,
            "verify_attempts": 0, "grievance_attempts": 0, "draft_attempts": 0, "critic_notes": [],
            "search_keywords": kw, "trace": trace}


def fan_out(state: dict) -> list[Send]:
    """The planner decides which workers run in parallel."""
    workers = {p["worker"] for p in state.get("search_plan", [])}
    sends = [Send(w, state) for w in ("grievance", "precedents", "registry") if w in workers]
    sends.append(Send("local_forum", state))  # always runs (notes a skip without a city), so fan-in is never empty
    return sends
