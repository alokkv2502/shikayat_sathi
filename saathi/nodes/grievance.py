"""grievance worker: find the company's official grievance contacts.

SerpApi Google Search (site:<official domain> queries + one open-web query) -> fetch the top pages -> extract
contacts (regex + LLM). The LLM output is never trusted directly: every email / phone / name it returns must
literally appear in the page text, or it is discarded and counted as `blocked_hallucinations`. On a verifier
retry the worker uses alternative phrasings (nodal officer, appellate authority, contact us…).
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from saathi import llm, timing
from saathi.nodes.common import live, node
from saathi.playbooks import SECTORS
from saathi.state import ExtractOut, schema
from saathi.tools.domains import GOOGLE, registrable
from saathi.tools.fetch import FREE_MAIL, deobfuscate, fetch, find_emails, find_phones, norm_email, norm_phone
from saathi.tools.serp import CreditLimitReached, SerpApiError, serp

ROLES = [  # (role, pattern) - most specific first
    ("appellate_authority", r"appellate authority"),
    ("principal_nodal_officer", r"principal nodal officer|pno\b"),
    ("nodal_officer", r"nodal officer"),
    ("grievance_officer", r"grievance (redressal )?officer|grievance officer|\bgro\b|chief grievance"),
    ("grievance_cell", r"grievance|escalat|complaint|redressal"),
    ("customer_care", r"customer (care|service|support)|helpline|toll[- ]free|call us|contact us"),
]
NAME_RE = re.compile(r"\b(?:Mr|Ms|Mrs|Shri|Smt|Dr|Sh)\.?\s+([A-Z][a-zA-Z.]+(?:\s+[A-Z][a-zA-Z.]+){0,3})")
LABEL_NAME_RE = re.compile(r"\bName\s*[:\-]\s*([A-Z][a-zA-Z.]+(?:\s+[A-Z][a-zA-Z.]+){0,3})")
TIMELINE_RE = re.compile(r"within\s+(?:a\s+period\s+of\s+)?(\d+|one|two|three|seven|fifteen|thirty|forty[- ]eight)"
                         r"\s*(?:\(\d+\)\s*)?(working\s+days|business\s+days|days|hours|hrs|weeks|months?)", re.I)

EXTRACT_SYSTEM = (
    "Extract grievance-redressal contacts for the named company from the page text. Copy names, emails and phone "
    "numbers EXACTLY as written; never guess or complete them. Use '' when a field is absent. Include the "
    "grievance officer, nodal / principal nodal officer, appellate authority, and the customer-care contact. "
    "Pages on a company's own website often list OTHER organisations' contacts too - lending partners, banks, "
    "NBFCs, sellers, insurers, group or parent companies (e.g. 'Lending partners: 1. Axis Bank … 3. EarlySalary: "
    "GRO Mr. X'). List every contact on the page with the organisation it belongs to, and set "
    "belongs_to_named_company to true ONLY for the named company's own contacts. When unsure, set it to false."
)


def _role(context: str) -> str:
    for role, pat in ROLES:
        if re.search(pat, context, re.I):
            return role
    return "other"


def _windows(text: str, size: int = 1600) -> str:
    """The parts of a long page around grievance keywords (keeps LLM input small and relevant)."""
    spans = [(max(0, m.start() - size // 2), m.end() + size // 2)
             for m in re.finditer(r"grievance|nodal|appellate|escalat|customer care|contact us|redressal", text, re.I)]
    if not spans:
        return text[:6000]
    merged: list[list[int]] = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return "\n…\n".join(text[a:b] for a, b in merged)[:9000]


def rule_contacts(text: str, mailtos: list[str] | None = None, tels: list[str] | None = None) -> list[dict]:
    """Every email/phone on the page with role, nearby name and timeline from its context."""
    out = []
    flat = re.sub(r"\s+", " ", text)
    values = [("email", e) for e in sorted(set(find_emails(flat)) | {norm_email(m) for m in mailtos or []})]
    values += [("phone", p) for p in sorted(set(find_phones(flat)) | {norm_phone(t) for t in tels or [] if t})]
    for kind, v in values:
        if kind == "email":
            pos = flat.lower().find(v)
        else:
            digits = re.sub(r"\D", "", flat)
            # locate the number in the original text by its first digits
            m = re.search(r"[\s-]?".join(v[:6]), flat)
            pos = m.start() if m else -1
            if v not in digits:
                continue
        ctx = flat[max(0, pos - 300): pos + 150] if pos >= 0 else ""
        before = flat[max(0, pos - 300): pos] if pos >= 0 else ""
        names = NAME_RE.findall(before) + LABEL_NAME_RE.findall(before)
        tl = TIMELINE_RE.search(ctx)
        out.append({"type": kind, "value": v, "role": _role(before[-220:] or ctx), "name": names[-1] if names else "",
                    "timeline": tl.group(0) if tl else "", "evidence": ctx.strip()[-400:]})
    return out


def llm_contacts(company: str, page: dict) -> tuple[list[dict], int, bool]:
    """LLM extraction, then literal verification against the page. Returns (contacts, blocked_count, llm_ran)."""
    text = page.get("text") or ""
    win = _windows(text)
    res = llm.json_call(EXTRACT_SYSTEM, f"Company: {company}\nPage: {page.get('final_url')}\n\n{win}",
                        schema(ExtractOut))
    llm.validated(ExtractOut, res)
    flat = re.sub(r"\s+", " ", deobfuscate(text)).lower()
    digits = re.sub(r"\D", "", flat)
    out, blocked = [], 0
    for c in res.get("contacts", []):
        name = c.get("name", "").strip()
        if name and name.lower() not in flat:
            blocked += 1
            name = ""
        for kind, raw in (("email", c.get("email", "")), ("phone", c.get("phone", ""))):
            if not raw:
                continue
            v = norm_email(raw) if kind == "email" else norm_phone(raw)
            ok = (v in flat) if kind == "email" else (len(v) >= 3 and v in digits)
            if not ok:
                blocked += 1  # the model produced a value that is not on the page
                continue
            out.append({"type": kind, "value": v, "role": c.get("role") or "other", "name": name,
                        "designation": c.get("designation", ""), "timeline": c.get("response_timeline", ""),
                        "evidence": c.get("evidence_quote", "")[:400],
                        "own": bool(c.get("belongs_to_named_company")),
                        "organisation": (c.get("organisation") or "").strip()})
    return out, blocked, True


def extract_from_pages(company: str, results: list[dict], *, max_pages: int = 8) -> dict:
    """Fetch the most promising result pages and extract contact observations from pages + snippets."""
    ranked = sorted(results, key=lambda r: (not r["on_official"], r.get("position") or 99))[:max_pages]
    with ThreadPoolExecutor(max_workers=6) as ex:
        pages = list(ex.map(timing.carry(lambda r: (r, fetch(r["url"]))), ranked))
    # Rules first. The LLM only adds role / name context to values the page itself contains (anything else is
    # blocked), so it runs only on pages where the rules found a contact AND the page is official or names the
    # company - and those calls run in parallel.
    rules = [rule_contacts(p["text"], p.get("mailtos"), p.get("tels")) if p.get("ok") else [] for _, p in pages]
    brand = company.casefold().split()[0] if company.split() else ""
    worth = [i for i, (r, p) in enumerate(pages)
             if rules[i] and (r["on_official"] or (brand and brand in p["text"].casefold()))]
    with ThreadPoolExecutor(max_workers=6) as ex:
        llm_res = dict(zip(worth, ex.map(timing.carry(lambda i: llm_contacts(company, pages[i][1])), worth)))
    llm_out = {i: (r[0], r[1]) for i, r in llm_res.items()}
    llm_ok = {i for i, r in llm_res.items() if r[2]}  # pages the LLM actually read (not a failed / disabled call)
    observations, page_log, blocked, other_org = [], [], 0, []
    for i, (r, page) in enumerate(pages):
        src = {"source_url": page.get("final_url") or r["url"], "source_domain": registrable(page.get("final_url")
                                                                                            or r["url"]),
               "page_title": page.get("title") or r["title"]}
        found, foreign = [], set()
        if page.get("ok"):
            found = rules[i]
            lc, b = llm_out.get(i, ([], 0))
            blocked += b
            by_val = {(f["type"], f["value"]): f for f in found}
            for c in lc:  # LLM adds role/name/designation context for values the page really contains
                k = (c["type"], c["value"])
                by_val[k] = {**by_val.get(k, {}), **{x: y for x, y in c.items() if y}}
            # A company's own page can list partners' / lenders' / sellers' officers: drop every value the LLM
            # attributes to another organisation (unless it also lists it as the company's own).
            own = {(c["type"], c["value"]) for c in lc if c["own"]}
            foreign = {(c["type"], c["value"]) for c in lc if not c["own"]} - own
            other_org += [{"value": c["value"], "organisation": c["organisation"], "url": src["source_url"]}
                          for c in lc if (c["type"], c["value"]) in foreign]
            found = [f for k, f in by_val.items() if k not in foreign]
            if i in llm_ok:  # the LLM read this page: what it did not call the company's own has an unknown owner
                for f in found:
                    f["owner"] = "company" if (f["type"], f["value"]) in own else "unknown"
        for f in found:
            observations.append({**f, **src, "via": "page"})
        # Search snippets are Google's excerpt of that page - weaker evidence, but still tied to its domain.
        for kind, vals in (("email", find_emails(deobfuscate(r["snippet"]))), ("phone", find_phones(r["snippet"]))):
            for v in vals:
                if (kind, v) in foreign:
                    continue
                if not any(o["value"] == v and o["source_url"] == src["source_url"] for o in observations):
                    observations.append({"type": kind, "value": v, "role": _role(r["snippet"]), "name": "",
                                         "timeline": "", "evidence": r["snippet"], **src, "via": "snippet"})
        page_log.append({"url": r["url"], "final_url": page.get("final_url"), "ok": page.get("ok"),
                         "error": page.get("error"), "on_official": r["on_official"], "found": len(found)})
    return {"observations": observations, "pages": page_log, "blocked_hallucinations": blocked,
            "other_org": other_org, "method": llm.label()}


def is_free_mail(email: str) -> bool:
    return email.split("@")[-1] in FREE_MAIL


# --------------------------------------------------------------------------- node

def search_results(company: str, queries: list[str], official: list[str], *,
                   live_ok: bool) -> tuple[list[dict], list[str]]:
    """Results of every query that succeeded; a failed/over-budget query is reported, not fatal."""
    out, seen, errors = [], set(), []
    for q in queries:
        try:
            data = serp.search({"engine": "google", "q": q, **GOOGLE}, live=live_ok)
        except (SerpApiError, CreditLimitReached) as e:
            errors.append(f"grievance: {q}: {e}")
            continue
        for r in (data.get("organic_results") or [])[:8]:
            link = r.get("link")
            if not link or link in seen:
                continue
            seen.add(link)
            d = registrable(link)
            out.append({"query": q, "url": link, "title": r.get("title") or "", "snippet": r.get("snippet") or "",
                        "domain": d, "on_official": d in official, "position": r.get("position")})
    return out, errors


def to_contact(o: dict, official: list[str]) -> dict:
    """Observation -> the brief's contact schema (internal verifier keys kept alongside)."""
    st = "registry" if o.get("via") == "registry" else (
        "official_domain" if o.get("source_domain") in official else "third_party")
    return {**o, "email": o["value"] if o["type"] == "email" else "", "phone": o["value"] if o["type"] == "phone"
            else "", "url": o.get("source_url"), "source_type": st, "verified": False, "reason": ""}


@node("🔎 Grievance search")
def grievance(state: dict) -> dict:
    company, cfg = state["company"], SECTORS[state["sector"]]
    official = state.get("official_domains") or []
    d = state.get("company_domain")
    attempt = state.get("grievance_attempts", 0)
    if attempt == 0:  # site: queries on the official domain first, plus one open-web query
        queries = [f"site:{d} {t}" for t in cfg["officer_terms"]] if d else []
        queries.append(f"{company} {cfg['officer_terms'][0]} email")
    else:             # verifier asked for a retry: alternative phrasing
        queries = [f"site:{d} {t}" for t in cfg["retry_terms"][:2]] if d else \
            [f"{company} {t} email" for t in cfg["retry_terms"][:2]]
    results, errors = search_results(company, queries, official, live_ok=live(state))
    ext = extract_from_pages(company, results)
    contacts = [to_contact(o, official) for o in ext["observations"]]
    n_off = sum(c["source_type"] == "official_domain" for c in contacts)
    label = "retry" if attempt else "search"
    return {"contacts": contacts, "pages": ext["pages"], "blocked_hallucinations": ext["blocked_hallucinations"],
            "grievance_attempts": attempt + 1, "errors": errors,
            "trace": [f"🔎 Grievance {label} #{attempt + 1}: {len(queries)} queries ({'; '.join(queries)}) → "
                      f"{len(results)} results, {len(ext['pages'])} pages read, {len(contacts)} contact mentions "
                      f"({n_off} on the official domain)"
                      + (f"; blocked {ext['blocked_hallucinations']} LLM values not on the page"
                         if ext["blocked_hallucinations"] else "")
                      + (f"; dropped {len(ext['other_org'])} contact(s) of other organisations listed on the page ("
                         + ", ".join(sorted({o['organisation'] or '?' for o in ext['other_org']}))[:120] + ")"
                         if ext["other_org"] else "")
                      + (f"; {len(errors)} query(ies) failed" if errors else "")]}
