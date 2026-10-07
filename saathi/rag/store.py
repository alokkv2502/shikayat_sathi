"""Legal clause retrieval for the legal_rag node. Clauses come back in the brief's shape:
{text, act, section, source_file, page, citation, title, score, source_url}."""
from __future__ import annotations

from saathi.rag.ingest import DOCS, ensure_index, retrieve as _retrieve

__all__ = ["DOCS", "ensure_index", "retrieve"]


def retrieve(query: str, docs: tuple[str, ...] = (), k: int = 3) -> list[dict]:
    out = []
    for h in _retrieve(query, tuple(docs), k):
        out.append({**h, "act": h["doc_title"], "section": h["ref"], "source_file": DOCS[h["doc_id"]]["file"],
                    "page": None})  # the corpus is chunked per section, not per page
    return out
