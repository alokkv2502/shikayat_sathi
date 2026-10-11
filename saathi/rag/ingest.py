"""RAG over Indian consumer-protection law. The drafting agent may cite ONLY clauses retrieved here.

Corpus (raw/legal/):
  cpa    Consumer Protection Act, 2019                                   (Section)
  ecom   Consumer Protection (E-Commerce) Rules, 2020                    (Rule)
  ios    Reserve Bank - Integrated Ombudsman Scheme, 2026                (Clause)
  trai   Telecom Consumers Complaint Redressal Regulations, 2012         (Regulation)
  ins    Insurance Ombudsman Rules, 2017 (as amended till 9.11.2023)     (Rule)
  dl     Reserve Bank of India (Digital Lending) Directions, 2025        (Para)
  ccr    Consumer Protection (Consumer Disputes Redressal Commissions) Rules, 2020   (Rule)
  gen    Consumer Protection (General) Rules, 2020                       (Rule)
  ecom26 Consumer Protection (E-Commerce) (Amendment) Rules, 2026        (Rule)
  ins23  Insurance Ombudsman (Amendment) Rules, 2023                     (Rule)
  rbc    RBI (Commercial Banks - Responsible Business Conduct) Directions, 2025   (Para)
  rbc26  RBI (Commercial Banks - Responsible Business Conduct) Third Amendment Directions, 2026   (Para)
  car_dbc    DGCA CAR 3-M-IV: denied boarding, cancellations, delays            (Para 3.3 - outline numbering)
  car_refund DGCA CAR 3-M-II: refund of airline tickets, Rev. 3, 2026      (Para 3(a) - from its OCR text)
  elec   Electricity (Rights of Consumers) Rules, 2020                   (Rule)

Only English text is indexed: the gazette PDFs are bilingual, so each document starts at its English notification
and any line containing Devanagari is dropped. (Hindi complaints are translated once, at intake - see store.py.)
"Consumer Protection (E-Commerce) Rules, 2020.pdf" is a scan with no text layer; ecommerce_rules_2020.html covers it.

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
            # Clause 1: "come into force with effect from July 1, 2026" - a complaints procedure, so judged against
            # the date the complaint is made
            "effective_from": "2026-07-01", "effective_for": "complaint",
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
    # One gazette PDF carries two notifications (G.S.R. 448(E) and 449(E)), each in Hindi then English.
    "ccr": {"title": "Consumer Protection (Consumer Disputes Redressal Commissions) Rules, 2020",
            "file": "Consumer Commission Rules & General Rules_1732703661.pdf", "unit": "Rule",
            "start": "G.S.R. 448(E)", "end": "SCHEDULE-I",  # the schedule is a reporting form, not law
            "source_url": "https://consumeraffairs.nic.in/acts-and-rules/consumer-protection"},
    "gen": {"title": "Consumer Protection (General) Rules, 2020",
            "file": "Consumer Commission Rules & General Rules_1732703661.pdf", "unit": "Rule",
            "start": "G.S.R. 449 (E)", "end": "[F.No.J-10/6/2018-CPU]",
            "source_url": "https://consumeraffairs.nic.in/acts-and-rules/consumer-protection"},
    # Text extraction drops some digits in this PDF, so the amending rules (3.-6.) are renumbered. In amendments,
    # "(4)" etc. inside a rule are the principal rules' sub-rules, so chunks are cited by the amending rule only.
    "ecom26": {"title": "Consumer Protection (E-Commerce) (Amendment) Rules, 2026",
               "file": "E Commerce Amendment Rules 2026_1789127949.pdf", "unit": "Rule",
               "start": "NOTIFICATION", "end": "[F. No.",
               "fix": lambda t: _renumber(t, r"(?m)^\.?\s*(?=In the princip ?al rules)", first=3),
               "subrefs": False,
               "source_url": "https://consumeraffairs.nic.in/acts-and-rules/consumer-protection"},
    # OCR'd scan: rule 2 reads "Z."; the gazette's Hindi running header comes through as Latin junk.
    "ins23": {"title": "Insurance Ombudsman (Amendment) Rules, 2023",
              "file": "Insurance Ombudsman(Amendment) Rules, 2023.pdf", "unit": "Rule",
              "start": "NOTIFICATION", "end": "[F. No.",
              "fix": lambda t: re.sub(r"(?m)^\[inT .*$", "", re.sub(r"(?m)^Z\. In the", "2. In the", t)),
              "subrefs": False,
              "source_url": "https://cioins.co.in:8082/notification/"},
    # RBI master direction for commercial banks: customer service, grievance redressal, customer liability in
    # unauthorised electronic banking transactions. Running paragraph numbers across chapters; the body starts at
    # the 2nd "Chapter I – Preliminary" (the 1st is the table of contents); the annexes are forms, not law.
    "rbc": {"title": "RBI (Commercial Banks - Responsible Business Conduct) Directions, 2025",
            "file": "Consumer_liability.pdf", "unit": "Para",
            "start": "Chapter I – Preliminary", "start_occurrence": 2, "end": "Annex I - Format of Comprehensive",
            "sections": "lettered",  # "D.1 …", "D.2 …": section D (Paras 64-76) is the customer-liability rules
            "source_url": "https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx"},
    # Revised customer-liability rules for unauthorised electronic banking (transactions from 1 January 2027).
    "rbc26": {"title": "RBI (Commercial Banks - Responsible Business Conduct) Third Amendment Directions, 2026",
              "file": "Commercial_bank_conduct.pdf", "unit": "Para", "subrefs": False,
              # Para 3(2): "shall apply in cases of electronic banking transactions undertaken … on or after
              # January 1, 2027" - judged against the transaction's date
              "effective_from": "2027-01-01", "effective_for": "event",
              "source_url": "https://www.rbi.org.in/Scripts/NotificationUser.aspx"},
    # DGCA Civil Aviation Requirements are numbered as an outline (3.2, 3.2.1 ...): chunk_outline cites "Para 3.2".
    "car_dbc": {"title": "DGCA CAR Section 3, Series M, Part IV (Facilities to passengers on denied boarding, "
                         "cancellation and delays)",
                "file": "D3M-M4-1.pdf", "unit": "Para", "outline": True, "start": "1. INTRODUCTION",
                "end": "(B.S. Bhullar)", "fix": lambda t: _drop_car_headers(t),
                "source_url": "https://www.dgca.gov.in"},
    # A scan with no text layer: indexed from its one-time OCR (CAR 3M2 Feb26.ocr.txt; one line in para 3(l) is
    # illegible and marked so). Lettered requirements are cited "Para 3(a)".
    "car_refund": {"title": "DGCA CAR Section 3, Series M, Part II (Refund of airline tickets), Rev. 3, 2026",
                   "file": "CAR 3M2 Feb26.ocr.txt", "unit": "Para", "outline": True, "letters": True,
                   "start": "1. Introduction", "end": "(Faiz", "fix": lambda t: _drop_car_headers(t),
                   "effective_from": "2026-03-26", "effective_for": "event",  # its header: "EFFECTIVE: 26 March 2026"
                   "source_url": "https://www.dgca.gov.in"},
    "elec": {"title": "Electricity (Rights of Consumers) Rules, 2020",
             "file": "Gazette_Electricity-Rights-of-Consumers-Rules-2020.pdf", "unit": "Rule",
             "start": "G.S.R. 818(E)", "end": "[F. No. 23/05/2020", "fix": lambda t: t.replace("Provisio ns", "Provisions"),
             "source_url": "https://powermin.gov.in"},
}


def _drop_car_headers(text: str) -> str:
    """CAR page headers and footers repeat on every page (also OCR'd without spaces)."""
    junk = re.compile(r"(?i)^(rev\.?\s*\d|civil\s*aviation\s*requirements|series\s*.m.\s*part|section\s*3\s*[-–]\s*air"
                      r"|\d{1,2}\s*(st|nd|rd|th)?\s*feb|issue\s*i,|dated\s*\d)")
    return "\n".join(ln for ln in text.splitlines() if not junk.match(ln.replace(" ", "")) and not junk.match(ln))

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")

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
    elif path.suffix == ".txt":  # an OCR'd scan, extracted once
        text = path.read_text(encoding="utf-8")
    else:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
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
    # English only; drop footnote-marker-only lines and page numbers.
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln and not _DEVANAGARI.search(ln) and not re.fullmatch(r"\d{1,2}", ln))
    return d["fix"](text) if d.get("fix") else text


def _renumber(text: str, pattern: str, first: int) -> str:
    """Prefix each heading line matched by `pattern` with consecutive numbers (for PDFs that lost them)."""
    n = iter(range(first, 10_000))
    return re.sub(pattern, lambda _: f"{next(n)}. ", text)


def _title(rest: str) -> str:
    m = re.match(r"(.{3,110}?)(?:\s*-{2,}|\s*[.:]\s*[—–-]+|\s*[—–]+)", rest)
    if m and not re.match(r"^\(", rest):
        return m.group(1).strip(" .:-—–")
    return re.sub(r"\s+", " ", rest)[:90].strip()


def _split_long(text: str, max_chars: int) -> list[str]:
    """Cut text with no sub-section markers to split on at sentence ends (or spaces), so nothing is truncated."""
    out = []
    while len(text) > max_chars:
        cut = max(text.rfind(". ", 0, max_chars), text.rfind("; ", 0, max_chars))
        cut = cut + 1 if cut > max_chars // 2 else (text.rfind(" ", 0, max_chars) if " " in text[:max_chars]
                                                    else max_chars)
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    return out + [text] if text else out


def chunk_outline(doc_id: str, max_chars: int = 1800) -> list[dict]:
    """Documents numbered as an outline - "3.", "3.2", and (with letters=True) "a)" items under a paragraph - as in
    DGCA's Civil Aviation Requirements. Cited as "Para 3.2" / "Para 3(a)". Numbering must move forward, so a wrapped
    line that happens to start with "1.5" inside para 3.4 is not taken as a heading."""
    d = DOCS[doc_id]
    unit, sections, cur, last, tops = d["unit"], [], None, (0, 0, ""), {}
    for ln in doc_text(doc_id).splitlines():
        m2 = re.match(r"^(\d{1,2})\.(\d{1,2})\.?\s+(.*)$", ln)
        m1 = None if m2 else re.match(r"^(\d{1,2})\.\s+(.*)$", ln)
        # lettered items are paragraphs of their own only under a bare heading ("3. Requirements"), not a list
        # inside a sentence ("1.1 ... the following categories: (a) ... (b) ...")
        ml = re.match(r"^\(?([a-z])\)\s*(.*)$", ln) if d.get("letters") and cur and (
            last[2] or len(" ".join(cur["lines"])) < 40) else None
        key = rest = None
        if m2:
            key, rest = (int(m2[1]), int(m2[2]), ""), m2[3]
        elif m1:
            key, rest = (int(m1[1]), 0, ""), m1[2]
        elif ml:
            want = "a" if not last[2] else chr(ord(last[2]) + 1)
            if ml[1] == want:
                key, rest = (last[0], last[1], ml[1]), ml[2]
        ok = key and key > last and (key[0] == last[0] or (key[0] == last[0] + 1 and key[1] <= 1)) \
            and (key[0] != last[0] or key[2] or key[1] <= last[1] + 2)
        if ok:
            if key[1] == 0 and not key[2]:
                tops[key[0]] = rest.strip(" .")[:90].title() if rest.isupper() else _title(rest)
            ref = f"{key[0]}" + (f".{key[1]}" if key[1] else "") + (f"({key[2]})" if key[2] else "")
            short = len(rest) < 60 and not rest.rstrip().endswith((".", ",", ";"))
            cur = {"ref": f"{unit} {ref}", "title": rest.strip(" .") if short and not key[2] and rest else
                   tops.get(key[0], ""), "lines": [ln]}
            sections.append(cur)
            last = key
        elif cur:
            cur["lines"].append(ln)
    chunks = []
    for s in sections:
        body = re.sub(r"\s+", " ", " ".join(s["lines"])).strip()
        if len(body) < 40:  # a bare heading ("3. REQUIREMENTS"): its title is carried by its paragraphs
            continue
        for i, piece in enumerate(_split_long(body, max_chars)):
            ref = s["ref"] + (" (contd.)" if i else "")
            chunks.append({"doc_id": doc_id, "doc_title": d["title"], "ref": ref, "title": s["title"],
                           "citation": f"{d['title']}, {ref}", "text": piece, "source_url": d["source_url"]})
    return chunks


def chunk_doc(doc_id: str, max_chars: int = 1800) -> list[dict]:
    """Split a document by its numbered sections (monotonic numbering), then by sub-sections if long."""
    d = DOCS[doc_id]
    if d.get("outline"):
        return chunk_outline(doc_id, max_chars)
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
        groups = [piece for g in groups for piece in _split_long(g, max_chars)]
        prev_ref = None
        for g in groups:
            ref = label
            subs = [int(x) for x in re.findall(r"\((\d{1,3})\)\s", g)]
            if len(groups) > 1 and subs and d.get("subrefs", True):
                seq = [subs[0]]  # top-level sub-sections run consecutively; inline cross-refs don't
                for x in subs[1:]:
                    if x == seq[-1] + 1:
                        seq.append(x)
                ref = f"{label}({seq[0]})" if len(seq) == 1 else f"{label}({seq[0]})–({seq[-1]})"
            if prev_ref and (ref == prev_ref or (len(groups) > 1 and (not subs or not d.get("subrefs", True)))):
                ref = f"{prev_ref} (contd.)"
            prev_ref = ref.replace(" (contd.)", "")
            chunks.append({"doc_id": doc_id, "doc_title": d["title"], "ref": ref, "title": s["title"],
                           "citation": f"{d['title']}, {ref}", "text": g,
                           "source_url": d["source_url"]})
    return chunks


# --------------------------------------------------------------------------- Qdrant

@lru_cache(maxsize=1)
def embedder():
    from fastembed import TextEmbedding

    from saathi import timing

    with timing.span("init", f"load embedding model {EMBED_MODEL} (once per process)"):
        return TextEmbedding(EMBED_MODEL)


@lru_cache(maxsize=1)
def client():
    from saathi import timing

    with timing.span("init", "connect to Qdrant (once per process)") as rec:
        c = _client()
        rec["label"] += f" → {client.mode}"
        return c


def _client():
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


SECTION_HEAD = re.compile(r"\b([A-Z])\.(\d{1,2})(?:\.\d{1,2})*\s+([A-Z][A-Za-z][^.;:]{3,80})")


def annotate(doc_id: str, chunks: list[dict]) -> list[dict]:
    """Add what retrieval needs beyond the text: `seq` (position in the document), `group` (the section a chunk
    belongs to, so a good hit can pull in the rest of its rules) and the document's `effective_from` /
    `effective_for` (event: the date of the transaction / incident; complaint: the date the complaint is made).

    Groups: RBI directions mark sections inline - "D.1 Strengthening…", "D.2 Reporting…" - and each heading sits at
    the end of the paragraph BEFORE the section it opens, so a chunk belongs to the last heading seen before it
    (section D = Paras 64-76). Elsewhere a group is the parent Section / Rule / Para: "Rule 4(1)-(6)" and "Rule
    4(7)-(10)" are one group, so are "Para 3(a)" … "Para 3(m)"."""
    d = DOCS[doc_id]
    letter, n, title = None, 0, ""
    for i, c in enumerate(chunks):
        c["seq"] = i
        if d.get("sections") == "lettered":
            c["group"], c["group_title"] = f"{doc_id}:{n}", title
            heads = list(SECTION_HEAD.finditer(c["text"]))
            if heads and heads[-1].group(1) != letter:  # the next chunk opens a new lettered section
                letter, n = heads[-1].group(1), n + 1
                title = f"{heads[-1].group(1)}.{heads[-1].group(2)} {heads[-1].group(3).strip()}"
        else:
            base = re.match(r"\w+ \d+[A-Z]?(?:\.\d+)?", c["ref"])
            c["group"], c["group_title"] = f"{doc_id}:{base.group(0) if base else c['ref']}", c.get("title", "")
        c["effective_from"], c["effective_for"] = d.get("effective_from", ""), d.get("effective_for", "")
    return chunks


def build_index() -> dict:
    from qdrant_client.models import Distance, PayloadSchemaType, PointStruct, VectorParams

    chunks = [c for doc_id in DOCS if (LEGAL_DIR / DOCS[doc_id]["file"]).exists()
              for c in annotate(doc_id, chunk_doc(doc_id))]
    vecs = [list(map(float, v)) for v in embedder().passage_embed([f"{c['citation']}: {c['text']}" for c in chunks], batch_size=32)]
    c = client()
    if c.collection_exists(QDRANT_COLLECTION):
        c.delete_collection(QDRANT_COLLECTION)
    c.create_collection(QDRANT_COLLECTION, vectors_config=VectorParams(size=len(vecs[0]), distance=Distance.COSINE))
    try:
        c.create_payload_index(QDRANT_COLLECTION, "doc_id", PayloadSchemaType.KEYWORD)
    except Exception:
        pass  # embedded mode ignores payload indexes
    points = [PointStruct(id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ch['citation']}|{i}")), vector=v, payload=ch)
              for i, (ch, v) in enumerate(zip(chunks, vecs))]
    for start in range(0, len(points), 128):  # small requests: the client's 3 s timeout is sized for searches
        c.upsert(QDRANT_COLLECTION, points=points[start:start + 128])
    retrieve.cache_clear()
    all_chunks.cache_clear()
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


@lru_cache(maxsize=1)
def all_chunks() -> tuple[dict, ...]:
    """Every indexed chunk (payload only), in document order - for keyword search and section expansion."""
    ensure_index()
    out, offset = [], None
    while True:
        batch, offset = client().scroll(QDRANT_COLLECTION, limit=256, offset=offset, with_payload=True,
                                        with_vectors=False)
        out += [p.payload for p in batch]
        if offset is None:
            break
    return tuple(sorted(out, key=lambda c: (list(DOCS).index(c["doc_id"]) if c["doc_id"] in DOCS else 99,
                                            c.get("seq", 0))))


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
