"""RAG over Indian consumer-protection law. The drafting agent may cite ONLY clauses retrieved here.

Corpus (raw/legal/):
  cpa    Consumer Protection Act, 2019                                   (Section)
  ecom   Consumer Protection (E-Commerce) Rules, 2020                    (Rule)
  ios    Reserve Bank - Integrated Ombudsman Scheme, 2026                (Clause)
  trai   Telecom Consumers Complaint Redressal Regulations, 2012         (Regulation)
  ins    Insurance Ombudsman Rules, 2017 (as amended till 9.11.2023)     (Rule)
  dl     Reserve Bank of India (Digital Lending) Directions, 2025        (Para)

Chunks keep their citation ("Consumer Protection Act, 2019, Section 35(1)–(2)"). Vectors go to a local
Qdrant (QDRANT_URL); if no server is reachable, embedded on-disk Qdrant (data/qdrant/) is used.

    python -m saathi.rag.ingest index
    python -m saathi.rag.ingest query "refund not given for damaged product" --docs ecom,cpa
"""
from __future__ import annotations

import argparse
import atexit
import re
import sys
import uuid
from functools import lru_cache

from config import EMBED_MODEL, LEGAL_DIR, QDRANT_API_KEY, QDRANT_COLLECTION, QDRANT_LOCAL_PATH, QDRANT_URL

DOCS: dict[str, dict] = {
    "cpa": {"title": "Consumer Protection Act, 2019", "file": "cpa_2019.pdf", "unit": "Section",
            "source_url": "https://www.indiacode.nic.in/bitstream/123456789/18964/1/cpa.pdf"},
    "ecom": {"title": "Consumer Protection (E-Commerce) Rules, 2020", "file": "ecommerce_rules_2020.html",
             "unit": "Rule", "start": "1. Short title and commencement", "end": "[ F. No.",
             "source_url": "https://consumeraffairs.nic.in/sites/default/files/E%20commerce%20rules.pdf"},
    "ios": {"title": "Reserve Bank – Integrated Ombudsman Scheme, 2026", "file": "rbi_ios_2026.pdf",
            "unit": "Clause", "parts": ["Clause", "Salient features, para"],  # the complaint-form annex is not law
            "source_url": "https://website.rbi.org.in/documents/d/rbi/reserve-bank-integrated-ombudsman-scheme-2026-160126"},
    "trai": {"title": "Telecom Consumers Complaint Redressal Regulations, 2012", "file": "trai_tccrr_2012.pdf",
             "unit": "Regulation", "parts": ["Regulation"],  # the Explanatory Memorandum is not binding
             "source_url": "https://trai.gov.in/sites/default/files/2024-10/Telecom-TCCRR0012012%20_0.pdf"},
    "ins": {"title": "Insurance Ombudsman Rules, 2017", "file": "insurance_ombudsman_rules_2017.pdf", "unit": "Rule",
            "source_url": "https://cioins.co.in:8082/notification/Insurance%20Ombudsman%20Rules,%202017"
                          "(%20as%20amended%20till%2009.11.2023).pdf"},
    "dl": {"title": "RBI (Digital Lending) Directions, 2025", "file": "rbi_digital_lending_directions_2025.html",
           "unit": "Para", "start": "Chapter I: Preliminary", "start_occurrence": 2, "end": "Back to previous page",
           "parts": ["Para", "Annex"],
           "source_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=12848"},
}

# The CPA PDF loses its marginal headings in text extraction; these are the Act's own headings for the
# sections consumer complaints most often engage.
CPA_HEADINGS = {
    1: "Short title, extent, commencement and application", 2: "Definitions",
    17: "Complaints relating to violation of consumer rights or unfair trade practices",
    18: "Powers and functions of Central Authority", 20: "Power of Central Authority to recall goods, etc.",
    21: "Power of Central Authority to issue directions and penalties against false or misleading advertisements",
    34: "Jurisdiction of District Commission", 35: "Manner in which complaint shall be made",
    36: "Proceedings before District Commission", 37: "Reference to mediation",
    38: "Procedure on admission of complaint", 39: "Findings of District Commission",
    41: "Appeal against order of District Commission", 69: "Limitation period",
    71: "Enforcement of orders of District Commission, State Commission and National Commission",
    72: "Penalty for non-compliance of order", 82: "Application of Chapter",
    83: "Product liability action", 84: "Liability of product manufacturer",
    85: "Liability of product service provider", 86: "Liability of product sellers",
    94: "Measures to prevent unfair trade practices in e-commerce, direct selling, etc.",
    100: "Act not in derogation of any other law",
}

_HEAD_RE = re.compile(r"^\s*(\d{1,3})([A-Z]?)\.\s*(.*)$")
_SUB_RE = re.compile(r"(?=\(\s?(\d{1,3}|[ivx]{1,5})\s?\)\s)")


# --------------------------------------------------------------------------- text extraction

def doc_text(doc_id: str) -> str:
    d = DOCS[doc_id]
    path = LEGAL_DIR / d["file"]
    if path.suffix == ".pdf":
        from pypdf import PdfReader

        text = "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
    else:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(path.read_text(errors="ignore"), "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    if d.get("start"):
        pos, n = -1, 0
        while n < d.get("start_occurrence", 1):
            pos = text.find(d["start"], pos + 1)
            n += 1
        text = text[max(pos, 0):]
    if d.get("end") and d["end"] in text:
        text = text[: text.find(d["end"])]
    # Drop footnote-marker-only lines and page numbers.
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln and not re.fullmatch(r"\d{1,2}", ln))


def _title(rest: str) -> str:
    m = re.match(r"(.{3,110}?)(?:\s*-{2,}|\s*[.:]\s*[—–-]+|\s*[—–]+)", rest)
    if m and not re.match(r"^\(", rest):
        return m.group(1).strip(" .:-—–")
    return re.sub(r"\s+", " ", rest)[:90].strip()


def chunk_doc(doc_id: str, max_chars: int = 1800) -> list[dict]:
    """Split a document by its numbered sections (monotonic numbering), then by sub-sections if long."""
    d = DOCS[doc_id]
    parts = d.get("parts", [d["unit"]])
    sections: list[dict] = []
    part, last = 0, 0
    cur: dict | None = None
    for ln in doc_text(doc_id).splitlines():
        m = _HEAD_RE.match(ln)
        num = int(m.group(1)) if m else None
        is_head = bool(m) and (last < num <= last + 2 or (num == last and m.group(2)))
        restart = bool(m) and num == 1 and last >= 3 and m.group(3) and m.group(3)[0].isupper()
        if restart:
            if part + 1 >= len(parts):
                break  # a further annex (forms, templates) - not citable law
            part += 1
            is_head = True
        if is_head:
            title = (CPA_HEADINGS.get(num) if doc_id == "cpa" and part == 0 else None) or _title(m.group(3))
            cur = {"num": f"{num}{m.group(2)}", "part": part, "title": title, "lines": [ln]}
            sections.append(cur)
            last = num
        elif cur:
            cur["lines"].append(ln)
    chunks = []
    for s in sections:
        body = re.sub(r"\s+", " ", " ".join(s["lines"])).strip()
        label = f"{parts[s['part']]} {s['num']}"
        pieces = [p for p in _SUB_RE.split(body) if p and not re.fullmatch(r"\d{1,3}|[ivx]{1,5}", p)] \
            if len(body) > max_chars else [body]
        groups: list[str] = []
        for p in pieces:
            if groups and len(groups[-1]) + len(p) <= max_chars:
                groups[-1] += p
            else:
                groups.append(p)
        prev_ref = None
        for g in groups:
            ref = label
            subs = [int(x) for x in re.findall(r"\((\d{1,3})\)\s", g)]
            if len(groups) > 1 and subs:
                seq = [subs[0]]  # top-level sub-sections run consecutively; inline cross-refs don't
                for x in subs[1:]:
                    if x == seq[-1] + 1:
                        seq.append(x)
                ref = f"{label}({seq[0]})" if len(seq) == 1 else f"{label}({seq[0]})–({seq[-1]})"
            if ref == prev_ref or (len(groups) > 1 and not subs):
                ref = f"{prev_ref or label} (contd.)"
            prev_ref = ref.replace(" (contd.)", "")
            chunks.append({"doc_id": doc_id, "doc_title": d["title"], "ref": ref, "title": s["title"],
                           "citation": f"{d['title']}, {ref}", "text": g[:3500],
                           "source_url": d["source_url"]})
    return chunks


# --------------------------------------------------------------------------- Qdrant

@lru_cache(maxsize=1)
def embedder():
    from fastembed import TextEmbedding

    return TextEmbedding(EMBED_MODEL)


@lru_cache(maxsize=1)
def client():
    from qdrant_client import QdrantClient

    try:
        c = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=3, check_compatibility=False)
        c.get_collections()
        client.mode = f"server {QDRANT_URL}"
        return c
    except Exception:
        print(f"  [rag] Qdrant server not reachable at {QDRANT_URL}; using embedded Qdrant at {QDRANT_LOCAL_PATH}",
              file=sys.stderr)
        client.mode = f"embedded {QDRANT_LOCAL_PATH}"
        local = QdrantClient(path=str(QDRANT_LOCAL_PATH))
        atexit.register(local.close)
        return local


def build_index() -> dict:
    from qdrant_client.models import Distance, PayloadSchemaType, PointStruct, VectorParams

    chunks = [c for doc_id in DOCS if (LEGAL_DIR / DOCS[doc_id]["file"]).exists() for c in chunk_doc(doc_id)]
    vecs = [list(map(float, v)) for v in embedder().passage_embed([f"{c['citation']}: {c['text']}" for c in chunks])]
    c = client()
    if c.collection_exists(QDRANT_COLLECTION):
        c.delete_collection(QDRANT_COLLECTION)
    c.create_collection(QDRANT_COLLECTION, vectors_config=VectorParams(size=len(vecs[0]), distance=Distance.COSINE))
    try:
        c.create_payload_index(QDRANT_COLLECTION, "doc_id", PayloadSchemaType.KEYWORD)
    except Exception:
        pass  # embedded mode ignores payload indexes
    c.upsert(QDRANT_COLLECTION, points=[
        PointStruct(id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ch['citation']}|{i}")), vector=v, payload=ch)
        for i, (ch, v) in enumerate(zip(chunks, vecs))])
    retrieve.cache_clear()
    per_doc = {d: sum(ch["doc_id"] == d for ch in chunks) for d in DOCS}
    return {"chunks": len(chunks), "per_doc": per_doc, "collection": QDRANT_COLLECTION, "qdrant": client.mode}


def ensure_index() -> None:
    c = client()
    if not c.collection_exists(QDRANT_COLLECTION) or not c.count(QDRANT_COLLECTION).count:
        print("  [rag] indexing legal corpus…", file=sys.stderr)
        print("  [rag]", build_index(), file=sys.stderr)


@lru_cache(maxsize=512)
def retrieve(query: str, docs: tuple[str, ...] = (), k: int = 3) -> tuple[dict, ...]:
    """Top-k clauses, optionally restricted to some documents. Only these may be cited."""
    from qdrant_client.models import FieldCondition, Filter, MatchAny

    ensure_index()
    qv = list(map(float, next(iter(embedder().query_embed(query)))))
    flt = Filter(must=[FieldCondition(key="doc_id", match=MatchAny(any=list(docs)))]) if docs else None
    hits = client().query_points(QDRANT_COLLECTION, query=qv, limit=k, query_filter=flt, with_payload=True).points
    return tuple({**h.payload, "score": round(float(h.score), 3)} for h in hits)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index")
    ch = sub.add_parser("chunks")
    ch.add_argument("doc", choices=list(DOCS))
    q = sub.add_parser("query")
    q.add_argument("text", nargs="+")
    q.add_argument("--docs", default="")
    q.add_argument("-k", type=int, default=3)
    a = ap.parse_args()
    if a.cmd == "index":
        print(build_index())
    elif a.cmd == "chunks":
        for c in chunk_doc(a.doc):
            print(f"{c['ref']:<34} {len(c['text']):>5}  {c['title'][:70]}")
    else:
        docs = tuple(x for x in a.docs.split(",") if x)
        for h in retrieve(" ".join(a.text), docs, a.k):
            print(f"[{h['score']}] {h['citation']} — {h['title']}\n{h['text'][:400]}\n")


if __name__ == "__main__":
    main()
