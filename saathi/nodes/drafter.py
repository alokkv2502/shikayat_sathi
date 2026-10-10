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
    # Guard: a step without a source (seed playbook or the official page that states it) never reaches a letter.
    cfg = SECTORS.get(state["sector"]) or SECTORS["other"]
    steps = [dict(s) for s in cfg["escalation"] if s.get("source")] or \
        [dict(s) for s in SECTORS["other"]["escalation"]]
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
    out = llm.validated(DraftOut, llm.json_call(DRAFT_SYSTEM, prompt, schema(DraftOut))).model_dump() |         {"method": llm.label()}
    return {"draft": out["body"], "subject": out["subject"], "summary": out["summary"], "escalation": esc,
            "draft_attempts": attempt, "draft_method": out["method"], "deadline": dl,
            "drafted_answers": dict(state.get("case_answers") or {}),
            "trace": [f"✍️ Drafter #{attempt} ({out['method']})" + (f": revising for {len(notes)} critic note(s)"
                                                                   if notes and attempt > 1 else "")]}
