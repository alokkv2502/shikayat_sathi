"""critic: deterministic checks + an LLM review. Issues send the draft back to the drafter (max 2 drafts).

Checks
  1. every legal reference maps to a retrieved clause ([C#] tags and Section/Rule/… numbers)
  2. at least one retrieved clause is cited (when any were retrieved)
  3. every email / phone in the draft is a verified contact or a public helpline from the playbook
  4. no invented facts: IDs, amounts and long numbers must come from the complaint / extracted facts
  5. missing facts use their placeholders
  6. (LLM, if configured) unsupported claims and tone
"""
from __future__ import annotations

import re

from saathi import llm
from saathi.nodes.common import node
from saathi.nodes.legal_rag import unsupported_refs
from saathi.playbooks import PUBLIC_CONTACTS
from saathi.state import CriticOut, schema
from saathi.tools.fetch import find_emails, find_phones

CRITIC_SYSTEM = (
    "You review a consumer complaint letter before it is sent. Flag only concrete problems: a legal claim not "
    "supported by the supplied clauses, a fact not present in the consumer's complaint, a threat or abusive tone, "
    "or a missing remedy/deadline. Return an empty list if the letter is fine."
)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


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
    return issues


@node("🧐 Critic")
def critic(state: dict) -> dict:
    issues = rule_issues(state)
    method = "rules"
    clause_block = "\n".join(f"[{c['tag']}] {c['citation']}: {c['text'][:600]}" for c in state.get("clauses") or [])
    res = llm.json_call(CRITIC_SYSTEM, f"CONSUMER'S COMPLAINT:\n{state['complaint']}\n\nCLAUSES:\n{clause_block}\n\n"
                                       f"LETTER:\n{state.get('draft', '')}", schema(CriticOut))
    if res:
        try:
            issues += [f"Reviewer: {i}" for i in CriticOut.model_validate(res).issues]
            method = f"rules + {llm.label()}"
        except Exception:
            pass
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
