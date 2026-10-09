"""gap_check: compare the complainant's facts with the case brief's conditions (met / not_met / unknown), turn every
unknown into a question for the human-review step, and say plainly whether the complainant appears to meet the
published criteria. It also applies the brief's frame: the complaint type decides the sector (and so the legal route),
and for a government scheme the administering body named in the sources becomes the addressee, with the official
hosts the brief was read from as its domains.

reassess: after human review, re-run the same assessment with the complainant's answers (and filled placeholders),
so the redraft is written from the updated facts.

Nothing here is specific to a scheme: without an LLM, conditions are only settled by a plain yes / no answer."""
from __future__ import annotations

import re

from saathi import llm
from saathi.nodes.common import node
from saathi.playbooks import SECTORS, missing_facts_for, tier_of
from saathi.state import GapCheckOut, schema
from saathi.tools.domains import host, registrable

GAP_SYSTEM = (
    "Compare every case condition with only the facts explicitly present in the complaint, the extracted facts and "
    "the complainant's answers. Use met only for direct supporting evidence, not_met only for direct contradicting "
    "evidence, otherwise unknown. Do not infer facts, eligibility, or dates. Put the supporting words in evidence. "
    "For every unknown condition, write one specific, plain question the complainant can answer (ask for the date or "
    "value where the condition needs one). Preserve the condition wording exactly in `condition`. Then list up to 3 "
    "other_questions about facts a strong letter needs that are still missing; never repeat a condition question, a "
    "listed placeholder or anything already answered."
)
TYPE_SECTOR = {"govt_scheme": "govt_scheme", "banking": "bank", "insurance": "insurance", "telecom": "telecom",
               "loan_app": "loan_app"}
YES = re.compile(r"^\s*(?:yes|y|yeah|yep|haan|ha|han|ji|ji haan|done|completed|correct|हाँ|हां|जी)\b", re.I)
NO = re.compile(r"^\s*(?:no|n|nope|not yet|nahi|nahin|na|haven'?t|have not|didn'?t|did not|नहीं|ना)\b", re.I)


def generic_question(condition: str) -> str:
    return f"Does this apply to you? “{condition.rstrip('.')}” — please add the date or detail if you have it."


def answer_for(item: dict, answers: dict) -> str:
    return (answers.get(item["condition"]) or answers.get(item.get("question") or "") or "").strip()


def rule_checks(conditions: list[str], answers: dict, previous: list[dict] | None = None) -> list[dict]:
    """No LLM: keep earlier statuses; a plain yes / no answer settles a condition, anything else stays unknown."""
    before = {c["condition"]: c for c in previous or []}
    out = []
    for cond in conditions:
        item = dict(before.get(cond) or {"condition": cond, "status": "unknown", "evidence": "",
                                         "question": generic_question(cond)})
        ans = answer_for(item, answers)
        if ans and item["status"] == "unknown":
            if YES.match(ans):
                item.update(status="met", evidence=f"You confirmed: {ans}", question="")
            elif NO.match(ans):
                item.update(status="not_met", evidence=f"You said: {ans}", question="")
            else:
                item["evidence"] = f"You said: {ans}"
        out.append(item)
    return out


def conditions_for(state: dict) -> tuple[list[str], str, str]:
    """(conditions, mode, label): the researched brief's conditions (all must hold), else the domain playbook's own
    criteria, e.g. free legal aid for court matters (any one is enough)."""
    brief = state.get("case_brief") or {}
    if brief.get("conditions"):
        return brief["conditions"], "all", brief.get("subject") or "this benefit"
    elig = SECTORS.get(state.get("sector") or "other", {}).get("eligibility") or {}
    return list(elig.get("conditions") or []), elig.get("mode", "all"), elig.get("label", "this")


def eligibility_of(checks: list[dict], mode: str = "all", label: str = "this") -> dict:
    """Does the complainant appear to meet the published criteria? Told to them before anything is sent."""
    if not checks:
        return {"status": "no_conditions", "message": ""}
    if mode == "any":  # e.g. free legal aid: one criterion is enough
        met = [c["condition"].rstrip(".") for c in checks if c["status"] == "met"]
        if met:
            return {"status": "meets_listed", "message": f"You appear to qualify for {label}: {met[0]}."}
        if all(c["status"] == "not_met" for c in checks):
            return {"status": "may_not_qualify", "message": f"From your answers you may not qualify for {label}; "
                                                            "you can still ask for advice."}
        return {"status": "unconfirmed", "message": f"Any one of these is enough for {label} — answer the ones that "
                                                    "apply to you."}
    not_met = [c["condition"].rstrip(".") for c in checks if c["status"] == "not_met"]
    unknown = [c for c in checks if c["status"] == "unknown"]
    if not_met:
        return {"status": "may_not_qualify",
                "message": "From what you've told us, you may not meet: " + "; ".join(not_met) + ". The letter "
                           "will ask the office to check its records and explain, not claim the benefit as due."}
    if unknown:
        return {"status": "unconfirmed",
                "message": f"{len(unknown)} of {len(checks)} published condition(s) are not confirmed yet. Answer "
                           "the questions to make the letter specific; unknown does not mean ineligible."}
    return {"status": "meets_listed", "message": f"Your details match all {len(checks)} published condition(s) we "
                                                 "found. The office may still check its own records."}


def assess(state: dict, conditions: list[str], answers: dict, previous: list[dict] | None = None) -> dict:
    """{checks, extra_questions, method}. The follow-up questions come from intake's single LLM call; an LLM call
    here is needed only to assess published conditions (none -> no call)."""
    facts = state.get("facts") or {}
    placeholders = [m["placeholder"] for m in state.get("missing_facts") or []]
    asked = [q for q in state.get("extra_questions") or []]
    if not conditions:
        return {"checks": [], "extra_questions": [q for q in asked if q not in answers], "method": "no conditions"}
    prompt = (f"CASE CONDITIONS:\n{conditions}\n\nCOMPLAINT:\n{state.get('complaint_en') or state.get('complaint', '')}\n\n"
              f"FACTS EXPLICITLY EXTRACTED:\n{facts}\n\nCOMPLAINANT'S ANSWERS:\n{answers or 'none yet'}\n\n"
              f"PLACEHOLDERS ALREADY ASKED FOR: {placeholders}\nQUESTIONS ALREADY ASKED: {asked}")
    result = llm.json_call(GAP_SYSTEM, prompt, schema(GapCheckOut))
    if result:
        try:
            parsed = GapCheckOut.model_validate(result)
            by_condition = {a.condition: a.model_dump() for a in parsed.assessments}
            fallback = {c["condition"]: c for c in rule_checks(conditions, answers, previous)}
            checks = [by_condition.get(c) or fallback[c] for c in conditions]
            for item in checks:
                if item["status"] == "unknown" and not item.get("question"):
                    item["question"] = generic_question(item["condition"])
                if item["status"] != "unknown":
                    item["question"] = ""
            extra = list(dict.fromkeys(q for q in asked + parsed.other_questions if q.strip() and q not in answers))
            return {"checks": checks, "extra_questions": extra[:4], "method": llm.label()}
        except ValueError:
            pass
    extra = [q for q in state.get("extra_questions") or [] if q not in answers]
    return {"checks": rule_checks(conditions, answers, previous), "extra_questions": extra, "method": "rules"}


def framed_sector(state: dict, brief: dict) -> str:
    sector = state.get("sector") or "other"
    ctype = brief.get("complaint_type")
    if ctype in TYPE_SECTOR:
        return TYPE_SECTOR[ctype]
    if ctype == "consumer_service" and sector == "govt_scheme":
        return "other"  # intake guessed a scheme, the sources say it is a consumer service
    return sector


def frame_updates(state: dict, brief: dict) -> dict:
    sector = framed_sector(state, brief)
    updates: dict = {}
    if sector != state.get("sector"):
        updates.update(sector=sector, tier=tier_of(sector),
                       missing_facts=missing_facts_for(sector, state.get("facts") or {}))
    if sector == "govt_scheme":
        # The department's own grievance channel replaces company grievance-officer searches.
        updates["search_plan"] = [p for p in state.get("search_plan") or [] if p["worker"] == "precedents"]
        body = brief.get("administering_body") or ""
        if body or not state.get("company"):
            updates["company"] = body or brief.get("subject") or state.get("company") or ""
        govt = [registrable(u) for u in brief.get("sources") or [] if host(u).endswith((".gov.in", ".nic.in"))]
        govt = list(dict.fromkeys(d for d in govt if d))
        if govt:
            updates.update(company_domain=govt[0], official_domains=govt,
                           domain_basis="official government source of the case brief")
    return updates


def summary_line(checks: list[dict], extra: list[str]) -> str:
    n_unknown = sum(c["status"] == "unknown" for c in checks)
    n_not = sum(c["status"] == "not_met" for c in checks)
    return (f"{len(checks) - n_unknown - n_not} met, {n_unknown} to confirm, {n_not} may not be met"
            + (f"; {len(extra)} other question(s)" if extra else ""))


@node("🗺️ Frame")
def frame(state: dict) -> dict:
    """Fast (no LLM): apply the brief's frame - sector / tier / addressee / domains - before the workers fan out."""
    updates = frame_updates(state, state.get("case_brief") or {})
    return updates | {"case_answers": {}, "fills": {}, "review_rounds": 0,
                      "trace": [f"🗺️ Frame: route → {updates['sector']} (tier {updates['tier']})" if updates.get("sector")
                                else f"🗺️ Frame: {state.get('sector')} route confirmed"]}


@node("🧩 Gap check")
def gap_check(state: dict) -> dict:
    """Runs in parallel with the search workers (it doesn't change what they search)."""
    conditions, mode, label = conditions_for(state)
    res = assess(state, conditions, {})
    checks = res["checks"]
    return {"eligibility_checks": checks, "extra_questions": res["extra_questions"],
            "gap_questions": [c["question"] for c in checks if c["status"] == "unknown"],
            "eligibility": eligibility_of(checks, mode, label),
            "trace": [f"🧩 Gap check ({res['method']}): "
                      + (summary_line(checks, res["extra_questions"]) if checks or res["extra_questions"]
                         else "no published conditions to compare")]}


@node("🔁 Reassess")
def reassess(state: dict) -> dict:
    """Fold the review answers and filled placeholders into the facts, re-check the conditions, then redraft."""
    answers = state.get("case_answers") or {}
    fills = state.get("fills") or {}
    facts = dict(state.get("facts") or {})
    missing = []
    for m in state.get("missing_facts") or []:
        if fills.get(m["placeholder"]):
            facts[m["fact"]] = fills[m["placeholder"]]
        else:
            missing.append(m)
    conditions, mode, label = conditions_for(state)
    res = assess({**state, "facts": facts}, conditions, answers, state.get("eligibility_checks"))
    checks = res["checks"]
    elig = eligibility_of(checks, mode, label)
    return {"facts": facts, "missing_facts": missing, "eligibility_checks": checks,
            "extra_questions": res["extra_questions"],
            "gap_questions": [c["question"] for c in checks if c["status"] == "unknown"], "eligibility": elig,
            "draft_attempts": 0, "critic_notes": [],
            "trace": [f"🔁 Reassess ({res['method']}): {len(answers)} answer(s) — "
                      + (summary_line(checks, res["extra_questions"]) if checks else "no published conditions")
                      + (f" · {elig['status'].replace('_', ' ')}" if checks else "") + " → drafting"]}
