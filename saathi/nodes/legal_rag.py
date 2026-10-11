"""legal_rag: find the clauses that back this complaint. The drafter may cite ONLY these, by tag [C1]…[Cn]; the
critic and finalize enforce it.

Retrieval searches the WHOLE corpus (no per-sector document filter), then one LLM call judges which retrieved
clauses actually govern this case - e.g. a rule on unauthorised transactions does not apply to a failed one, and
airline rules do not apply to a bus operator. Code checks the judge: it can only keep clauses that were retrieved,
never add one; if none applies, the letter makes no legal claims. (playbooks' law_docs are the evaluation's
answer key for where a sector's law should come from - never a search filter.)"""
from __future__ import annotations

import re

from saathi import llm
from saathi.nodes.common import node
from saathi.playbooks import SECTORS
from saathi.rag.store import retrieve
from saathi.state import ClausesOut, schema

REF_RE = re.compile(r"\b(Section|Sec\.|Rule|Regulation|Clause|Para(?:graph)?)\s+(\d+[A-Z]?)(?:\s*\(\s*\w+\s*\))*", re.I)
UNIT_ALIASES = {"sec.": "section", "paragraph": "para"}
CANDIDATES_EACH = 5   # per search
MAX_CANDIDATES = 10   # shown to the judge
MAX_CLAUSES = 4       # kept for the letter

JUDGE_SYSTEM = (
    "You check which retrieved Indian legal clauses actually apply to a complaint. A clause applies only if it "
    "governs THIS situation and THIS kind of party - the same type of transaction, service and provider (a rule on "
    "unauthorised transactions does not apply to a failed transaction; airline rules do not apply to a bus or "
    "travel agent's own service; consumer-commission procedure applies only to a consumer dispute). A definition "
    "applies when the complaint IS that defined wrong - a paid service not delivered, or a refund withheld, is a "
    "'deficiency' in service, so the clause defining deficiency is the legal basis of the complaint and applies "
    "even though it sets no time limit; other definitions, introductions, purpose and commencement clauses state no "
    "rule and do not apply. Judge only the "
    "clauses listed, by index - never "
    "add or rename one. Prefer clauses that set the duty, time limit, refund or compensation the complainant relies "
    "on. If none applies, mark every clause as not applying - an honest letter without citations is better than a "
    "wrong citation."
)


def candidate_clauses(sector: str, legal_query: str, issue: str = "") -> list[dict]:
    """Searches over the whole corpus: the sector's grievance-rule query (if its playbook has one), intake's English
    legal query for THIS complaint, and the issue sentence. Taken round-robin so one search can't crowd out the
    others."""
    cfg = SECTORS.get(sector) or SECTORS["other"]
    queries = [q for q in [cfg.get("basis_query"), legal_query, issue[:200] if issue != legal_query else ""]
               if q and q.strip()]
    ranked = [retrieve(q, (), CANDIDATES_EACH) for q in queries]
    seen, pool = set(), []
    for rank in range(CANDIDATES_EACH):
        for q, hits in zip(queries, ranked):
            if rank < len(hits) and hits[rank]["citation"] not in seen and len(pool) < MAX_CANDIDATES:
                seen.add(hits[rank]["citation"])
                pool.append({**hits[rank], "query": q})
    return pool


def judge_clauses(state: dict, candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """(clauses that apply, in retrieval order, with tags; rejected ones with the judge's reason)."""
    if not candidates:
        return [], []
    listing = "\n\n".join(f"[{i}] {c['citation']} — {c.get('title') or ''}\n{c['text']}"
                          for i, c in enumerate(candidates))
    facts = state.get("facts") or {}
    prompt = (f"COMPLAINT (English): {state.get('complaint_en') or state.get('complaint') or ''}\n"
              f"ISSUE: {facts.get('issue') or ''}\nAGAINST: {state.get('company') or ''}\n"
              f"KIND OF COMPLAINT: {state.get('sector') or ''}\n\nRETRIEVED CLAUSES:\n{listing}")
    verdicts = {v.index: v for v in llm.validated(ClausesOut, llm.json_call(JUDGE_SYSTEM, prompt,
                                                                            schema(ClausesOut))).results}
    kept, rejected = [], []
    for i, c in enumerate(candidates):  # only retrieved clauses can survive - the judge cannot add one
        v = verdicts.get(i)
        if v and v.applies and len(kept) < MAX_CLAUSES:
            kept.append({**c, "why": v.why})
        else:
            rejected.append({"citation": c["citation"], "why": v.why if v else "not judged"})
    return [{**c, "tag": f"C{i}"} for i, c in enumerate(kept, 1)], rejected


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
    bad_refs = [m.group(0) for m in REF_RE.finditer(body)
                if (UNIT_ALIASES.get(m.group(1).lower(), m.group(1).lower()), m.group(2).upper()) not in allowed]
    return bad_tags, bad_refs


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
    issue = (state.get("facts") or {}).get("issue") or state.get("complaint_en") or state["complaint"]
    candidates = candidate_clauses(state.get("sector") or "other", state.get("legal_query") or issue, issue)
    clauses, rejected = judge_clauses(state, candidates)
    head = (f"📚 Legal RAG: {len(clauses)} of {len(candidates)} retrieved clause(s) apply"
            + (" — " + "; ".join(c["citation"] for c in clauses) if clauses else
               " — no citations will be made"))
    return {"clauses": clauses, "clauses_rejected": rejected,
            "trace": [head] + [f"   ✗ {r['citation']}: {r['why']}" for r in rejected[:6]]}
