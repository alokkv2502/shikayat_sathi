"""verifier (fan-in point; rules, no LLM): a contact is shown as VERIFIED only if
   (a) it appears on a page of the company's own official domain, or
   (b) it comes from a regulator's registry (RBI's DLA directory via LoanLens), or
   (c) it is confirmed by two independent sources (different registrable domains, none of them a
       brand-lookalike "helpline" site).
Everything else is UNVERIFIED. Phone numbers seen only on third-party sites carry a scam warning; a free
webmail address (gmail…) for a company can never be verified by corroboration alone.
"""
from __future__ import annotations

from saathi.nodes.common import live, node
from saathi.playbooks import SECTORS
from saathi.tools.domains import brand_match, lookalike_domain, registrable
from saathi.tools.fetch import FREE_MAIL
from saathi.tools.serp import serp

ROLE_RANK = {"grievance_officer": 0, "principal_nodal_officer": 1, "nodal_officer": 2, "appellate_authority": 3,
             "grievance_cell": 4, "customer_care": 5, "other": 6}
EMERGENCY = {"1915", "14448", "155255", "1930", "198", "112", "100"}  # public helplines, never "company" contacts


def verify(company: str, observations: list[dict], official_domains: list[str]) -> list[dict]:
    official = set(official_domains)
    groups: dict[tuple, list[dict]] = {}
    for o in observations:
        if o["type"] == "phone" and (o["value"] in EMERGENCY or not 8 <= len(o["value"]) <= 13):
            continue  # too short, or two numbers run together by the page text (no Indian number has > 13 digits)
        groups.setdefault((o["type"], o["value"]), []).append(o)

    out = []
    for (kind, value), obs in groups.items():
        domains = {o["source_domain"] for o in obs if o["source_domain"]}
        on_official = sorted({o["source_url"] for o in obs if o["source_domain"] in official and o["via"] == "page"})
        on_official_snip = sorted({o["source_url"] for o in obs if o["source_domain"] in official})
        from_registry = [o for o in obs if o["via"] == "registry"]
        lookalikes = sorted(d for d in domains if lookalike_domain(company, d, official))
        independent = sorted(d for d in domains if d not in official and d not in lookalikes)
        warnings, checks = [], []
        email_domain = value.split("@")[-1] if kind == "email" else ""

        # On official pages the LLM read, it labels whose contact each value is ("company" / "unknown"); pages
        # often list partners' / lenders' officers too. Unknown owner + nothing tying the value to the company
        # (an off-brand email, or a bare phone number) is not shown as the company's verified contact.
        official_obs = [o for o in obs if o["source_domain"] in official]
        owner_unknown = bool(official_obs) and all(o.get("owner") == "unknown" for o in official_obs)
        on_brand = kind == "email" and (email_domain in official or any(email_domain.endswith("." + d)
                                                                        for d in official)
                                        or brand_match(company, registrable(email_domain)))
        if from_registry:
            status, basis = "verified", "regulator registry: " + from_registry[0]["source_domain"]
        elif owner_unknown and not on_brand and len(independent) < 2:
            status, basis = "unverified", ("listed on the official site, but not identified as the company's own "
                                           "contact (the page also lists other organisations)")
        elif on_official:
            status, basis = "verified", f"found on official domain ({', '.join(sorted(official))})"
        elif on_official_snip:
            status, basis = "verified", "in Google's excerpt of an official-domain page"
        elif len(independent) >= 2 and email_domain not in FREE_MAIL:
            status, basis = "verified", f"confirmed by {len(independent)} independent sources"
        else:
            status, basis = "unverified", (f"only {len(independent)} independent source(s); not on the official site"
                                           if independent or lookalikes else "no supporting source")
        checks.append(f"sources: {', '.join(sorted(domains)) or '-'}")
        off_brand = False
        if kind == "email":
            if email_domain in official or any(email_domain.endswith("." + d) for d in official):
                checks.append("email uses the official domain")
            elif brand_match(company, registrable(email_domain)):
                checks.append(f"email domain {email_domain} carries the company's brand")
            elif email_domain not in FREE_MAIL:
                # e.g. a lending partner's officer listed on the company's own page. Genuine cases exist too
                # (Vi -> vodafoneidea.com), so this warns and ranks it last for "send to" rather than rejecting.
                off_brand = True
                warnings.append(f"the address is on {email_domain}, not the company's own domain - check it is "
                                f"{company}'s contact and not another organisation listed on the page")
            if email_domain in FREE_MAIL:
                warnings.append("free webmail address - companies don't handle grievances from gmail/yahoo")
                if status == "verified" and not (on_official or from_registry):
                    status, basis = "unverified", "free webmail address seen only off the official site"
            elif status != "verified" and not off_brand:
                warnings.append(f"email domain {email_domain} is not the company's official domain")
        if lookalikes:
            warnings.append(f"seen on look-alike domain(s) {', '.join(lookalikes)} - a common fake-helpline pattern")
        if kind == "phone" and status == "unverified":
            warnings.append("fake customer-care numbers are a common scam: call only numbers from the official site")

        best = sorted(obs, key=lambda o: (o["source_domain"] not in official, ROLE_RANK.get(o.get("role"), 6)))[0]
        names = [o.get("name") for o in obs if o.get("name")]
        name_ok = [o.get("name") for o in obs if o.get("name") and (o["source_domain"] in official or
                                                                      o["via"] == "registry")]
        source_type = "registry" if from_registry else ("official_domain" if on_official or on_official_snip
                                                        else "third_party")
        out.append({
            "email": value if kind == "email" else "", "phone": value if kind == "phone" else "",
            "url": best.get("source_url"), "source_url": best.get("source_url"), "source_type": source_type,
            "verified": status == "verified", "reason": basis,
            "type": kind, "value": value, "role": best.get("role") or "other",
            "name": (name_ok or names or [""])[0], "name_verified": bool(name_ok),
            "designation": best.get("designation", ""), "timeline": next((o["timeline"] for o in obs if o.get("timeline")), ""),
            "status": status, "basis": basis, "warnings": warnings, "checks": checks, "off_brand": off_brand,
            "sources": sorted({o["source_url"] for o in obs}), "evidence": best.get("evidence", ""),
        })
    out.sort(key=lambda c: (c["status"] != "verified", ROLE_RANK.get(c["role"], 6), c["type"] != "email"))
    return out


def recommended(contacts: list[dict]) -> dict | None:
    """The verified grievance contact to address the complaint to (email preferred). An address on another
    organisation's domain (e.g. a partner listed on the company's page) is used only if nothing else is verified."""
    emails = [c for c in contacts if c["status"] == "verified" and c["type"] == "email"]
    for pool in ([c for c in emails if not c.get("off_brand")], emails):
        for c in pool:
            if ROLE_RANK.get(c["role"], 6) <= 4:
                return c
        if pool:
            return pool[0]
    return None


@node("🛡️ Verifier")
def verifier(state: dict) -> dict:
    contacts = verify(state.get("company") or "", state.get("contacts") or [], state.get("official_domains") or [])
    ver = [c for c in contacts if c["status"] == "verified"]
    unv = [c for c in contacts if c["status"] != "verified"]
    rec = recommended(contacts)
    attempts = state.get("verify_attempts", 0) + 1
    if rec:
        msg = f"✅ {rec['value']} verified ({rec['reason']})"
    elif not state.get("company"):
        msg = "no company identified - nothing to verify"
    elif should_retry({**state, "verify_attempts": attempts, "recommended": None}):
        terms = SECTORS[state["sector"]]["retry_terms"][:2]
        why = (f"{len(unv)} contact(s) found only on third-party sites" if unv else "no grievance contact found")
        msg = f"{why} — retrying search with “{'”, “'.join(terms)}”"
    else:
        msg = f"no verified grievance email ({len(unv)} unverified contact(s) kept aside)"
    return {"verified_contacts": ver, "unverified_contacts": unv, "recommended": rec, "verify_attempts": attempts,
            "trace": [f"🛡️ Verifier #{attempts}: {len(ver)} verified, {len(unv)} unverified — {msg}"]}


def should_retry(state: dict) -> bool:
    """Retry the grievance search (alternative phrasing) at most once: verify_attempts is capped at 2."""
    return (state.get("sector") != "govt_scheme" and not state.get("recommended") and bool(state.get("company"))
            and state.get("verify_attempts", 0) < 2
            and live(state) and bool(serp.api_key)
            and not (state.get("registry") or {}).get("status") == "listed")


def route_after_verify(state: dict) -> str:
    return "grievance" if should_retry(state) else "legal_rag"
