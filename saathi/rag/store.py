"""Legal clause retrieval for the legal_rag node. Clauses come back in the brief's shape:
{text, act, section, source_file, page, citation, title, score, source_url}.

The index holds English text only. Queries arrive in English: the complaint is translated once, by intake's LLM
call (complaint_en and English facts), and the rest of the pipeline works from that - no second translation here."""
from __future__ import annotations

from saathi import timing
from saathi.rag.ingest import DOCS, ensure_index, retrieve as _retrieve

__all__ = ["DOCS", "ensure_index", "retrieve"]


def retrieve(query: str, docs: tuple[str, ...] = (), k: int = 3) -> list[dict]:
    with timing.span("rag", f"retrieve [{','.join(docs)}] {query[:60]}"):
        hits = _retrieve(query, tuple(docs), k)
    return [{**h, "act": h["doc_title"], "section": h["ref"], "source_file": DOCS[h["doc_id"]]["file"],
             "page": None} for h in hits]  # the corpus is chunked per section, not per page
