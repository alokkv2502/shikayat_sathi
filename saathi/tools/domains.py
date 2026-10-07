"""Official-domain resolution and matching (tldextract-based registrable domains)."""
from __future__ import annotations

import re

import tldextract

from saathi.tools.fetch import fetch
from saathi.tools.serp import SerpClient

GOOGLE = {"gl": "in", "hl": "en", "google_domain": "google.co.in"}

# Bundled public-suffix snapshot (no network); .bank.in / .fin.in are India's regulated-entity domains.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), extra_suffixes=["bank.in", "fin.in"])


def registrable(url_or_host: str | None) -> str:
    """'https://www.support.hdfc.bank.in/x' -> 'hdfc.bank.in'; '' if unparseable."""
    if not url_or_host:
        return ""
    r = _EXTRACT(url_or_host.strip().lower())
    reg = r.top_domain_under_public_suffix if hasattr(r, "top_domain_under_public_suffix") else r.registered_domain
    if not reg:  # unknown TLD (e.g. test domains): fall back to the last two host labels
        h = host(url_or_host)
        reg = ".".join(h.split(".")[-2:]) if "." in h else h
    return reg


def host(url: str | None) -> str:
    u = re.sub(r"^[a-z]+://", "", (url or "").strip().lower()).split("/")[0].split(":")[0]
    return u[4:] if u.startswith("www.") else u


# Domains that are never a company's own site: social, reference, directories, complaint boards, app stores.
NOT_OFFICIAL = {
    "wikipedia.org", "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
    "google.com", "apple.com", "justdial.com", "glassdoor.co.in", "glassdoor.com", "ambitionbox.com", "crunchbase.com",
    "zaubacorp.com", "tofler.in", "quora.com", "reddit.com", "consumercomplaints.in", "mouthshut.com",
    "voxya.com", "akosha.com", "indiankanoon.org", "economictimes.com", "indiatimes.com", "livemint.com",
    "moneycontrol.com", "ndtv.com", "hindustantimes.com", "thehindu.com", "indianexpress.com", "medium.com",
    "bankbazaar.com", "paisabazaar.com", "policybazaar.com", "trustpilot.com", "scribd.com", "slideshare.net",
}
# Words that make a domain look like an unofficial "helpline" site rather than the brand's own.
HELPLINE_WORDS = re.compile(r"care|helpline|help-?desk|customer|support|toll|number|complain|contact|refund", re.I)


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def brand_match(company: str, domain: str) -> bool:
    """Does the domain's main label look like the company's own brand? ('hdfc.bank.in' ~ 'HDFC Bank')."""
    label = domain.split(".")[0]
    comp = _compact(re.sub(r"\b(ltd|limited|pvt|private|india|inc|co|company|the)\b", "", company.lower()))
    first = _compact(company.split()[0]) if company.split() else ""
    lab = _compact(label)
    return bool(lab) and (lab == comp or lab in comp and len(lab) >= 3 or comp.startswith(lab) and len(lab) >= 3
                          or (len(first) >= 3 and lab.startswith(first)))


def lookalike_domain(company: str, domain: str, official: set[str]) -> bool:
    """A brand-bearing domain that is not official, e.g. 'flipkart-customer-care.co.in' - a scam pattern."""
    if not domain or domain in official:
        return False
    label = domain.split(".")[0]
    first = _compact(company.split()[0]) if company.split() else ""
    return len(first) >= 3 and first in _compact(label) and bool(HELPLINE_WORDS.search(label) or "-" in label)


def resolve_official(serp: SerpClient, company: str, *, hint_urls: list[str] | None = None, live: bool = True) -> dict:
    """Official domain(s) of a company with the basis for believing it."""
    data = serp.search({"engine": "google", "q": f"{company} official website", **GOOGLE}, live=live)
    kg = data.get("knowledge_graph") or {}
    candidates = []
    for r in (data.get("organic_results") or [])[:8]:
        d = registrable(r.get("link"))
        if d and d not in NOT_OFFICIAL:
            candidates.append({"domain": d, "position": r.get("position"), "title": r.get("title"),
                               "brand_match": brand_match(company, d), "helpline_like": bool(HELPLINE_WORDS.search(d))})
    official, basis = None, None
    kg_site = kg.get("website") or (kg.get("profiles") or [{}])[0].get("link") if kg else None
    if kg_site and registrable(kg_site) not in NOT_OFFICIAL:
        official, basis = registrable(kg_site), "Google Knowledge Graph website"
    else:
        for c in candidates[:3]:  # top-3 organic result that carries the brand and doesn't look like a helpline site
            if c["brand_match"] and not c["helpline_like"]:
                official, basis = c["domain"], f"top-{c['position']} result for “{company} official website”, brand match"
                break
    for u in hint_urls or []:  # e.g. the lender website from RBI's DLA directory
        if not official and registrable(u):
            official, basis = registrable(u), "regulator registry (RBI DLA directory)"
    domains = {official} if official else set()
    if official:  # follow redirects: hdfcbank.com -> hdfc.bank.in
        page = fetch(f"https://{official}")
        final = registrable(page.get("final_url"))
        if final and final != official and final not in NOT_OFFICIAL:
            domains.add(final)
    return {"domain": official, "domains": sorted(domains), "basis": basis, "candidates": candidates,
            "knowledge_graph": {k: kg.get(k) for k in ("title", "website", "type") if kg.get(k)}}


