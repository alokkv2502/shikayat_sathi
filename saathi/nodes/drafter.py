"""drafter: the complaint email to the verified grievance contact, the escalation ladder (from playbooks.py)
and a plain-language summary in the user's language. Missing facts become [PLACEHOLDERS] - never invented.
On a critic loop it receives the critic's notes and must fix them."""
from __future__ import annotations

import re

from saathi import deadlines, llm
from saathi.nodes.common import node
from saathi.playbooks import CYBER_RE, SECTORS, tier_of
from saathi.state import DraftOut, schema

DRAFT_SYSTEM = (
    "You draft firm, respectful and case-specific grievances for Indian members or consumers. First plan: fill "
    "`outline` with the ordered points of THIS case's document and choose the correct complaint frame; then write "
    "the body from that outline, using only facts supported by the complaint, extracted facts, case brief, or "
    "reviewer answers. Never invent dates, eligibility, IDs, amounts, names, contacts or actions already taken. Do not reuse a generic "
    "letter template. Cite consumer-law clauses only through supplied [C#] tags and only when applicable. For a "
    "government-scheme case, name the scheme and relevant part, use its supplied official conditions and channel, "
    "and do not cite consumer-protection law or describe it as a deficiency in consumer service. If eligibility is "
    "unknown, ask the department to identify the exact condition or record blocking the benefit; do not claim the "
    "member is eligible. If a condition appears not met, do not demand the benefit: ask the office to confirm its "
    "records and explain the decision. Work the complainant's answers to the review questions into the letter as "
    "facts in their own words. If the complainant edited the previous draft, keep their changes unless an answer "
    "contradicts them. Keep the letter under 350 words, with no markdown, greeting or sign-off. The summary is "
    "in the consumer's language (en = English, hi = Hindi in Devanagari, hinglish = Hinglish)."
)
# What the document is, per tier (A: letter to the company / department - DRAFT_SYSTEM above as is).
TIER_RULES = {
    "B": ("THIS IS A WRITTEN COMPLAINT TO THE POLICE (Station House Officer). Set out the facts in time order: date, "
          "time and place, what happened, the people, phone numbers, accounts, UPI IDs, links or transaction IDs "
          "involved, the money lost, and the evidence the complainant has. Ask the SHO to register an FIR and give "
          "the complainant a free copy, and to investigate. For cyber fraud, quote the 1930 / cybercrime.gov.in "
          "acknowledgement if the complainant has one and ask that the money be traced and frozen. Do not cite "
          "section numbers, do not accuse anyone of more than the facts show, and do not threaten. No consumer-law "
          "frame."),
    "C": ("THIS IS NOT A LETTER. Write a short plain-language explainer addressed to the person ('you'): what kind "
          "of matter this generally is, what they can check or keep ready, how to look up their case status on "
          "eCourts with the 16-character CNR number if they already have a case, and that the District Legal "
          "Services Authority (DLSA, helpline 15100) gives free legal advice and a free lawyer to those who qualify. "
          "Give general information only: no legal advice, no prediction of the outcome, no section numbers, and "
          "never draft a petition, plaint, notice, affidavit or any court document. The subject is a short title "
          "for the explainer; remedy is 'Free legal aid and case-status check'; reply_deadline_days is 0."),
}
PLAN_TIER = {"B": "This is a police complaint (FIR request), not a consumer letter.",
             "C": "This is a plain-language explainer and referral for the person, not a letter or court document."}


def addressee(state: dict, esc: list[dict]) -> str:
    """Who the document is for: the police station for tier B (even when 1930 is the first step), the person
    themselves for tier C, otherwise the verified contact or the first escalation step."""
    tier = tier_of(state.get("sector") or "other")
    if tier == "B":
        station = (state.get("facts") or {}).get("police_station")
        name = SECTORS["police"]["escalation"][0]["name"]
        return name.replace("[POLICE STATION]", f"{station} Police Station") if station else name
    if tier == "C":
        return "the complainant (an explainer, not a letter)"
    rec = state.get("recommended")
    return ((rec or {}).get("name") or esc[0]["name"]) + (f" <{rec['value']}>" if rec else "")


def escalation_for(state: dict) -> list[dict]:
    steps = [dict(s) for s in SECTORS[state["sector"]]["escalation"]]
    brief = state.get("case_brief") or {}
    if state["sector"] == "police" and (CYBER_RE.search(state.get("complaint") or "")
                                        or "cyber_fraud" in (state.get("issue_types") or [])):
        steps.insert(0, dict(SECTORS["police"]["cyber_step"]))  # money lost online: 1930 before anything else
    if state.get("sector") in ("govt_scheme", "public_service") and brief.get("official_grievance_channel"):
        channel = brief["official_grievance_channel"]  # from the researched sources (URL checked against them)
        url = re.search(r"https?://[^\s)\]]+", channel)
        name = re.split(r"\s*(?::|\(|https?://| - | — )", channel, maxsplit=1)[0].strip(" ,.")
        body = brief.get("administering_body") or ""
        if name:
            steps[0]["name"] = name if not body or body.casefold() in name.casefold() else f"{name} ({body})"
        steps[0]["channel"] = channel
        if url:
            steps[0]["url"] = url.group(0).rstrip(".,;")
    rec = state.get("recommended")
    if rec and tier_of(state["sector"]) == "A":
        steps[0]["channel"] = f"{steps[0]['channel']} — {rec['value']}"
    station = (state.get("facts") or {}).get("police_station")
    if station:
        for s in steps:
            s["name"] = s["name"].replace("[POLICE STATION]", station)
    reg = state.get("registry") or {}
    if state["sector"] == "loan_app" and reg.get("status") in ("not_listed", "lookalike"):
        steps = [steps[2]] + steps[:2]  # unlisted / lookalike app: cyber-crime reporting comes first
        steps[0]["when_to_use"] = "Now: the app is not in RBI's Digital Lending App directory (or imitates a listed " \
                                  "app), so report it rather than writing to it. " + steps[0]["when_to_use"]
    deadlines.annotate(steps, incident=deadlines.to_date((state.get("facts") or {}).get("date") or ""))
    return steps


def reply_deadline(steps: list[dict]) -> dict:
    """The date the letter asks for a reply by - computed from the first step's rule, or {} if none applies."""
    s = steps[0] if steps else {}
    rw = s.get("reply_within")
    return {"date": s["reply_by"], "basis": rw["basis"], "source": rw.get("source", ""),
            "kind": rw.get("kind", "statutory")} if rw and s.get("reply_by") else {}


def answer_lines(state: dict) -> list[str]:
    """Review answers that are not already reflected in a condition check, as plain statements."""
    conditions = {c["condition"] for c in state.get("eligibility_checks") or []}
    extra = [(q, a) for q, a in (state.get("case_answers") or {}).items() if q not in conditions]
    return ["Further details about my case: " + " ".join(f"{q.rstrip('?')}? {a.rstrip('.')}." for q, a in extra)] \
        if extra else []


def _hi(state: dict) -> bool:
    return state.get("language") in ("hi", "hinglish")


def police_template(state: dict) -> dict:
    """Tier B fallback: a written complaint to the SHO asking for an FIR."""
    f = state.get("facts") or {}
    ph = {m["fact"]: m["placeholder"] for m in state.get("missing_facts") or []}
    cyber = CYBER_RE.search(state["complaint"]) or "cyber_fraud" in (state.get("issue_types") or [])
    when = f.get("date") or ph.get("date", "[DATE AND TIME OF INCIDENT]")
    where = f.get("place") or ph.get("place", "[PLACE OF INCIDENT]")
    station = f.get("police_station") or ph.get("police_station", "[POLICE STATION]")
    lines = [f"I wish to report {'a cyber fraud' if cyber else 'an offence'} and request that an FIR be registered. "
             f"I am submitting this complaint at {station} Police Station.",
             f"When and where: {when}, at {where}.",
             f"What happened: {state['complaint'].strip()}"]
    known = [f"amount lost: ₹{f['amount']}" if f.get("amount") else "",
             f"transaction / reference: {f['reference_id']}" if f.get("reference_id") else ""]
    known += [f"{k.replace('_', ' ')}: {v}" for k, v in (f.get("details") or {}).items() if v]
    if any(known):
        lines.append("Details: " + "; ".join(k for k in known if k) + ".")
    lines += answer_lines(state)
    if cyber:
        lines.append("My National Cyber Crime Helpline (1930 / cybercrime.gov.in) acknowledgement number, if any: "
                     "[1930 ACKNOWLEDGEMENT NUMBER, IF ANY].")
    lines.append("I request you to register an FIR on this complaint, give me a free copy of it, and investigate"
                 + (", including tracing and freezing the money" if cyber else "") + ". I will provide any evidence I "
                 "have, and I request a signed and stamped receipt for this complaint.")
    summary = ("यह शिकायत थाने के SHO के लिए है: FIR दर्ज करने की मांग। "
               + ("पैसे गए हैं तो अभी 1930 पर कॉल करें और बैंक को बताएं। " if cyber else "")
               + "थाना FIR न लिखे तो SP को डाक से लिखित शिकायत भेजें।" if _hi(state) else
               "This is a written complaint for the SHO asking for an FIR. "
               + ("If you lost money, call 1930 and your bank now. " if cyber else "")
               + "If the police won't register it, send it in writing to the SP/DCP.")
    return {"subject": f"Request to register an FIR — {'cyber fraud' if cyber else 'complaint'} on {when}",
            "body": "\n\n".join(lines), "remedy": "Register an FIR and investigate", "reply_deadline_days": 0,
            "summary": summary, "method": "rules"}


MATTER = [(r"divorce|talaq|custody|maintenance|alimony|dowry|तलाक", "a family matter (marriage, custody or "
           "maintenance), usually heard by a Family Court"),
          (r"property|land|plot|partition|inheritance|succession|ancestral|zameen|jameen|ज़मीन|जमीन", "a civil dispute "
           "about property, decided by a civil court and often settled through mediation"),
          (r"landlord|tenant|eviction|rent", "a tenancy dispute between a landlord and tenant"),
          (r"bail|chargesheet|summons|arrest|fir", "a criminal case, where the police and the court each have a role")]
CNR_RE = re.compile(r"\b[A-Z]{4}\d{12}\b")


def legal_explainer(state: dict) -> dict:
    """Tier C fallback: a plain-language explainer and referral. Never a court document."""
    text = state["complaint"]
    kind = next((label for pat, label in MATTER if re.search(pat, text, re.I)), "a matter for the courts")
    cnr = CNR_RE.search(text)
    elig = state.get("eligibility") or {}
    crit = (SECTORS["legal"].get("eligibility") or {}).get("conditions") or []
    lines = [f"What this is: From what you describe, this looks like {kind}. This is general information to help you "
             "take the next step, not legal advice.",
             "If you already have a case: check its status, next hearing date and orders on eCourts "
             "(services.ecourts.gov.in or the eCourts Services app) with the 16-character CNR number printed on your "
             "court papers, or with the court, case type, number and year."
             + (f" The number you gave, {cnr.group(0)}, looks like a CNR." if cnr else ""),
             "Free legal help: the District Legal Services Authority (DLSA) at your district court gives free legal "
             "advice and, if you qualify, a free lawyer. Any one of these is enough: "
             + "; ".join(re.sub(r"^(?:You are|You belong to)\s+", "", c.rstrip(".")).replace("Your ", "your ")
                         for c in crit)
             + ". Call the NALSA helpline 15100 or visit the DLSA office.",
             "Keep ready: copies of any court papers, notices, agreements or property documents, ID proof, and a "
             "short dated timeline of what happened.",
             "Many civil and family disputes can be settled faster through a Lok Adalat or mediation, which the DLSA "
             "can arrange."]
    if elig.get("status") in ("meets_listed", "may_not_qualify"):  # settled by the review answers
        lines.insert(3, elig["message"])
    lines += [a.replace("Further details about my case:", "What you told us:") for a in answer_lines(state)]
    summary = ("यह सामान्य जानकारी है, कानूनी सलाह नहीं। मुफ़्त वकील और सलाह के लिए ज़िला विधिक सेवा प्राधिकरण (DLSA) "
               "या 15100 पर संपर्क करें; केस की स्थिति eCourts पर CNR नंबर से देखें।" if _hi(state) else
               "General information, not legal advice: contact the District Legal Services Authority (15100) for free "
               "advice and a lawyer if you qualify, and check any case on eCourts with its CNR number.")
    return {"subject": "Your legal matter: what it means and where to get free help", "body": "\n\n".join(lines),
            "remedy": "Free legal aid and case-status check", "reply_deadline_days": 0, "summary": summary,
            "method": "rules"}


def template_draft(state: dict) -> dict:
    """Deterministic, case-aware fallback used when no LLM is configured."""
    f = state.get("facts") or {}
    brief = state.get("case_brief") or {}
    ph = {m["fact"]: m["placeholder"] for m in state.get("missing_facts") or []}
    clauses = state.get("clauses") or []
    nxt = escalation_for(state)[1]["name"] if len(escalation_for(state)) > 1 else "the appropriate forum"
    tier = tier_of(state["sector"])
    if tier == "B":
        return police_template(state)
    if tier == "C":
        return legal_explainer(state)
    if state.get("sector") == "govt_scheme":
        subject_name = brief.get("subject") or ", ".join(state.get("case_entities") or []) or "the scheme"
        body_name = brief.get("administering_body") or state.get("company") or "your office"
        detail_values = f.get("details") or {}
        known = [f"{key.replace('_', ' ')}: {value}" for key, value in detail_values.items() if value]
        known += [f"{label}: {f[key]}" for key, label in (("reference_id", "reference"), ("date", "date"),
                                                          ("amount", "amount (₹)")) if f.get(key)]
        checks = state.get("eligibility_checks") or []
        unknown = [item["condition"] for item in checks if item["status"] == "unknown"]
        lines = [f"I am writing about my benefit under {subject_name}, which I have not received."
                 + (f" What happened: {f['issue']}" if f.get("issue") else "")]
        if brief.get("entitlement"):
            lines.append(f"The scheme describes this benefit as: {brief['entitlement']}")
        if known:
            lines.append("The details I can confirm are: " + "; ".join(known) + ".")
        if checks:
            condition_lines = []
            for item in checks:
                condition = item["condition"]
                if item["status"] == "met":
                    condition_lines.append(f"I understand this condition is met: {condition} "
                                           + (f"My details: {item['evidence']}" if item.get("evidence") else ""))
                elif item["status"] == "not_met":
                    condition_lines.append(f"I understand this condition may not be met: {condition} "
                                           + (f"My details: {item['evidence']}" if item.get("evidence") else ""))
                else:
                    condition_lines.append(f"I cannot confirm this condition yet: {condition} "
                                           + (f"My details: {item['evidence']}" if item.get("evidence") else ""))
            lines.append("Condition-by-condition review:\n" + "\n".join(line.strip() for line in condition_lines))
        lines += answer_lines(state)
        not_met = [item["condition"].rstrip(".") for item in checks if item["status"] == "not_met"]
        if not_met:
            lines.append(f"Please confirm from the records held by {body_name} whether these conditions apply in my "
                         "case, and explain the decision on my benefit: " + "; ".join(not_met) + ".")
        if unknown:
            lines.append(f"Please check the unconfirmed conditions against the records held by {body_name} and tell "
                         "me which one is blocking payment: " + "; ".join(u.rstrip(".") for u in unknown) + ".")
        elif not checks:
            lines.append(f"Please assess my eligibility against the scheme conditions using the records held by "
                         f"{body_name} and identify any information still required.")
        if brief.get("common_failure_reasons"):
            lines.append("Please also check whether any of these known causes applies to my case: "
                         + "; ".join(r.rstrip(".") for r in brief["common_failure_reasons"]) + ".")
        lines.append("Please explain the specific correction or document needed"
                     + (f" (for example: {', '.join(brief['documents_needed'][:4])})"
                        if brief.get("documents_needed") else "")
                     + ". I request a response through the official grievance channel.")
        channel = escalation_for(state)[0]["name"]
        lines.append(f"If this remains unresolved, I will follow the scheme's grievance process through {nxt}.")
        summary = (f"This draft asks {channel} to check your scheme conditions and identify the record holding up "
                   "payment. Review the details and add anything you know before sending.")
        return {"subject": f"Request to review {subject_name} benefit status",
                "body": "\n\n".join(lines), "remedy": "Confirm eligibility and explain or correct the payment issue",
                "reply_deadline_days": 0, "summary": summary, "method": "rules"}

    ref = f.get("order_id") or f.get("reference_id") or ph.get("order_id") or ph.get("reference_id") or "[REFERENCE]"
    amount = f"₹{f['amount']}" if f.get("amount") else ph.get("amount", "")
    date = f.get("date") or ph.get("date", "")
    lines = [f"I am writing to formally complain about {f.get('product_or_service') or 'your product/service'} "
             f"(reference {ref}" + (f", dated {date}" if date else "") + (f", amount {amount}" if amount else "") + ")."]
    lines.append(f"What happened: {state['complaint'].strip()}")
    lines += answer_lines(state)
    if clauses:
        lines.append("This is contrary to the consumer-protection rules that apply to you, including: "
                     + "; ".join(f"{c['title']} [{c['tag']}]" for c in clauses[:3]) + ".")
    lines.append(f"I request: {f.get('what_user_wants') or '[REMEDY - e.g. full refund / replacement / reversal]'}.")
    dl = reply_deadline(escalation_for(state))
    by = dl.get("date") or "[REPLY-BY DATE]"
    if dl.get("kind") == "statutory":
        lines.append(f"As the rules require, {dl['basis']}. Please acknowledge this complaint and resolve it by {by}. "
                     f"If it is not resolved by then, I will escalate it to {nxt}.")
    else:
        lines.append(f"Please acknowledge this complaint and resolve it by {by}. If it is not resolved by then, I will "
                     f"escalate it to {nxt}.")
    subject = f"Complaint: {(f.get('issue') or state['complaint'])[:70]} — {ref}"
    lang = state.get("language", "en")
    step1 = escalation_for(state)[0]["name"]
    summary = (f"आपकी शिकायत {state.get('company') or 'कंपनी'} के ख़िलाफ़ है। पहले {step1} को यह पत्र भेजें; "
               f"{by} तक जवाब न मिले तो {nxt} पर जाएँ।" if lang in ("hi", "hinglish") else
               f"Your complaint is against {state.get('company') or 'the company'}. Send this letter to the "
               f"{step1} first; if it isn't fixed by {by}, go to {nxt}.")
    return {"subject": subject, "body": "\n\n".join(lines), "remedy": f.get("what_user_wants") or "",
            "reply_deadline_days": 0, "summary": summary, "method": "template"}


def outline_for(state: dict) -> list[str]:
    tier = tier_of(state.get("sector") or "other")
    if tier == "B":
        return ["State that you are reporting an offence and ask for an FIR to be registered.",
                "Give the facts in time order: date, time, place, what happened, who and what was involved.",
                "List the money lost, transaction / account / phone details and the evidence you have.",
                "Ask for an FIR, a free copy, investigation and (for fraud) tracing and freezing the money."]
    if tier == "C":
        return ["Explain in plain words what kind of matter this is (general information, not advice).",
                "How to check case status on eCourts with the CNR number, if there is a case.",
                "Free legal aid from the District Legal Services Authority and who qualifies.",
                "What papers to keep ready, and that Lok Adalat / mediation may settle it."]
    if state.get("sector") == "govt_scheme":
        conditions = [item["condition"] for item in state.get("eligibility_checks") or []]
        assessment = "; ".join(conditions) if conditions else "the sourced scheme conditions"
        return ["Identify the named scheme, relevant part, and the fact that payment has not arrived.",
                "State only the entitlement and eligibility facts supported by the brief and user facts.",
                f"Walk through each condition as met, not met, or needing confirmation: {assessment}.",
                "Ask the department to identify the exact record blocking payment.",
                "Request the correction or document needed and use the scheme's official grievance route."]
    return ["Identify the transaction or service and the user's specific problem.",
            "State the supported facts and the remedy requested.",
            "Use only relevant retrieved legal clauses.",
            "Give a reasonable response request and the correct next escalation step."]


@node("✍️ Drafter")
def drafter(state: dict) -> dict:
    attempt = state.get("draft_attempts", 0) + 1
    clauses = state.get("clauses") or []
    tier = tier_of(state["sector"])
    esc = escalation_for(state)
    dl = reply_deadline(esc) if tier == "A" else {}
    facts = state.get("facts") or {}
    placeholders = "\n".join(f"- {m['fact']}: write {m['placeholder']}" for m in state.get("missing_facts") or [])
    notes = state.get("critic_notes") or []
    outline_context = (f"COMPLAINT TYPE: {(state.get('case_brief') or {}).get('complaint_type') or state.get('sector')}\n"
                       f"CASE BRIEF: {state.get('case_brief') or 'No separate case brief'}\n"
                       f"ELIGIBILITY CHECKS: {state.get('eligibility_checks') or []}\n"
                       f"GAP QUESTIONS: {state.get('gap_questions') or []}\n"
                       f"OFFICIAL SOURCES: {(state.get('case_brief') or {}).get('sources') or []}\n")
    # One call: the model writes its own case-specific outline (the `outline` field) and then the body from it.
    # (A separate planning call cost a full LLM round-trip; eval/latency.py.)
    prompt = (f"LANGUAGE OF THE CONSUMER: {state.get('language', 'en')}\n"
              f"{outline_context}"
              f"{PLAN_TIER.get(tier, '')}\n"
              f"REMEDY REQUESTED: {facts.get('what_user_wants') or 'not stated'}\n"
              "SUGGESTED STRUCTURE (adapt it to this case in your `outline`, then write the body from it):\n- "
              + "\n- ".join(outline_for(state)) + "\n\n"
              f"COMPLAINT (consumer's words):\n{state['complaint']}\n\n"
              f"FACTS: {facts}\nCOMPANY: {state.get('company')}\n"
              f"ELIGIBILITY: {(state.get('eligibility') or {}).get('message') or 'no published conditions'}\n"
              f"COMPLAINANT'S ANSWERS TO REVIEW QUESTIONS: {state.get('case_answers') or 'none'}\n"
              + (f"THE COMPLAINANT EDITED THE PREVIOUS DRAFT TO:\n{state['user_draft']}\n" if state.get("user_draft")
                 else "") +
              f"MISSING FACTS - use these placeholders:\n{placeholders or '- none'}\n\n"
              f"ADDRESSED TO: {addressee(state, esc)}\n"
              f"NEXT FORUM IF UNRESOLVED: {esc[1]['name']} — {esc[1]['when_to_use']}\n"
              + (f"REPLY DEADLINE (computed in code from the rules - write exactly this date, and no other "
                 f"deadline): {dl['date']} — {dl['basis']}"
                 + (" (a statutory time limit; you may mention the rule only via a [C#] clause that states it)"
                    if dl["kind"] == "statutory" else " (a reasonable time we ask for; no rule fixes one)") + "\n"
                 if dl else "") + "\n"
              + (f"{TIER_RULES[tier]}\n\n" if tier in TIER_RULES else "") +
              "CLAUSES YOU MAY CITE:\n"
              + "\n\n".join(f"[{c['tag']}] {c['citation']} — {c['title']}\n{c['text'][:1200]}" for c in clauses)
              + (("\n\nA REVIEWER FOUND THESE PROBLEMS IN YOUR PREVIOUS DRAFT - FIX ALL OF THEM:\n- "
                  + "\n- ".join(notes) + f"\n\nPREVIOUS DRAFT:\n{state.get('draft', '')}") if notes else ""))
    out = llm.json_call(DRAFT_SYSTEM, prompt, schema(DraftOut))
    if out:
        try:
            out = DraftOut.model_validate(out).model_dump() | {"method": llm.label()}
        except Exception:
            out = None
    out = out or template_draft(state)
    return {"draft": out["body"], "subject": out["subject"], "summary": out["summary"], "escalation": esc,
            "draft_attempts": attempt, "draft_method": out["method"], "deadline": dl,
            "drafted_answers": dict(state.get("case_answers") or {}),
            "trace": [f"✍️ Drafter #{attempt} ({out['method']})" + (f": revising for {len(notes)} critic note(s)"
                                                                   if notes and attempt > 1 else "")]}
