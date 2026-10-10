"""human_review: pause with interrupt() so the user can fill placeholders and edit the draft; finalize: build the
final letter (never sent automatically)."""

import json
import re

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from config import CASES_DIR
from saathi.deadlines import localize, step_dates
from saathi.letter import labels, today
from saathi.nodes.common import node
from saathi.nodes.drafter import escalation_for, reply_deadline
from saathi.nodes.escalation import CPGRAMS, STATE_SIDE
from saathi.nodes.legal_rag import strip_unsupported
from saathi.playbooks import tier_of
from saathi.tools.domains import host, registrable

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
            "complainant": state.get("complainant") or {},
            "letter_language": state.get("letter_language") or "en"}


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
    chosen = edits.get("letter_language")
    lang = chosen if chosen in ("en", "hi") else state.get("letter_language") or "en"
    return {"case_answers": answers, "fills": fills, "complainant": me, "letter_language": lang,
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


def addressee_block(state: dict, rec: dict | None, tier: str, L: dict) -> list[str]:
    """The 'To,' block, professional and in the letter's language: the receiving officer and organisation - never a
    portal or website name."""
    if tier == "C":
        return []
    if tier == "B":  # always the police station, even when 1930 is the first step on the ladder
        station = (state.get("fills") or {}).get("[POLICE STATION]") or \
            (state.get("facts") or {}).get("police_station") or "[POLICE STATION]"
        return [L["sho"], L["station"].format(station)]
    lines = [ln for ln in state.get("addressee_lines") or []
             if not re.search(r"https?://|www\.|@|portal|website|पोर्टल|वेबसाइट", ln, re.I)][:3]
    officer = (rec or {}).get("name") or ""
    if lines:
        return ([officer] if officer and officer not in " ".join(lines) else []) + lines
    org = state.get("company") or (state.get("case_brief") or {}).get("administering_body") or ""
    st = (state.get("route") or {}).get("state") or ""
    org_line = org + (f", {st}" if org and st and st.lower() not in org.lower() else "")
    return [officer or L["gro"]] + ([org_line] if org_line else [])


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
    lang = state.get("letter_language") or "en"
    L = labels(lang)
    if lang == "hi":  # an English gloss in brackets after a Hindi word - "लंबित (pending)" - is dropped
        gloss = r"\s*\((?:[A-Za-z][A-Za-z .'/-]*)\)"
        body, subject = re.sub(gloss, "", body), re.sub(gloss, "", subject)
    if undrafted:
        details = "\n".join(f"- {question}: {answer}" for question, answer in undrafted.items())
        body += f"\n\n{L['details']}\n" + details
    # Deadlines are recomputed for the day the letter is dated (the review may finish on a later day).
    escalation = escalation_for(state)
    deadline = reply_deadline(escalation) if tier_of(state.get("sector") or "other") == "A" else {}
    old = (state.get("deadline") or {}).get("date")
    if old and deadline.get("date") and old != deadline["date"]:
        body = body.replace(localize(old, lang), localize(deadline["date"], lang))
    clauses = state.get("clauses") or []
    body, removed = strip_unsupported(body, clauses)
    by_tag = {c["tag"]: c for c in clauses}
    used = [t for t in by_tag if f"[{t}]" in body]
    tier = tier_of(state.get("sector") or "other")
    rec = state.get("recommended") if tier == "A" else None
    if rec and not (rec.get("verified") and rec.get("source_url")):  # guard: no source, no recipient
        rec = None
    me = {**(state.get("complainant") or {}), **(edits.get("complainant") or {})}
    to_lines = addressee_block(state, rec, tier, L)
    to_name = to_lines[0] if to_lines else ""
    header = (f"{L['to']}\n" + "\n".join(to_lines) + "\n" + (f"{L['email']}: {rec['value']}\n" if rec else "")
              + f"\n{L['date']}: {today(lang)}\n\n{L['subject']}: {subject}\n\n{L['salutation']}\n\n")
    footer = (f"\n\n{L['closing']}\n{me.get('name') or L['name']}\n{me.get('phone') or L['phone']}\n"
              f"{me.get('email') or L['email_ph']}\n{me.get('address') or L['address']}")
    if tier == "B":
        footer += f"\n\n{L['receipt']}"
    if tier == "C":  # an explainer for the person, not a letter to anyone
        to_name = ""
        header = f"{subject}\n{today(lang)}\n\n"
        footer = f"\n\n{L['not_advice']}"
    basis = "\n".join(f"[{t}] {by_tag[t]['citation']} — {by_tag[t]['title']}" for t in used)
    brief = state.get("case_brief") or {}
    own = {registrable(d) for d in [state.get("company_domain"), *(state.get("official_domains") or [])] if d}
    sources = [u for u in brief.get("sources") or []  # a letter cites official pages only
               if (host(u).endswith((".gov.in", ".nic.in")) or registrable(u) in own)
               and not ((state.get("route") or {}).get("authority_type") in STATE_SIDE and CPGRAMS.search(u))]
    sources_text = "\n".join(f"- {url}" for url in sources)
    letter = (header + body + footer + (f"\n\n{L['basis']}\n{basis}" if basis else "")
              + (f"\n\n{L['sources']}\n{sources_text}" if sources_text else ""))
    ladder = "\n".join(f"{i}. **{s['name']}** ({s['channel']}) — {s['when_to_use']}"
                       + (f" {s['url']}" if s.get("url") else "") + (f" · {s['phone']}" if s.get("phone") else "")
                       + (f"\n   📅 {step_dates(s)}" if step_dates(s) else "")
                       + (f"\n   🔗 source: {s['source']}" if s.get("source") else "")
                       for i, s in enumerate(escalation, 1))
    lad = state.get("ladder") or {}
    if lad.get("fallback") and lad.get("note"):  # honest label when no sector-specific route was found
        ladder = f"_{lad['note']}_\n\n" + ladder
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
