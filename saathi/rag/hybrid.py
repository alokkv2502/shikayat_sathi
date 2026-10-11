"""Hybrid legal retrieval: dense embeddings (bge-small, Qdrant) + BM25 keyword search, merged by reciprocal rank
fusion, plus section expansion and the in-force date filter.

Why both: exact legal phrases - "shadow reversal", "three working days", "zero liability" - are what BM25 catches
and a small embedding model often misses; the embeddings catch paraphrases ("money taken without my permission"
~ "unauthorised electronic banking transaction"). The LLM judge in legal_rag then acts as the reranker.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date
from functools import lru_cache

from saathi import timing
from saathi.rag.ingest import DOCS, all_chunks
from saathi.rag.store import retrieve

RRF_K = 60            # reciprocal rank fusion constant (the usual value)
GROUP_WHOLE = 16      # a hit pulls in its whole section if the section has at most this many chunks…
GROUP_NEIGHBOURS = 3  # …otherwise this many chunks on each side
STOP = set("a an the of to in on for by with and or is are be been was were this that these those it its as at "
           "from any such shall may which who whom whose under into than then there their his her he she they "
           "them not no all also has have had will would can could should if".split())


def tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w in STOP or len(w) < 2:
            continue
        out.append(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w)  # days ~ day
    return out


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.len = [len(d) for d in docs]
        self.avg = sum(self.len) / max(len(docs), 1)
        df = Counter(w for d in docs for w in set(d))
        n = len(docs)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for tf, ln in zip(self.tf, self.len):
            s = 0.0
            for w in query:
                f = tf.get(w)
                if f:
                    s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / self.avg))
            out.append(s)
        return out


@lru_cache(maxsize=1)
def _bm25() -> tuple[tuple[dict, ...], BM25]:
    chunks = all_chunks()
    return chunks, BM25([tokens(f"{c.get('title', '')} {c['text']}") for c in chunks])


def keyword(query: str, k: int = 8) -> list[dict]:
    chunks, index = _bm25()
    q = tokens(query)
    if not q:
        return []
    scores = index.scores(q)
    best = sorted(range(len(chunks)), key=lambda i: -scores[i])[:k]
    return [shape({**chunks[i], "bm25": round(scores[i], 2)}) for i in best if scores[i] > 0]


def shape(c: dict) -> dict:
    """The clause shape every caller expects (as store.retrieve returns it)."""
    return {**c, "act": c.get("act") or c["doc_title"], "section": c.get("section") or c["ref"],
            "source_file": c.get("source_file") or DOCS.get(c["doc_id"], {}).get("file"), "page": None}


def search(query: str, k: int = 8) -> list[dict]:
    """Dense + keyword results for one query, fused by rank."""
    with timing.span("rag", f"hybrid {query[:60]}"):
        dense = retrieve(query, (), k)
        kw = keyword(query, k)
    return fuse([dense, kw])[:k]


def fuse(rankings: list[list[dict]]) -> list[dict]:
    """Reciprocal rank fusion over several ranked lists of chunks (keyed by citation)."""
    score: dict[str, float] = {}
    best: dict[str, dict] = {}
    for ranking in rankings:
        for rank, c in enumerate(ranking):
            key = c["citation"]
            score[key] = score.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
            best[key] = {**best.get(key, {}), **c}
    return [{**best[k], "rrf": round(s, 4)} for k, s in sorted(score.items(), key=lambda kv: -kv[1])]


def section_of(chunk: dict) -> list[dict]:
    """The other chunks of a chunk's section: the whole section if it is small, else its neighbours."""
    group = chunk.get("group")
    if not group:
        return []
    members = [c for c in all_chunks() if c.get("group") == group]
    if len(members) > GROUP_WHOLE:
        seq = chunk.get("seq", 0)
        members = [c for c in members if abs(c.get("seq", 0) - seq) <= GROUP_NEIGHBOURS]
    return [shape(c) for c in members if c["citation"] != chunk["citation"]]


def in_force(chunk: dict, incident: date | None, today: date) -> bool:
    """Was this law in force for this case? effective_for 'event': the date of the transaction / incident (today
    if unknown - a rule that applies only to future transactions cannot apply to this one); 'complaint': today."""
    eff = chunk.get("effective_from") or DOCS.get(chunk.get("doc_id"), {}).get("effective_from")
    if not eff:
        return True
    when = incident if (chunk.get("effective_for") or DOCS.get(chunk.get("doc_id"), {}).get("effective_for")) \
        == "event" and incident else today
    return when >= date.fromisoformat(eff)
