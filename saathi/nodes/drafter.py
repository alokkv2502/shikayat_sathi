"""drafter: the complaint email to the verified grievance contact, the escalation ladder (from playbooks.py)
and a plain-language summary in the user's language. Missing facts become [PLACEHOLDERS] - never invented.
On a critic loop it receives the critic's notes and must fix them."""
from __future__ import annotations

import re

from saathi import deadlines, llm
from saathi.nodes.common import node
from saathi.letter import LANGS
from saathi.nodes.escalation import CPGRAMS, STATE_SIDE, general_route, merge_with_seed, seeded
from saathi.playbooks import SECTORS, tier_of
from saathi.nodes.legal_rag import split_tags
from saathi.state import DraftOut, schema

DRAFT_SYSTEM = (
    "You draft firm, respectful and case-specific grievances for Indian members or consumers. First plan: fill "
    "`outline` with the ordered points of THIS case's document and choose the correct complaint frame; then write "
    "the body from that outline, using only facts supported by the complaint, extracted facts, case brief, or "
    "reviewer answers. Never invent dates, eligibility, IDs, amounts, names, contacts or actions already taken. "
    "The case brief's conditions are the RULES, not facts about the complainant: never state that they meet, did, "
    "did not do, or will do anything a condition mentions unless the ELIGIBILITY CHECKS mark it met with the "
    "complainant's own evidence - otherwise ask the office to check that condition. Do not reuse a generic "
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
    # Guard: a step without a source (seed playbook or the official page that states it) never reaches a letter.
    cfg = SECTORS.get(state["sector"]) or SECTORS["other"]
    lad = (state.get("ladder") or {}) if tier_of(state["sector"]) == "A" else {}
    planned = [] if lad.get("fallback") else [s for s in lad.get("steps") or [] if s.get("source")]
    ladder = None
    if seeded(state["sector"]):  # hand-checked seed stays the base; the planner may only add official steps
        steps, _ = merge_with_seed([s for s in cfg["escalation"] if s.get("source")], planned)
    elif planned:
        ladder = planned
    elif lad.get("fallback") and lad.get("steps") and state["sector"] == "public_service":
        ladder = lad["steps"]  # the labelled general government route
    if ladder:  # planned from evidence and validated (escalation_planner)
        steps = [dict(s) for s in ladder if s.get("source")]
        seed = cfg["escalation"]
        if steps and steps[0].get("level") == "company" and seed and seed[0].get("reply_within"):
            steps[0]["reply_within"] = seed[0]["reply_within"]  # e.g. the reasonable 30 days the letter asks for
        cpa = SECTORS["other"]["escalation"][-1].get("file_within")  # CPA s.69: two years
        for s in steps:
            if s.get("level") == "court_or_commission" and "consumer" in s["name"].lower() and cpa:
                s["file_within"] = cpa
    elif not seeded(state["sector"]):  # the planner failed or timed out: the seed playbook
        steps = [dict(s) for s in cfg["escalation"] if s.get("source")] or \
            [dict(s) for s in SECTORS["other"]["escalation"]]
    brief = state.get("case_brief") or {}
    if state["sector"] == "police" and "cyber_fraud" in (state.get("issue_types") or []):
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
    if (state.get("route") or {}).get("authority_type") in STATE_SIDE:  # guard on the FINAL ladder, seed included
        kept = [s for s in steps if not CPGRAMS.search(f"{s['name']} {s.get('channel') or ''} {s.get('url') or ''}")]
        if len(kept) < len(steps):
            if len(kept) >= 2:
                steps = kept
            else:  # the labelled general state route: the office's own cell, then the state's grievance portal
                gr = [dict(s) for s in general_route(state)]
                steps = (kept or gr[:1]) + [s for s in gr if s["level"] != "company"]
    # Deadlines run from the complainant's own complaint to the company / bank when they already made one (the
    # 30 days to the RBI Ombudsman start when the bank received the complaint, not today); otherwise from today.
    facts = state.get("facts") or {}
    deadlines.annotate(steps, start=deadlines.to_date(str(facts.get("complained_on") or "")),
                       incident=deadlines.to_date(facts.get("date") or ""))
    return steps


def clause_time_limits(clauses: list[dict]) -> list[dict]:
    """The time limits the law judge found in the kept clauses (checked against each clause's text in legal_rag),
    as rules for deadlines.case_dates - e.g. Para 72: shadow reversal within 10 working days of the report."""
    out = []
    for c in clauses:
        d = c.get("deadline")
        if d:
            unit = d["unit"].replace("_", " ")
            out.append({"label": f"{d['what']} due", "n": d["n"], "unit": d["unit"],
                        "from": {"incident": "date"}.get(d["counted_from"], d["counted_from"]),
                        "basis": f"{d['what']} within {d['n']} {unit}", "source": c["citation"]})
    return out


def reply_deadline(steps: list[dict]) -> dict:
    """The date the letter asks for a reply by - computed from the first step's rule, or {} if none applies."""
    s = steps[0] if steps else {}
    rw = s.get("reply_within")
    return {"date": s["reply_by"], "basis": rw["basis"], "source": rw.get("source", ""),
            "kind": rw.get("kind", "statutory"), "from_date": s.get("reply_from"),
            "passed": bool(s.get("reply_passed"))} if rw and s.get("reply_by") else {}


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
    cdates = deadlines.case_dates(clause_time_limits(clauses), facts) if tier == "A" else []
    placeholders = "\n".join(f"- {m['fact']}: write {m['placeholder']}" for m in state.get("missing_facts") or [])
    notes = state.get("critic_notes") or []
    outline_context = (f"COMPLAINT TYPE: {(state.get('case_brief') or {}).get('complaint_type') or state.get('sector')}\n"
                       f"CASE BRIEF: {state.get('case_brief') or 'No separate case brief'}\n"
                       f"ELIGIBILITY CHECKS: {state.get('eligibility_checks') or []}\n"
                       f"GAP QUESTIONS: {state.get('gap_questions') or []}\n"
                       f"OFFICIAL SOURCES: {(state.get('case_brief') or {}).get('sources') or []}\n")
    # One call: the model writes its own case-specific outline (the `outline` field) and then the body from it.
    # (A separate planning call cost a full LLM round-trip; eval/latency.py.)
    lang = state.get("letter_language") or "en"
    prompt = (f"LETTER LANGUAGE: {LANGS[lang]}. Write the subject, the addressee_lines and the WHOLE body in "
              f"{LANGS[lang]} only - do not mix languages, and do not add English glosses in brackets such as "
              f"'(pending)'. Keep [PLACEHOLDERS], [C#] tags, IDs, amounts, emails "
              f"and links exactly as given; write company / scheme names as the complainant wrote them.\n"
              f"LANGUAGE OF THE CONSUMER (for the summary): {state.get('language', 'en')}\n"
              f"{outline_context}"
              f"{PLAN_TIER.get(tier, '')}\n"
              f"REMEDY REQUESTED: {facts.get('what_user_wants') or 'not stated'}\n"
              "SUGGESTED STRUCTURE (adapt it to this case in your `outline`, then write the body from it):\n- "
              + "\n- ".join(outline_for(state)) + "\n\n"
              f"COMPLAINT (consumer's words):\n{state['complaint']}\n\n"
              f"FACTS: {facts}\n"
              "WHAT THE FACT KEYS MEAN: date = when it happened; complained_on = when the complainant reported it to "
              "the company / bank itself (NOT the police or cyber-crime portal); complaint_no = the number the "
              "company / bank gave; first_learned_on = when and how they first learned of it; alert_received = "
              "whether the company's / bank's alert came. Reports elsewhere (cyber-crime portal, police) are separate "
              "events - keep their own dates and numbers.\n"
              f"COMPANY: {state.get('company')}\n"
              f"ELIGIBILITY: {(state.get('eligibility') or {}).get('message') or 'no published conditions'}\n"
              f"COMPLAINANT'S ANSWERS TO REVIEW QUESTIONS: {state.get('case_answers') or 'none'}\n"
              + (f"THE COMPLAINANT EDITED THE PREVIOUS DRAFT TO:\n{state['user_draft']}\n" if state.get("user_draft")
                 else "") +
              f"MISSING FACTS - use these placeholders:\n{placeholders or '- none'}\n\n"
              f"ADDRESSED TO: {addressee(state, esc)}\n"
              f"NEXT FORUM IF UNRESOLVED: {esc[-1]['name'] if len(esc) < 2 else esc[1]['name']} — "
              f"{(esc[1] if len(esc) > 1 else esc[-1])['when_to_use']}\n"
              + (f"REPLY DEADLINE (computed in code - write exactly this date): "
                 f"{deadlines.localize(dl['date'], lang)} — {dl['basis']}"
                 + (f"; counted from the complainant's complaint of {deadlines.localize(dl['from_date'], lang)}"
                    if dl.get("from_date") and facts.get("complained_on") else "")
                 + (" (a statutory time limit; you may mention the rule only via a [C#] clause that states it)"
                    if dl["kind"] == "statutory" else " (a reasonable time we ask for; no rule fixes one)")
                 + (" - THIS DATE HAS ALREADY PASSED: say the period ended without a resolution and that you are "
                    "now entitled to approach the next forum" if dl.get("passed") else "") + "\n"
                 if dl else "")
              + ("OTHER DATES COMPUTED IN CODE FROM THIS CASE'S FACTS (use exactly these; no other dates):\n"
                 + "\n".join(f"- {d['label']}: {'about ' if d['approx'] else ''}{deadlines.localize(d['date'], lang)}"
                             f" ({d['basis']}; counted from {deadlines.localize(d['from_date'], lang)}; "
                             f"{d['source']})" + (" - ALREADY MISSED: say so plainly" if d["passed"] else "")
                             for d in cdates) + "\n" if cdates else "")
              + "REQUESTS: ask for what the cited clauses entitle the complainant to, for a written answer on any "
                "liability or decision against them, and for the records the other side holds that would prove the "
                "facts (e.g. transaction and authentication logs, or why an alert was not sent) - only what fits "
                "this case.\n"
              + "KEY FACTS TO STATE when the complainant gave them: whether an alert was received, when and how they "
                "first learned of the problem, when they reported it and the complaint number - these decide "
                "liability and deadlines. NEVER state when the complainant learned of, noticed or reported something "
                "unless the FACTS or their ANSWERS give that date - use its [PLACEHOLDER] or leave it out.\n"
              "CITATIONS: name each rule in words where you rely on it (e.g. 'paragraph 72 of the RBI (Commercial "
              "Banks - Responsible Business Conduct) Directions, 2025') and put its tag right after it, e.g. "
              "'... Directions, 2025 [C3]'. The tags are removed from the final letter. Do not start the body with "
              "a salutation or the addressee - they are added for you.\n\n"
              + (f"{TIER_RULES[tier]}\n\n" if tier in TIER_RULES else "") +
              "CLAUSES YOU MAY CITE (each with its ROLE - use it accordingly: RIGHT_OR_DUTY and DEADLINE clauses are "
              "the legal basis of the complaint, cite them where you say what was owed and by when; ESCALATION "
              "clauses only where you say where you will go next; a DEFINITION only to name the wrong, never as the "
              "rule that was broken; CONTEXT clauses only if needed):\n"
              + "\n\n".join(f"[{c['tag']}] ROLE: {(c.get('role') or 'context').upper()} · {c['citation']} — "
                            f"{c['title']}\n{c['text'][:1500]}" for c in clauses)
              + (("\n\nA REVIEWER FOUND THESE PROBLEMS IN YOUR PREVIOUS DRAFT - FIX ALL OF THEM:\n- "
                  + "\n- ".join(notes) + f"\n\nPREVIOUS DRAFT:\n{state.get('draft', '')}") if notes else ""))
    out = llm.validated(DraftOut, llm.json_call(DRAFT_SYSTEM, prompt, schema(DraftOut))).model_dump() |         {"method": llm.label()}
    return {"draft": split_tags(out["body"]), "subject": out["subject"], "summary": out["summary"], "escalation": esc,
            "addressee_lines": [x.strip() for x in out.get("addressee_lines") or [] if x.strip()][:3],
            "draft_attempts": attempt, "draft_method": out["method"], "deadline": dl, "case_dates": cdates,
            "drafted_answers": dict(state.get("case_answers") or {}),
            "trace": [f"✍️ Drafter #{attempt} ({out['method']})" + (f": revising for {len(notes)} critic note(s)"
                                                                   if notes and attempt > 1 else "")]}
