"""A deterministic stand-in for the LLM, for tests only. Production has no rule-based fallback: without the LLM the
run stops (llm.AIUnavailable). Here every agent's prompt gets a predictable answer, so the graph's own logic -
routing, guards, verifier, critic, deadlines, interrupts - can be tested offline. Tests that need a specific answer
monkeypatch llm.json_call themselves and delegate everything else to `answer`."""
from __future__ import annotations

import ast
import re

from saathi import llm

BRANDS = ["Kartify", "IndiGo", "Flipkart", "Jio", "SBI", "Star Health", "LIC", "KreditBee", "EPFO", "Myntra"]
DOMAINS = [  # (playbook, pattern) - only for the fake's "understanding" of the test complaints
    ("police", r"police|\bfir\b|thana|cyber|scam|fraudster|\botp\b|thagi|stolen|theft|fake customer care"),
    ("legal", r"court|\bcnr\b|partition|ancestral|divorce|custody|tenant|landlord|hearing"),
    ("loan_app", r"loan app|recovery agent|lending app"),
    ("govt_scheme", r"yojana|scheme|pm-[a-z]+"),
    ("public_service", r"municipal|street light|ration card|electricity"),
    ("insurance", r"insurance|claim|policy|star health|lic\b"),
    ("telecom", r"\bjio\b|airtel|sim\b|recharge|broadband"),
    ("bank", r"bank|upi|debit|account|sbi"),
    ("ecommerce", r"order|refund|damaged|delivery|kartify|flipkart|myntra"),
]


def _between(prompt: str, start: str, end: str) -> str:
    m = re.search(re.escape(start) + r"(.*?)" + re.escape(end), prompt, re.S)
    return m.group(1).strip() if m else ""


def _literal(text: str, default):
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return default


def intake(prompt: str) -> dict:
    text = _between(prompt, "Complaint:\n", "\n\nCompany (if given by user):")
    hint = _between(prompt, "Company (if given by user):", "\n").strip()
    low = text.lower()
    sector = next((k for k, pat in DOMAINS if re.search(pat, low)), "other")
    company = hint or next((b for b in BRANDS if re.search(rf"\b{re.escape(b)}\b", text, re.I)), "")
    amt = re.search(r"(?:₹|rs\.?|inr|rupees?)\s?([\d,]+)", text, re.I)
    oid = re.search(r"\border\s*(?:id|no\.?|number|#)?\s*[:#-]?\s*([A-Z]{1,4}\d[A-Z0-9-]{5,})", text, re.I)
    date = re.search(r"\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s+\d{4})?", text, re.I)
    ps = re.search(r"([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,2}) (?:police station|thana|PS)\b", text)
    entities = list(dict.fromkeys(re.findall(r"\b[A-Z]{2,}(?:-[A-Z]{2,})+\b", text)))
    m = re.search(r"((?:[A-Z][a-z]+ ){1,4}(?:Yojana|Scheme))", text)
    if m:
        entities.append(m.group(1))
    issues = [i for i, pat in (("refund", r"refund|money back"), ("defective", r"damaged|broken|defect"),
                               ("cyber_fraud", r"otp|fraud|scam|thagi"), ("claim_rejection", r"claim .*reject"))
              if re.search(pat, low)] or ["other"]
    language = "hi" if re.search("[ऀ-ॿ]", text) else \
        "hinglish" if len(re.findall(r"\b(ne|ka|ki|ke|kar|diya|hai|nahi|mera|tha)\b", low)) >= 2 else "en"
    return {"language": language, "complaint_en": text, "domain_free_text": sector.replace("_", " "),
            "matched_playbook": sector, "match_confidence": 0.9, "route_reason": "fake", "company": company,
            "issue_types": issues, "urgent_fraud_or_harassment": bool(re.search(r"fraud|scam|threat", low)),
            "search_keywords": issues[0].replace("_", " "), "case_entities": entities[:3],
            "facts": {"issue": re.sub(r"\s+", " ", text)[:200], "amount": amt.group(1).replace(",", "") if amt else "",
                      "order_id": oid.group(1) if oid else "", "date": date.group(0) if date else "",
                      "police_station": ps.group(1) if ps else ""},
            "follow_up_questions": [], "danger": [], "legal_query": f"{' '.join(issues)} consumer complaint",
            "missing_facts": []}


def gap(prompt: str) -> dict:
    conditions = _literal(_between(prompt, "CASE CONDITIONS:\n", "\n\nCOMPLAINT:"), [])
    answers = _literal(_between(prompt, "COMPLAINANT'S ANSWERS:\n", "\n\nPLACEHOLDERS"), {}) or {}
    out = []
    for c in conditions:
        ans = str(answers.get(c, "")).strip() if isinstance(answers, dict) else ""
        if re.match(r"(?i)yes\b", ans):
            out.append({"condition": c, "status": "met", "evidence": f"You confirmed: {ans}", "question": ""})
        elif re.match(r"(?i)no\b", ans):
            out.append({"condition": c, "status": "not_met", "evidence": f"You said: {ans}", "question": ""})
        else:
            out.append({"condition": c, "status": "unknown", "evidence": "", "question": f"Does this apply: {c}"})
    return {"assessments": out, "other_questions": []}


def extract(prompt: str) -> dict:
    from saathi.tools.fetch import find_emails, find_phones
    company = _between(prompt, "Company:", "\n").strip()
    page = prompt.split("\n\n", 1)[-1]
    contacts = [{"organisation": company, "belongs_to_named_company": True, "role": "other", "email": e}
                for e in find_emails(page)]
    contacts += [{"organisation": company, "belongs_to_named_company": True, "role": "other", "phone": p}
                 for p in find_phones(page)]
    for c in contacts:
        v = c.get("email") or c.get("phone")
        i = page.lower().find(v.lower() if "@" in v else v[:6])
        ctx = page[max(0, i - 200): i + 50].lower() if i >= 0 else ""
        c["role"] = next((r for r, k in (("appellate_authority", "appellate"), ("principal_nodal_officer", "principal "
                          "nodal"), ("nodal_officer", "nodal"), ("grievance_officer", "grievance officer"),
                          ("grievance_cell", "grievance"), ("customer_care", "customer")) if k in ctx), "other")
        name = re.search(r"(?:Name\s*:\s*|M[rs]s?\.?\s+)([A-Z][a-z]+(?: [A-Z][a-z]+)?)", page[max(0, i - 200): i])
        c["name"] = name.group(1) if name else ""
    return {"contacts": contacts}


def draft(prompt: str) -> dict:
    complaint = _between(prompt, "COMPLAINT (consumer's words):\n", "\n\nFACTS:")
    facts = _literal(_between(prompt, "FACTS: ", "\nCOMPANY:"), {}) or {}
    brief = _literal(_between(prompt, "CASE BRIEF: ", "\nELIGIBILITY CHECKS:"), {}) or {}
    checks = _literal(_between(prompt, "ELIGIBILITY CHECKS: ", "\nGAP QUESTIONS:"), []) or []
    answers = _literal(_between(prompt, "COMPLAINANT'S ANSWERS TO REVIEW QUESTIONS: ", "\n"), {}) or {}
    placeholders = re.findall(r"write (\[[^\]]+\])", prompt)
    deadline = re.search(r"REPLY DEADLINE \(computed[^:]*\): (\d{1,2} \w+ \d{4})", prompt)
    tags = list(dict.fromkeys(re.findall(r"^\[(C\d+)\]", prompt, re.M)))
    lines = [f"Re: {brief.get('subject')}." if isinstance(brief, dict) and brief.get("subject") else "",
             f"What happened: {complaint}",
             "Details: " + "; ".join(str(v) for k, v in facts.items() if v and k not in ("details",)),
             " ".join(placeholders)]
    if checks:
        lines.append("Condition-by-condition review: " + " ".join(
            f"{c['condition']} - {c['status']}{(' (' + c['evidence'] + ')') if c.get('evidence') else ''}."
            for c in checks))
        if any(c["status"] == "not_met" for c in checks):
            lines.append("Please confirm your records and explain the decision.")
    shown = {c["condition"] for c in checks}  # answers to conditions are already in the review above
    extra = {k: v for k, v in answers.items() if k not in shown} if isinstance(answers, dict) else {}
    if extra:
        lines.append("My answers: " + " ".join(str(v) for v in extra.values()))
    if tags:
        lines.append("Supporting rules: " + " ".join(f"[{t}]" for t in tags[:2]))
    if "THIS IS A WRITTEN COMPLAINT TO THE POLICE" in prompt:
        lines.append("I request you to register an FIR and give me a free copy.")
    if "THIS IS NOT A LETTER" in prompt:
        lines.append("Free legal help: the District Legal Services Authority (DLSA, 15100). Not legal advice.")
    if deadline:
        lines.append(f"Please resolve this by {deadline.group(1)}.")
    return {"outline": ["facts", "request"], "subject": "Complaint regarding " + (facts.get("issue") or "my case")[:60],
            "body": "\n\n".join(x for x in lines if x), "remedy": "resolve", "reply_deadline_days": 0, "summary": "s"}


def answer(system: str, prompt: str, schema: dict, **_) -> dict:
    from saathi.nodes.critic import CRITIC_SYSTEM
    from saathi.nodes.drafter import DRAFT_SYSTEM
    from saathi.nodes.gap_check import GAP_SYSTEM
    from saathi.nodes.grievance import EXTRACT_SYSTEM
    from saathi.nodes.intake import INTAKE_SYSTEM
    from saathi.nodes.precedents import RELEVANCE_SYSTEM
    if system == INTAKE_SYSTEM:
        return intake(prompt)
    if system == GAP_SYSTEM:
        return gap(prompt)
    if system == EXTRACT_SYSTEM:
        return extract(prompt)
    if system == DRAFT_SYSTEM:
        return draft(prompt)
    if system == CRITIC_SYSTEM:
        return {"issues": []}
    from saathi.nodes.escalation import PLAN_SYSTEM
    if system == PLAN_SYSTEM:  # no route found by default: the seed / labelled general route is used
        return {"steps": [], "notes": ""}
    if system == RELEVANCE_SYSTEM:
        n = len(re.findall(r"^\[\d+\]", prompt, re.M))
        return {"results": [{"index": i, "relevant": True, "how_resolved": ""} for i in range(n)]}
    raise llm.AIUnavailable(f"fake LLM has no answer for: {system[:60]}")
