"""registry worker (loan_app only): is the app in RBI's Digital Lending App directory, which lenders declared it,
and who are their Grievance Redressal Officers? Uses the LoanLens registry (sibling project, LOANLENS_DIR).

GRO contacts come from RBI's own file, so they enter with source_type="registry" (verified). Apps that are
not_listed / lookalike are flagged: their escalation is cyber-crime reporting, not the company.

LoanLens has its own `config` / `matcher` modules, so they are imported in isolation and restored afterwards.
"""
from __future__ import annotations

import sys
from functools import lru_cache

from config import LOANLENS_DIR
from saathi.nodes.common import node

_SHARED = ("config", "build_registry", "matcher")


@lru_cache(maxsize=1)
def _registry():
    if not (LOANLENS_DIR / "data" / "registry.db").exists():
        return None
    saved = {k: sys.modules.pop(k) for k in _SHARED if k in sys.modules}
    sys.path.insert(0, str(LOANLENS_DIR))
    try:
        import matcher as ll_matcher  # noqa: F401 - LoanLens' matcher (binds LoanLens' own config)

        reg = ll_matcher.Registry(LOANLENS_DIR / "data" / "registry.db")
    finally:
        sys.path.remove(str(LOANLENS_DIR))
        for k in _SHARED:
            sys.modules.pop(k, None)
        sys.modules.update(saved)
    return reg


def available() -> bool:
    return _registry() is not None


def check_app(name: str, package_id: str | None = None) -> dict:
    """{status, match_via, lenders: [...], contacts: [observation...], lookalike_of, notes}"""
    reg = _registry()
    if reg is None:
        return {"status": "unavailable", "notes": [f"LoanLens registry not found at {LOANLENS_DIR}"]}
    r = reg.check_app(package_id=package_id, app_name=name)
    listings = r.get("matches") or []
    obs = []
    for m in listings[:5]:
        g = m["grievance_officer"]
        base = {"role": "grievance_officer", "name": g.get("name") or "", "designation": "Grievance Redressal "
                "Officer (declared to RBI)", "timeline": "", "evidence": f"RBI DLA directory: {m['dla_name']} — "
                f"lender {m['lender']}", "source_url": "https://sachet.rbi.org.in/", "source_domain":
                "RBI DLA directory", "page_title": "RBI Digital Lending Apps directory", "via": "registry"}
        if g.get("email"):
            obs.append({**base, "type": "email", "value": g["email"].strip().lower()})
        for p in {g.get("mobile"), g.get("phone")} - {None, ""}:
            from saathi.tools.fetch import norm_phone
            obs.append({**base, "type": "phone", "value": norm_phone(str(p))})
    import re

    from saathi.tools.domains import registrable
    stores = {"google.com", "apple.com", "indusapp.store", "mi.com", "samsung.com", "amazon.in", "onelink.me"}
    app_sites = sorted({d for m in listings for raw in m.get("links", []) for d in map(registrable,
                        re.findall(r"https?://[^\s,]+", raw)) if "." in d} - stores)
    return {"status": r["status"], "match_via": r.get("match_via"), "app_domains": app_sites,
            "lenders": [{"lender": m["lender"], "lender_status": m.get("lender_status"), "dla_name": m["dla_name"],
                         "website": m.get("lender_website")} for m in listings[:8]],
            "lookalike_of": [{"dla_name": m["dla_name"], "lender": m["lender"]} for m in r.get("lookalike_of") or []][:3],
            "notes": r.get("notes") or [], "contacts": obs, "data_as_on": reg.meta.get("data_as_on")}


@node("🏛️ Registry")
def registry(state: dict) -> dict:
    r = check_app(state["company"])
    contacts = [{**o, "email": o["value"] if o["type"] == "email" else "", "phone": o["value"] if o["type"] == "phone"
                 else "", "url": o["source_url"], "source_type": "registry", "verified": False, "reason": ""}
                for o in r.get("contacts", [])]
    status = r.get("status")
    msg = {"listed": f"listed in RBI's DLA directory — {len(r.get('lenders', []))} lender(s), "
                     f"{len(contacts)} GRO contact(s)",
           "lookalike": "LOOKALIKE of a listed app — not the real app; escalate to cyber crime",
           "not_listed": "NOT in RBI's DLA directory — escalate to Sachet / cyber crime",
           "possible_match": "possible match only — confirm the Play Store package ID"}.get(status, status)
    return {"registry": {k: v for k, v in r.items() if k != "contacts"}, "contacts": contacts,
            "trace": [f"🏛️ Registry (LoanLens): {state['company']} → {msg}"]}
