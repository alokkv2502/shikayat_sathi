"""critic: deterministic checks + an LLM review. Issues send the draft back to the drafter (max 2 drafts).

Checks
  1. every legal reference maps to a retrieved clause ([C#] tags and Section/Rule/… numbers)
  2. at least one retrieved clause is cited (when any were retrieved)
  3. every email / phone in the draft is a verified contact or a public helpline from the playbook
  4. no invented facts: IDs, amounts and long numbers must come from the complaint / extracted facts
  5. missing facts use their placeholders
  6. every ₹ amount and date matches the complainant's own / a deadline computed in code; the reply-by date is there
  7. tier rules (police complaint asks for an FIR; court matters get an explainer, never a filing)
  8. (LLM, if configured) unsupported claims and tone
"""
from __future__ import annotations

import json
import re
from datetime import date

from saathi import deadlines, llm
from saathi.nodes.common import node
from saathi.letter import language_issue
from saathi.nodes.legal_rag import unsupported_refs
from saathi.playbooks import PUBLIC_CONTACTS, TIERS, tier_of
from saathi.state import CriticOut, schema
from saathi.tools.fetch import find_emails, find_phones

CRITIC_SYSTEM = (
    "You review a personalized grievance before it is sent. Compare it with the complaint type, case brief, "
    "conditions, source evidence, user facts and supplied clauses. Flag only concrete problems: a fact not supported "
    "by the complaint, facts or answers; a clause cited for something its text does not say; an incorrect legal or "
    "administrative frame; known case-specific facts left out; a threat or abusive tone; a missing remedy; an "
    "eligibility claim the condition checks do not support; ignored review answers; or a letter that could be sent "
    "unchanged for a different case. Never ask the letter to add facts, evidence or documents the complainant has "
    "not stated - the review step asks them for those. [PLACEHOLDERS] in capitals and brackets are deliberate "
    "blanks for facts the complainant has not given yet: never flag them. Return an empty list if the letter is "
    "accurate and specific."
)
GOVT_FRAME_RULE = ("This is a government scheme, not a consumer-service deficiency: flag any consumer-law framing or "
                   "routing to the National Consumer Helpline.")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


PLAIN_ANSWER = re.compile(r"^\s*(yes|no|y|n|haan|nahi|not yet|done|ok)\W*$", re.I)
ELIGIBILITY_CLAIM = re.compile(r"(?<!whether )(?<!if )(?<!whether or not )"
                               r"\b(?:i am|i'm|i was) (?:fully |clearly )?eligible\b|"
                               r"(?<!whether )(?<!if )\bi (?:fully |clearly )?qualify\b|"
                               r"(?<!whether )(?<!if )\bi (?:meet|satisfy|fulfil+) (?:all|every)\b", re.I)


def case_fact_values(state: dict) -> list[str]:
    """The complainant's own case-specific facts: extracted facts, details and review answers (not yes/no)."""
    facts = state.get("facts") or {}
    values = [str(v).strip() for k, v in facts.items() if k not in ("issue", "city", "details") and v]
    values += [str(v).strip() for v in (facts.get("details") or {}).values() if v]
    values += [a.strip() for a in (state.get("case_answers") or {}).values()
               if isinstance(a, str) and a.strip() and not PLAIN_ANSWER.match(a)]
    return list(dict.fromkeys(v for v in values if len(v) >= 2))


def facts_used(values: list[str], body: str) -> list[str]:
    low, digits = body.casefold(), _digits(body)
    return [v for v in values if v.casefold() in low or (len(_digits(v)) >= 3 and _digits(v) in digits)]


CONSUMER_FRAME = re.compile(r"consumer protection|deficiency in service|national consumer helpline|\bNCH\b|"
                            r"consumer commission|consumer court", re.I)
COURT_DRAFTING = re.compile(r"\b(?:petitioner|respondent|plaintiff|defendant|humbly (?:prays?|submits?|sheweth)|"
                            r"it is (?:therefore )?(?:most )?(?:respectfully )?prayed|in the (?:hon'?ble )?court of|"
                            r"vakalatnama|affidavit of|memo of parties|plaint\b|prayer clause)", re.I)


def consistency_issues(state: dict, body: str, today: date | None = None) -> list[str]:
    """Every ₹ amount and date in the letter must match one the complainant gave (complaint, facts, review answers,
    filled placeholders), a deadline computed in code, or the sourced case brief / clauses / escalation steps; and a
    tier-A letter must carry the computed reply-by date."""
    today = today or date.today()
    known = " ".join([state.get("complaint") or "",
                      json.dumps(state.get("facts") or {}, ensure_ascii=False),
                      " ".join(f"{k} {v}" for k, v in (state.get("case_answers") or {}).items()),
                      " ".join(f"{k} {v}" for k, v in (state.get("fills") or {}).items()),
                      json.dumps(state.get("case_brief") or {}, ensure_ascii=False),
                      " ".join(c.get("text", "") for c in state.get("clauses") or []),
                      json.dumps(state.get("escalation") or [], ensure_ascii=False),
                      json.dumps(state.get("deadline") or {}, ensure_ascii=False),
                      deadlines.fmt(today)])
    issues = []
    ok_amounts = {v for v, _ in deadlines.amounts(known)}
    if _digits((state.get("facts") or {}).get("amount", "")):
        ok_amounts.add(int(_digits(state["facts"]["amount"])))
    for v, raw in deadlines.amounts(body):
        if v not in ok_amounts:
            issues.append(f"Amount '{raw}' does not match any amount the complainant gave - use their figure or "
                          f"a [PLACEHOLDER].")
    full = {(y, m, d) for y, m, d, _ in deadlines.parse_dates(known)}
    day_month = {(m, d) for _, m, d in full}
    for y, m, d, raw in deadlines.parse_dates(body):
        if (y, m, d) in full or (y is None and (m, d) in day_month) or (None, m, d) in full:
            continue
        issues.append(f"Date '{raw}' does not match any date the complainant gave or a computed deadline - do not "
                      f"invent or recalculate dates.")
    dl = state.get("deadline") or {}
    by = deadlines.localize(dl.get("date") or "", state.get("letter_language") or "en")
    if by and tier_of(state.get("sector") or "other") == "A" and by not in body:
        issues.append(f"State the reply deadline computed from the rules: {by} ({dl['basis']}).")
    return issues


def tier_issues(state: dict, body: str) -> list[str]:
    """B: a police complaint asking for an FIR, outside the consumer-law frame. C: an explainer and referral -
    never a court document."""
    tier = tier_of(state.get("sector") or "other")
    issues = []
    if tier == "B":
        if not re.search(r"\bFIR\b|first information report", body, re.I):
            issues.append("A police complaint must ask the SHO to register an FIR (and give a free copy).")
        if CONSUMER_FRAME.search(body):
            issues.append("Uses a consumer-law frame in a police complaint - describe the offence and facts instead.")
    if tier == "C":
        if COURT_DRAFTING.search(body):
            issues.append("Reads like a court filing - this must be a plain-language explainer and referral, not a "
                          "petition, plaint or affidavit.")
        if not re.search(r"legal services authority|DLSA|legal aid|15100", body, re.I):
            issues.append("Refer the person to the District Legal Services Authority (free legal aid, 15100).")
    return issues


# Hindi: "मैं (एक) पात्र आवेदक हूँ", "मैं पात्र हूँ", "मैं सभी शर्तें पूरी करता/करती हूँ", "मैं योग्य हूँ"
HI_ELIGIBILITY_CLAIM = re.compile(r"(?<!क्या )मैं\s+(?:एक\s+)?(?:पात्र|योग्य)|सभी\s+(?:\S+\s+){0,2}(?:शर्तें|शर्तों|मानदंड)"
                                  r"\s+(?:को\s+)?(?:पूरी|पूरा|पूर्ण)\s+(?:कर|हो)")


def eligibility_issues(state: dict, body: str) -> list[str]:
    """The letter must not claim more than the condition checks support."""
    checks = state.get("eligibility_checks") or []
    open_ = [c for c in checks if c["status"] != "met"]
    if open_ and (ELIGIBILITY_CLAIM.search(body) or HI_ELIGIBILITY_CLAIM.search(body)):
        return [f"Claims eligibility, but {len(open_)} condition(s) are unconfirmed or not met "
                f"({'; '.join(c['condition'].rstrip('.') for c in open_[:3])}) - ask the office to check instead."]
    return []


def case_specific_issues(state: dict, body: str) -> list[str]:
    brief = state.get("case_brief") or {}
    if not brief:
        return []
    issues = []
    hindi = (state.get("letter_language") or "en") == "hi"  # names and facts arrive in English from intake
    names = list(state.get("case_entities") or []) + [brief.get("subject") or ""]
    if not hindi and names and not any(name.casefold() in body.casefold() for name in names if name):
        issues.append(f"Name the researched scheme, policy, or product ({brief.get('subject')}).")

    values = case_fact_values(state)
    if hindi:  # only values that survive translation unchanged can be matched: IDs, amounts, dates
        values = [v for v in values if any(ch.isdigit() for ch in v)]
    matched = len(facts_used(values, body))
    required = min(4, len(values))
    if required and matched < required:
        issues.append(f"Use at least {required} of the {len(values)} explicit case-specific facts; the current draft "
                      f"uses {matched}.")

    if brief.get("complaint_type") == "govt_scheme" or state.get("sector") == "govt_scheme":
        wrong_frame = CONSUMER_FRAME.search(body)
        if wrong_frame:
            issues.append("Uses a consumer-law/consumer-helpline frame for a government scheme; route through the "
                          "scheme's administering department instead.")
    known_urls = " ".join([*(brief.get("sources") or []), brief.get("official_grievance_channel") or "",
                           *(s.get("url") or "" for s in state.get("escalation") or []),
                           *(c.get("source_url") or "" for c in state.get("verified_contacts") or [])])
    for url in set(re.findall(r"https?://[^\s)\]]+", body)):
        if re.sub(r"^https?://(www\.)?", "", url).split("/")[0] not in known_urls:
            issues.append(f"Link {url} is not from the researched sources or the escalation route - remove it.")
    return issues


def rule_issues(state: dict) -> list[str]:
    body, clauses = state.get("draft") or "", state.get("clauses") or []
    issues = []
    bad_tags, bad_refs = unsupported_refs(body, clauses)
    if bad_tags:
        issues.append(f"Cites clause tags that were not retrieved: {', '.join(bad_tags)}. Use only "
                      f"{', '.join('[' + c['tag'] + ']' for c in clauses)}.")
    if bad_refs:
        issues.append(f"Mentions statute numbers not in the retrieved clauses: {', '.join(bad_refs)}. Refer to the "
                      f"law only through the [C#] tags.")
    if clauses and not re.search(r"\[C\d+\]", body):
        issues.append("Does not cite any retrieved clause - support the complaint with at least one [C#].")
    allowed = {c["value"] for c in state.get("verified_contacts") or []} | PUBLIC_CONTACTS
    for e in find_emails(body):
        if e not in allowed:
            issues.append(f"Email {e} is not a verified contact - remove it.")
    for p in find_phones(body):
        if p not in allowed and len(p) >= 8:
            issues.append(f"Phone {p} is not a verified contact - remove it.")
    known = state["complaint"] + " " + " ".join(str(v) for v in (state.get("facts") or {}).values())
    known_digits = _digits(known)
    for tok in set(re.findall(r"\b[A-Z0-9-]*\d[A-Z0-9-]{4,}\b", body)):
        d = _digits(tok)
        if tok in {c["value"] for c in state.get("verified_contacts") or []} or d in allowed:
            continue
        if tok not in known and (not d or d not in known_digits):
            issues.append(f"'{tok}' does not appear in the complaint - do not invent IDs, amounts or numbers.")
    for m in state.get("missing_facts") or []:
        if m["placeholder"] not in body:
            issues.append(f"The {m['fact'].replace('_', ' ')} is unknown - write the placeholder {m['placeholder']}.")
    issues.extend(case_specific_issues(state, body))
    issues.extend(eligibility_issues(state, body))
    issues.extend(tier_issues(state, body))
    issues.extend(consistency_issues(state, body))
    from saathi.nodes.escalation import CPGRAMS, STATE_SIDE
    if (state.get("route") or {}).get("authority_type") in STATE_SIDE and CPGRAMS.search(body):
        issues.append("CPGRAMS is for central-government bodies; this is a state matter - name the state's own "
                      "grievance route from the escalation ladder instead.")
    lang_issue = language_issue(f"{state.get('subject') or ''} {body}", state.get("letter_language") or "en",
                                [state.get("company") or "", *(state.get("case_entities") or []),
                                 (state.get("case_brief") or {}).get("subject") or ""])
    if lang_issue:
        issues.append(lang_issue)
    return issues


@node("🧐 Critic")
def critic(state: dict) -> dict:
    issues = rule_issues(state)
    method = "rules"
    clause_block = "\n".join(f"[{c['tag']}] {c['citation']}: {c['text'][:600]}" for c in state.get("clauses") or [])
    tier = tier_of(state.get("sector") or "other")
    frame_context = (f"DOCUMENT: tier {tier} - {TIERS[tier]} "
                     + ("(a police complaint asking for an FIR)" if tier == "B" else
                        "(a plain-language explainer and referral, never a court document)" if tier == "C" else
                        "(a grievance letter)") + "\n"
                     f"COMPLAINT TYPE: {(state.get('case_brief') or {}).get('complaint_type') or state.get('sector')}\n"
                     f"CASE BRIEF: {state.get('case_brief') or {}}\n"
                     f"CONDITION CHECKS: {state.get('eligibility_checks') or []}\n"
                     f"COMPLAINANT'S REVIEW ANSWERS: {state.get('case_answers') or {}}\n"
                     f"USER FACTS: {state.get('facts') or {}}\n"
                     "ESCALATION ROUTE (verified playbook - correct for this case, do not dispute it): "
                     + " → ".join(s["name"] for s in state.get("escalation") or []) + "\n"
                     + (GOVT_FRAME_RULE + "\n" if state.get("sector") == "govt_scheme" else ""))
    res = llm.json_call(CRITIC_SYSTEM, f"{frame_context}\nCOMPLAINT:\n{state['complaint']}\n\nCLAUSES:\n{clause_block}\n\n"
                                       f"LETTER:\n{state.get('draft', '')}", schema(CriticOut))
    asked = [m["placeholder"] for m in state.get("missing_facts") or []]
    issues += [f"Reviewer: {i}" for i in llm.validated(CriticOut, res).issues
               if not any(ph in i for ph in asked)]  # placeholders we asked for are deliberate, not a fault
    method = f"rules + {llm.label()}"
    attempts = state.get("draft_attempts", 0)
    if issues and attempts < 2:
        verdict = f"{len(issues)} issue(s) → back to drafter"
    elif issues:
        verdict = f"{len(issues)} issue(s) remain after {attempts} drafts → finalize will strip unsupported citations"
    else:
        verdict = "passed — every claim is cited, no invented facts"
    return {"critic_notes": issues, "trace": [f"🧐 Critic ({method}): {verdict}"]
            + [f"   • {i}" for i in issues[:5]]}


def route_after_critic(state: dict) -> str:
    return "drafter" if state.get("critic_notes") and state.get("draft_attempts", 0) < 2 else "human_review"
