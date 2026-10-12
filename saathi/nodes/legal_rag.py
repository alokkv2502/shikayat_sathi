"""legal_rag: find the clauses that back this complaint. The drafter may cite ONLY these, by tag [C1]…[Cn]; the
critic and finalize enforce it.

1. Several searches over the WHOLE corpus (no per-sector document filter): intake's 3-6 targeted legal queries
   (one per right, duty, deadline or escalation route), its overall legal query, the issue and the sector's
   base query. Each search is HYBRID - embeddings + BM25 keywords (rag/hybrid.py) - and the results are fused.
2. Code drops law not in force for this case: a rule that applies to transactions from 1 January 2027 cannot
   decide a 2026 transaction (judged against the incident date, or the complaint date for complaint procedures).
3. Section expansion: rules only make sense together, so a strong hit pulls in the rest of its section (RBI's
   section D, Paras 64-76, on customer liability: zero liability, limited liability, shadow reversal…).
4. One LLM call judges each candidate against the case facts and dates and gives it a role - right_or_duty,
   deadline, escalation, definition - which the drafter uses (legal basis vs next steps vs naming the wrong).
Code checks the judge: it can only keep retrieved clauses, never add one; if none applies, the letter makes no
legal claims. (playbooks' law_docs are the evaluation's answer key - never a search filter.)"""
from __future__ import annotations

import re
from datetime import date

from saathi import llm
from saathi.deadlines import to_date
from saathi.nodes.common import node
from saathi.playbooks import SECTORS
from saathi.rag import hybrid
from saathi.state import ClausesOut, schema

REF_RE = re.compile(r"\b(Section|Sec\.|Rule|Regulation|Clause|Para(?:graph)?)\s+(\d+[A-Z]?)(?:\s*\(\s*\w+\s*\))*", re.I)
UNIT_ALIASES = {"sec.": "section", "paragraph": "para"}
PER_SEARCH = 6        # hybrid results per search
SEEDS = 12            # best fused results before section expansion
EXPAND_TOP = 4        # the sections of this many best results are pulled in
MAX_CANDIDATES = 30   # shown to the judge
MAX_CLAUSES = 8       # kept for the letter (legal basis, deadlines, then escalation)
JUDGE_THINKING = "medium"  # which liability tier applies is reasoning over facts and dates (measured: low
                          # rejected the zero-liability rule although no alert was received)
ROLE_ORDER = {"right_or_duty": 0, "deadline": 1, "escalation": 2, "definition": 3, "context": 4}

JUDGE_SYSTEM = (
    "You decide which retrieved Indian legal clauses matter for a complaint, using its facts and dates. For each "
    "clause ask: does it decide the complainant's rights, the other side's duties, a deadline, or the escalation "
    "route IN THIS CASE? It must govern this situation and this kind of party - the same type of transaction, "
    "service and provider (a rule on unauthorised transactions does not apply to a failed transaction; airline rules "
    "do not apply to a bus operator). Work through the facts: e.g. for an unauthorised debit, which liability tier "
    "applies depends on when it was reported relative to the bank's alert - so the zero-liability, limited-liability "
    "and beyond-seven-days (board policy) paragraphs, the shadow-reversal timeline and the burden of proof can all "
    "matter; keep every one the facts could turn on. When the outcome depends on a fact that is unknown or "
    "disputed - e.g. the clock runs from the bank's alert and the complainant got no alert, or the refund deadline "
    "depends on a payment method not stated - keep the clause for EACH possible outcome, the one most favourable "
    "to the complainant first; never reject a clause by assuming the unfavourable reading. A clause that only gives "
    "the other side an exemption or defence does not apply unless the facts put it in issue. Give each kept "
    "clause a role: right_or_duty (a right or a duty "
    "that is the legal basis), deadline (a time limit, reversal or reply timeline), escalation (where to go next and "
    "when that body can act), definition (only when the complaint IS the defined wrong, e.g. 'deficiency' in "
    "service), or context. Introductions, purpose and commencement clauses, and definitions of other things, do not "
    "apply. Judge only the clauses listed, by index - never add or rename one. If none applies, mark every clause as "
    "not applying: an honest letter without citations is better than a wrong citation. Code has already removed "
    "law that was not in force on the relevant date. For each kept clause whose OWN text states a time limit that "
    "applies here (e.g. 'within 10 working days' of being notified), give it as deadline: the number and unit "
    "exactly as the clause states them and the event it counts from - code computes the date. Then list in "
    "facts_needed the facts the kept clauses' outcome turns on that the complaint does not state (e.g. whether the "
    "bank's alert was received, when the complainant first learned of it, when they reported it) - include every "
    "date a kept rule's reporting window or deadline counts from; they are asked in the form before the letter is "
    "written."
)
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty five": 45,
             "forty-five": 45, "sixty": 60, "ninety": 90}
UNIT_WORDS = {"hours": r"hours?", "days": r"(?<!working )days?", "working_days": r"working\s+days?",
              "months": r"months?", "years": r"years?"}


def checked_deadline(clause_text: str, d) -> dict | None:
    """Keep the judge's time limit only if that number and unit are written in the clause itself."""
    if not d or d.n <= 0:
        return None
    text = " ".join(clause_text.lower().split())
    numbers = [str(d.n)] + [w for w, n in NUM_WORDS.items() if n == d.n]
    unit = UNIT_WORDS[d.unit]
    ok = any(re.search(rf"(?<![\d\w]){re.escape(num)}\s*(?:\(\d+\)\s*)?{unit}", text) for num in numbers)
    return d.model_dump() if ok else None


def case_queries(state: dict) -> list[str]:
    facts = state.get("facts") or {}
    issue = facts.get("issue") or state.get("complaint_en") or state.get("complaint") or ""
    cfg = SECTORS.get(state.get("sector") or "other") or SECTORS["other"]
    qs = [*(state.get("legal_queries") or []), state.get("legal_query") or "", issue[:200], cfg.get("basis_query")]
    return list(dict.fromkeys(q.strip() for q in qs if q and q.strip()))


def incident_date(state: dict, today: date) -> date | None:
    facts = state.get("facts") or {}
    return to_date(" ".join(str(facts.get(k) or "") for k in ("date",)) or "", today) or \
        to_date(state.get("complaint_en") or "", today)


def candidate_clauses(state: dict, today: date | None = None) -> tuple[list[dict], list[dict]]:
    """(candidates for the judge, clauses dropped as not in force). Seeds come from fusing every search; then the
    sections of the best few are pulled in; law not in force for this case is removed before the LLM sees it."""
    today = today or date.today()
    incident = incident_date(state, today)
    rankings = [[{**c, "query": q} for c in hybrid.search(q, PER_SEARCH)] for q in case_queries(state)]
    seeds = hybrid.fuse(rankings)[:SEEDS]
    pool, seen = [], set()

    def add(c: dict, via: str) -> None:
        if c["citation"] not in seen:
            seen.add(c["citation"])
            pool.append({**c, "via": via})
    for c in seeds:
        add(c, "search")
    for c in seeds[:EXPAND_TOP]:
        for s in hybrid.section_of(c):
            add(s, f"same section as {c['ref']}")
    in_force = [c for c in pool if hybrid.in_force(c, incident, today)]
    dropped = [{"citation": c["citation"], "why": f"not in force for this case (applies from {c.get('effective_from')}"
                                                  f", judged by the {'transaction' if c.get('effective_for') == 'event' else 'complaint'} date)"}
               for c in pool if c not in in_force]
    return in_force[:MAX_CANDIDATES], dropped


def judge_clauses(state: dict, candidates: list[dict], today: date | None = None) -> tuple[list[dict], list[dict],
                                                                                          list[dict]]:
    """(clauses that matter, ordered by role, with tags and any time limit checked against their text; rejected ones
    with the judge's reason; the facts the outcome turns on, for the form)."""
    if not candidates:
        return [], [], []
    today = today or date.today()
    listing = "\n\n".join(f"[{i}] {c['citation']} — {c.get('title') or ''}\n{c['text']}"
                          for i, c in enumerate(candidates))
    facts = {k: v for k, v in (state.get("facts") or {}).items() if v}
    prompt = (f"TODAY: {today.isoformat()}\nCOMPLAINT (English): {state.get('complaint_en') or state.get('complaint') or ''}\n"
              f"FACTS: {facts}\nAGAINST: {state.get('company') or ''}\nKIND OF COMPLAINT: {state.get('sector') or ''}"
              f"\n\nRETRIEVED CLAUSES:\n{listing}")
    parsed = llm.validated(ClausesOut, llm.json_call(JUDGE_SYSTEM, prompt, schema(ClausesOut),
                                                     thinking=JUDGE_THINKING))
    verdicts = {v.index: v for v in parsed.results}
    kept, rejected = [], []
    for i, c in enumerate(candidates):  # only retrieved clauses can survive - the judge cannot add one
        v = verdicts.get(i)
        if v and v.applies:
            kept.append({**c, "why": v.why, "role": v.role, "deadline": checked_deadline(c["text"], v.deadline)})
        else:
            rejected.append({"citation": c["citation"], "why": v.why if v else "not judged"})
    for c in kept:
        if c["deadline"]:
            c["role"] = "deadline"  # a time limit checked in its own text is a deadline, whatever else it says
    chosen = select_by_role(kept)
    rejected += [{"citation": c["citation"], "why": "applies, but beyond the clause limit"} for c in kept
                 if c not in chosen]
    kept = sorted(chosen, key=lambda c: ROLE_ORDER.get(c["role"], 9))  # legal basis first, deadlines, next steps…
    needed = [{"fact": re.sub(r"\W+", "_", f.fact.strip().lower()).strip("_"),
               "placeholder": f.placeholder if f.placeholder.strip().startswith("[") else f"[{f.placeholder.strip().upper()}]"}
              for f in parsed.facts_needed[:5] if f.fact.strip() and f.placeholder.strip()]
    # Every time limit counts from an event: if the case doesn't have that date yet, the form asks for it.
    facts = state.get("facts") or {}
    for c in kept:
        start = (c.get("deadline") or {}).get("counted_from")
        if start and start != "incident" and not facts.get(start) and all(n["fact"] != start for n in needed):
            needed.append({"fact": start, "placeholder": START_FIELD[start]})
    return [{**c, "tag": f"C{i}"} for i, c in enumerate(kept, 1)], rejected, needed


ROLE_QUOTA = {"deadline": 3, "escalation": 2, "definition": 1}  # reserved, so the legal basis cannot crowd them out
START_FIELD = {"complained_on": "[DATE YOU FIRST REPORTED IT TO THEM]",
               "first_learned_on": "[DATE AND HOW YOU FIRST LEARNED OF IT]"}


def select_by_role(kept: list[dict]) -> list[dict]:
    """At most MAX_CLAUSES, in the judge's order within each role: deadlines, escalation and a definition get their
    reserved places first, then rights and duties fill the rest, then context."""
    chosen = []
    for role, quota in ROLE_QUOTA.items():
        chosen += [c for c in kept if c["role"] == role][:quota]
    for role in ("right_or_duty", "context"):
        room = MAX_CLAUSES - len(chosen)
        chosen += [c for c in kept if c["role"] == role][:max(room, 0)]
    return chosen


COMBINED_TAGS = re.compile(r"\[\s*(C\d+(?:\s*(?:,|;|&|and)\s*C\d+)+)\s*\]")


def split_tags(body: str) -> str:
    """'[C1, C2]' / '[C1 and C3]' -> '[C1] [C2]', the form every check and finalize understand."""
    return COMBINED_TAGS.sub(lambda m: " ".join(f"[{t}]" for t in re.findall(r"C\d+", m.group(1))), body or "")


def allowed_refs(clauses: list[dict]) -> set[tuple[str, str]]:
    out = set()
    for c in clauses:
        m = re.match(r"(Section|Rule|Regulation|Clause|Para)\s+(\d+[A-Z]?)", c["ref"])
        if m:
            out.add((m.group(1).lower(), m.group(2).upper()))
    return out


def unsupported_refs(body: str, clauses: list[dict]) -> tuple[list[str], list[str]]:
    """(bad [C#] tags, statute references that don't belong to a retrieved clause)."""
    tags = {c["tag"] for c in clauses}
    bad_tags = [f"[{t}]" for t in re.findall(r"\[(C\d+)\]", body) if t not in tags]
    allowed = allowed_refs(clauses)
    names = name_spans(body, clauses)
    bad_refs = [m.group(0) for m in REF_RE.finditer(body)
                if not any(a <= m.start() < b for a, b in names)
                and (UNIT_ALIASES.get(m.group(1).lower(), m.group(1).lower()), m.group(2).upper()) not in allowed]
    return bad_tags, bad_refs


def name_spans(body: str, clauses: list[dict]) -> list[tuple[int, int]]:
    """Where the letter writes a document's NAME, whose own words can look like a citation: "DGCA CAR Section 3,
    Series M, Part IV" is a document, not Section 3 of anything. These spans are not checked as citations."""
    spans = []
    titles = {t for c in clauses for t in (c.get("doc_title") or c.get("act") or "",) if t}
    for t in titles | {t.split(" (")[0] for t in titles}:
        if len(t) >= 12:
            spans += [(m.start(), m.end()) for m in re.finditer(re.escape(t), body, re.I)]
    # A Civil Aviation Requirement is named by section and series: "CAR Section 3, Series M, Part II"
    spans += [(m.start(), m.end()) for m in re.finditer(r"\bCAR\s+Section\s+\d+(?:\s*,?\s*Series\s+\w+)?", body, re.I)]
    return spans


def strip_unsupported(body: str, clauses: list[dict]) -> tuple[str, list[str]]:
    """Last-resort guard used by finalize: remove anything the critic flagged that is still there."""
    bad_tags, bad_refs = unsupported_refs(body, clauses)
    for t in bad_tags:
        body = body.replace(t, "")
    for r in bad_refs:
        body = body.replace(r, "[citation removed - not in retrieved law]")
    return re.sub(r"[ \t]{2,}", " ", body).strip(), bad_tags + bad_refs


@node("📚 Legal RAG")
def legal_rag(state: dict) -> dict:
    from saathi.playbooks import merge_missing
    candidates, not_in_force = candidate_clauses(state)
    clauses, rejected, needed = judge_clauses(state, candidates)
    rejected = not_in_force + rejected
    facts = state.get("facts") or {}
    # The facts the kept clauses turn on join the one details form (before drafting), protected from being
    # overwritten or duplicated by the generic fields.
    missing = merge_missing(state.get("sector") or "other", facts, state.get("extra_missing"), needed)
    limits = [f"{c['ref']}: {c['deadline']['n']} {c['deadline']['unit'].replace('_', ' ')} from "
              f"{c['deadline']['counted_from'].replace('_', ' ')}" for c in clauses if c.get("deadline")]
    head = (f"📚 Legal RAG: {len(clauses)} of {len(candidates)} candidate clause(s) apply"
            + (f" ({len(not_in_force)} not in force for this case removed first)" if not_in_force else "")
            + (" — " + "; ".join(f"{c['citation']} [{c['role']}]" for c in clauses) if clauses else
               " — no citations will be made"))
    return {"clauses": clauses, "clauses_rejected": rejected, "missing_facts": missing,
            "trace": [head]
            + ([f"   ⏱ time limits (checked in the clause text): {'; '.join(limits)}"] if limits else [])
            + ([f"   ❓ deciding facts to ask: {', '.join(n['placeholder'] for n in needed)}"] if needed else [])
            + [f"   ✗ {r['citation']}: {r['why']}" for r in rejected[:8]]}
