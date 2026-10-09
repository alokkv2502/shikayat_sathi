"""legal_rag: retrieve the clauses that back this complaint (sector documents only). The drafter may cite ONLY
these, by tag [C1]…[Cn]; the critic and finalize enforce it."""
from __future__ import annotations

import re

from saathi.nodes.common import node
from saathi.playbooks import ISSUE_QUERIES, SECTORS
from saathi.rag.store import english_query, retrieve

REF_RE = re.compile(r"\b(Section|Sec\.|Rule|Regulation|Clause|Para(?:graph)?)\s+(\d+[A-Z]?)(?:\s*\(\s*\w+\s*\))*", re.I)
UNIT_ALIASES = {"sec.": "section", "paragraph": "para"}


def gather_clauses(sector: str, issue_types: list[str], issue: str, k_each: int = 3, limit: int = 4) -> list[dict]:
    """One query for the sector's grievance rule + one per issue type; clauses taken round-robin across queries
    so one query can't crowd out the others; clauses far weaker than the best match are dropped. `issue` is
    already English (intake translates once); english_query only drops stray Devanagari in rules-only mode."""
    cfg = SECTORS[sector]
    issue = english_query(issue)
    queries = [cfg["basis_query"]] + [f"{ISSUE_QUERIES[i]} {issue[:160]}" for i in issue_types]
    ranked = [retrieve(q, tuple(cfg["law_docs"]), k_each) for q in queries]
    seen, pool = set(), []
    for rank in range(k_each):
        for q, hits in zip(queries, ranked):
            if rank < len(hits) and hits[rank]["citation"] not in seen and len(pool) < limit:
                seen.add(hits[rank]["citation"])
                pool.append({**hits[rank], "query": q})
    best = max((h["score"] for h in pool), default=0)
    pool = [h for h in pool if h["score"] >= best - 0.15]
    return [{**h, "tag": f"C{i}"} for i, h in enumerate(pool, 1)]


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
    if not SECTORS[state.get("sector") or "other"]["law_docs"]:
        return {"clauses": [], "trace": [f"📚 Legal RAG: skipped — no indexed law applies to the "
                                         f"{state.get('sector')} route (no citations will be made)"]}
    clauses = gather_clauses(state["sector"], state.get("issue_types") or ["other"],
                             (state.get("facts") or {}).get("issue") or state.get("complaint_en")
                             or state["complaint"])
    return {"clauses": clauses,
            "trace": [f"📚 Legal RAG: {len(clauses)} clause(s) — " + "; ".join(c["citation"] for c in clauses)]}
