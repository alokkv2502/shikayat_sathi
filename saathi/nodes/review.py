"""human_review: pause with interrupt() so the user can fill placeholders and edit the draft; finalize: build the
final letter (never sent automatically)."""

import json
from datetime import date

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from config import CASES_DIR
from saathi.deadlines import step_dates
from saathi.nodes.common import node
from saathi.nodes.drafter import escalation_for, reply_deadline
from saathi.nodes.legal_rag import strip_unsupported
from saathi.playbooks import tier_of

MAX_REVIEW_ROUNDS = 2  # answering questions redrafts the letter; the last round finalizes


def details_payload(state: dict) -> dict:
    """Everything still unknown, asked in ONE form before anything is drafted."""
    checks = state.get("eligibility_checks") or []
    return {"stage": "details",
            "missing_facts": state.get("missing_facts") or [],
            "eligibility_checks": checks,
            "questions": [{"key": c["condition"], "question": c["question"], "condition": c["condition"]}
                          for c in checks if c["status"] == "unknown" and c.get("question")]
                         + [{"key": q, "question": q, "condition": ""} for q in state.get("extra_questions") or []],
            "eligibility": state.get("eligibility") or {}, "case_brief": state.get("case_brief") or {},
            "tier": tier_of(state.get("sector") or "other"), "safety": state.get("safety") or {},
            "sector": state.get("sector"), "company": state.get("company"),
            "complainant": state.get("complainant") or {}}


@node("📝 Details form")
def ask_details(state: dict) -> dict:
    """One pause before drafting: missing facts, eligibility questions and other useful details, all at once (like
    a form). Resume with Command(resume={"fills": {placeholder: value}, "case_answers": {key: answer},
    "complainant": {...}}). Skipped when there is nothing to ask."""
    payload = details_payload(state)
    if not payload["questions"] and not payload["missing_facts"]:
        return {"case_answers": {}, "fills": {}, "trace": ["📝 Details form: nothing to ask — drafting directly"]}
    edits = interrupt(payload) or {}
    answers, fills = _clean(edits.get("case_answers")), _clean(edits.get("fills"))
    me = {**(state.get("complainant") or {}), **_clean(edits.get("complainant"))}
    return {"case_answers": answers, "fills": fills, "complainant": me,
            "trace": [f"📝 Details form: {len(answers)} answer(s), {len(fills)} detail(s) filled of "
                      f"{len(payload['questions'])} question(s) and {len(payload['missing_facts'])} missing fact(s)"]}


def route_after_details(state: dict) -> str:
    return "reassess" if state.get("case_answers") or state.get("fills") else "drafter"


def review_payload(state: dict) -> dict:
    return {"stage": "review", "subject": state.get("subject"), "draft": state.get("draft"),
            "summary": state.get("summary"),
            "missing_facts": state.get("missing_facts") or [], "critic_notes": state.get("critic_notes") or [],
            "case_brief": state.get("case_brief") or {},
            "eligibility_checks": state.get("eligibility_checks") or [],
            "gap_questions": state.get("gap_questions") or [],
            "extra_questions": state.get("extra_questions") or [],
            "tier": tier_of(state.get("sector") or "other"), "safety": state.get("safety") or {},
            "deadline": state.get("deadline") or {}, "escalation": state.get("escalation") or [],
            "eligibility": state.get("eligibility") or {},
            "case_answers": state.get("case_answers") or {},
            "review_round": state.get("review_rounds", 0) + 1, "max_review_rounds": MAX_REVIEW_ROUNDS,
            "recommended": state.get("recommended"),
            "verified_contacts": state.get("verified_contacts") or [],
            "unverified_contacts": state.get("unverified_contacts") or []}


def _clean(d: dict | None) -> dict:
    return {k: v.strip() for k, v in (d or {}).items() if isinstance(v, str) and v.strip()}


@node("🙋 Human review")
def human_review(state: dict) -> dict:
    """Resume with Command(resume={"draft": <edited body>?, "subject": ..?, "fills": {"[ORDER ID]": "OD1…"},
    "case_answers": {<condition or question>: <answer>}, "complainant": {"name":…, "phone":…, "email":…,
    "address":…}}) - or {} to approve as is. New answers send the case back for reassessment and a redraft
    (shown for review again); otherwise the letter is finalized."""
    edits = interrupt(review_payload(state)) or {}
    before = state.get("case_answers") or {}
    answers = before | _clean(edits.get("case_answers"))
    new = {k: v for k, v in answers.items() if before.get(k) != v}
    fills = (state.get("fills") or {}) | _clean(edits.get("fills"))
    rounds = state.get("review_rounds", 0) + 1
    redraft = bool(new) and rounds < MAX_REVIEW_ROUNDS
    edited = bool(edits.get("draft")) and edits["draft"] != state.get("draft")
    filled = ", ".join(_clean(edits.get("fills")))
    return {"user_edits": edits, "case_answers": answers, "fills": fills, "review_rounds": rounds,
            "redraft": redraft, "user_draft": edits["draft"] if edited else "",
            "trace": ["🙋 Human review: " + (f"{len(new)} new answer(s) → reassess and redraft" if redraft
                                             else "approved")
                      + (f", filled {filled}" if filled else "") + (", edited the draft" if edited else "")]}


def route_after_review(state: dict) -> str:
    return "reassess" if state.get("redraft") else "finalize"


@node("📦 Finalize")
def finalize(state: dict, config: RunnableConfig | None = None) -> dict:
    edits = state.get("user_edits") or {}
    body = edits.get("draft") or state.get("draft") or ""
    subject = edits.get("subject") or state.get("subject") or "Consumer complaint"
    for ph, val in ((state.get("fills") or {}) | _clean(edits.get("fills"))).items():
        body, subject = body.replace(ph, val), subject.replace(ph, val)
    case_answers = (state.get("case_answers") or {}) | _clean(edits.get("case_answers"))
    drafted = state.get("drafted_answers") or {}
    undrafted = {k: v for k, v in case_answers.items() if drafted.get(k) != v}  # e.g. given in the last round
    if undrafted:
        details = "\n".join(f"- {question}: {answer}" for question, answer in undrafted.items())
        body += "\n\nDetails I can confirm about my case:\n" + details
    # Deadlines are recomputed for the day the letter is dated (the review may finish on a later day).
    escalation = escalation_for(state)
    deadline = reply_deadline(escalation) if tier_of(state.get("sector") or "other") == "A" else {}
    old = (state.get("deadline") or {}).get("date")
    if old and deadline.get("date") and old != deadline["date"]:
        body = body.replace(old, deadline["date"])
    clauses = state.get("clauses") or []
    body, removed = strip_unsupported(body, clauses)
    by_tag = {c["tag"]: c for c in clauses}
    used = [t for t in by_tag if f"[{t}]" in body]
    tier = tier_of(state.get("sector") or "other")
    rec = state.get("recommended") if tier == "A" else None
    if rec and not (rec.get("verified") and rec.get("source_url")):  # guard: no source, no recipient
        rec = None
    me = {**(state.get("complainant") or {}), **(edits.get("complainant") or {})}
    to_name = (rec or {}).get("name") or (state.get("escalation") or [{}])[0].get("name", "Grievance Officer")
    if tier == "B":  # always the police station, even when 1930 is the first step on the ladder
        station = (state.get("fills") or {}).get("[POLICE STATION]") or (state.get("facts") or {}).get("police_station")
        to_name = "The Station House Officer\n" + (f"{station} Police Station" if station else
                                                   "[POLICE STATION] Police Station")
    header = (f"To,\n{to_name}\n{state.get('company') or ''}\n" + (f"Email: {rec['value']}\n" if rec else "")
              + f"\nDate: {date.today().strftime('%d %B %Y')}\n\nSubject: {subject}\n\nDear Sir/Madam,\n\n")
    footer = (f"\n\nYours faithfully,\n{me.get('name') or '[YOUR NAME]'}\n{me.get('phone') or '[PHONE]'}\n"
              f"{me.get('email') or '[EMAIL]'}\n{me.get('address') or '[ADDRESS]'}")
    if tier == "B":
        footer += "\n\nKeep a copy and ask for a signed, stamped receipt (or the FIR copy) when you submit this."
    if tier == "C":  # an explainer for the person, not a letter to anyone
        to_name = "You"
        header = f"{subject}\n{date.today().strftime('%d %B %Y')}\n\n"
        footer = ("\n\nThis is general information to help you take the next step. It is not legal advice; a lawyer "
                  "or the District Legal Services Authority can advise on your situation.")
    basis = "\n".join(f"[{t}] {by_tag[t]['citation']} — {by_tag[t]['title']}" for t in used)
    brief = state.get("case_brief") or {}
    sources = brief.get("sources") or []
    sources_text = "\n".join(f"- {url}" for url in sources)
    letter = (header + body + footer + (f"\n\nLegal basis cited:\n{basis}" if basis else "")
              + (f"\n\nScheme and case sources:\n{sources_text}" if sources_text else ""))
    ladder = "\n".join(f"{i}. **{s['name']}** ({s['channel']}) — {s['when_to_use']}"
                       + (f" {s['url']}" if s.get("url") else "") + (f" · {s['phone']}" if s.get("phone") else "")
                       + (f"\n   📅 {step_dates(s)}" if step_dates(s) else "")
                       for i, s in enumerate(escalation, 1))
    if deadline:
        ladder = (f"**Reply due by {deadline['date']}** — {deadline['basis']}"
                  + (f" ({deadline['source']})" if deadline.get("source") else "") + ".\n\n" + ladder)
    no_email = {"A": " (no verified email - use the official complaint form)",
                "B": " (submit at the police station or on your state police portal)", "C": ""}[tier]
    safety = state.get("safety") or {}
    alert = ("## ⚠️ If anyone is in danger right now\n" + "\n".join(f"- {h}" for h in safety.get("helplines") or [])
             + "\n\n") if safety.get("level") == "emergency" else ""
    next_steps = {"A": "## If there's no reply", "B": "## Where to report, and if the police don't act",
                  "C": "## Where to get help"}[tier]
    to_line = "" if tier == "C" else (f"\n\n**To:** {to_name.replace(chr(10), ', ')}"
                                      + (f" <{rec['value']}>" if rec else no_email))
    markdown = (alert + f"# {subject}" + to_line
                + f"\n\n{state.get('summary') or ''}\n\n```\n{letter}\n```\n\n{next_steps}\n{ladder}\n\n"
                  "_Information, not legal advice._\n")
    final = {"to": rec["value"] if rec else None, "to_name": to_name, "tier": tier, "safety": safety, "subject": subject, "letter": letter,
             "markdown": markdown, "summary": state.get("summary"), "escalation": escalation, "deadline": deadline,
             "cited": [by_tag[t] for t in used], "removed_citations": removed,
             "case_brief": brief, "case_answers": case_answers, "eligibility": state.get("eligibility") or {},
             "eligibility_checks": state.get("eligibility_checks") or [],
             "unresolved_critic_notes": state.get("critic_notes") or []}
    thread = ((config or {}).get("configurable") or {}).get("thread_id") or "case"
    record = {k: v for k, v in state.items() if k not in ("pages",)} | {"final": final, "thread_id": thread}
    (CASES_DIR / f"{thread}.json").write_text(json.dumps(record, indent=1, ensure_ascii=False, default=str),
                                              encoding="utf-8")
    ready = {"A": f"letter ready for {final['to'] or 'the official complaint form'}",
             "B": "police complaint ready for the SHO", "C": "explainer and referral ready"}[tier]
    return {"final": final, "trace": [f"📦 Finalize: {ready}"
                                      + (f"; stripped {len(removed)} unsupported citation(s)" if removed else "")
                                      + " — nothing is sent automatically"]}
