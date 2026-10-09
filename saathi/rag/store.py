"""Legal clause retrieval for the legal_rag node. Clauses come back in the brief's shape:
{text, act, section, source_file, page, citation, title, score, source_url}.

The index holds English text only. Queries arrive in English: the complaint is translated once, by intake's LLM
call (complaint_en and English facts), and the rest of the pipeline works from that - no second translation here."""
from __future__ import annotations

import re

from saathi import timing
from saathi.rag.ingest import _DEVANAGARI, DOCS, ensure_index, retrieve as _retrieve

__all__ = ["DOCS", "ensure_index", "english_query", "retrieve"]


def english_query(text: str) -> str:
    """No LLM. Drops any Devanagari left in a query (rules-only mode, where intake could not translate) so the
    English part still searches cleanly."""
    if not _DEVANAGARI.search(text or ""):
        return text
    return re.sub(r"\s+", " ", _DEVANAGARI.sub(" ", text)).strip() or text


def retrieve(query: str, docs: tuple[str, ...] = (), k: int = 3) -> list[dict]:
    out = []
    q = english_query(query)
    with timing.span("rag", f"retrieve [{','.join(docs)}] {q[:60]}"):
        hits = _retrieve(q, tuple(docs), k)
    for h in hits:
        out.append({**h, "act": h["doc_title"], "section": h["ref"], "source_file": DOCS[h["doc_id"]]["file"],
                    "page": None})  # the corpus is chunked per section, not per page
    return out
