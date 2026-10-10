"""Domain playbooks: a VERIFIED CACHE of complaint routes, not a closed list.

Seed playbooks below were checked against the regulators' own texts (raw/legal/) and portals on 2026-10-07. When a
complaint fits none of them, research mode (nodes/route_research.py) finds the official route from sources and,
once its code-level checks pass, saves it to data/playbooks/<key>.json - from then on it is matched like a seed one.

Each escalation step: {name, channel, url, phone, when_to_use, typical_wait, source}. Every step must carry a
source (seed steps: this file; learned steps: the official page that states them). Legal citations in a draft
never come from here - only from retrieved clauses.
"""
from __future__ import annotations

import json
import re
from datetime import date

from config import DATA_DIR

PLAYBOOK_DIR = DATA_DIR / "playbooks"
PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
SEED_SOURCE = "Shikayat Saathi verified playbook (checked against the regulators' texts, 2026-10-07)"

SECTORS: dict[str, dict] = {
    "ecommerce": {
        "label": "E-commerce / online shopping",
        "regulator": "Department of Consumer Affairs (National Consumer Helpline) · Consumer Commissions",
        "officer_terms": ["grievance officer", "\"grievance redressal\""],
        "retry_terms": ["nodal officer", "customer care escalation", "contact us"],
        "law_docs": ["ecom", "ecom26", "cpa", "ccr", "gen"],
        "basis_query": "e-commerce grievance officer acknowledge 48 hours redress one month",
        "escalation": [
            {"name": "Company grievance officer", "channel": "email / grievance page",
             "url": None, "phone": None,
             "when_to_use": "First. E-commerce entities must display their grievance officer; the officer must "
                            "acknowledge within 48 hours and redress within one month.",
             "typical_wait": "48 h acknowledgement, 1 month resolution",
             "ack_within": {"n": 48, "unit": "hours"},
             "reply_within": {"n": 1, "unit": "months", "kind": "statutory",
                              "basis": "the grievance officer must acknowledge your complaint within 48 hours and "
                                       "resolve it within one month of receiving it",
                              "source": "Consumer Protection (E-Commerce) Rules, 2020, Rule 4(5)"}},
            {"name": "National Consumer Helpline (NCH)", "channel": "portal / phone / WhatsApp",
             "url": "https://consumerhelpline.gov.in", "phone": "1915 (toll-free) · WhatsApp 8800001915",
             "when_to_use": "If the company does not resolve it in a month. NCH forwards the docket to the company.",
             "typical_wait": "about 30-45 days"},
            {"name": "District Consumer Disputes Redressal Commission", "channel": "online filing (e-Jagriti)",
             "url": "https://e-jagriti.gov.in", "phone": None,
             "when_to_use": "Still unresolved: file a consumer complaint (claims up to ₹50 lakh) within two years "
                            "of the cause of action. e-Jagriti replaced e-Daakhil.", "typical_wait": "months",
             "file_within": {"n": 2, "unit": "years", "from": "incident", "basis": "a consumer complaint must be filed within two years of the cause of action", "source": "Consumer Protection Act, 2019, Section 69(1)"}},
        ],
    },
    "bank": {
        "label": "Bank / NBFC / UPI / credit card",
        "regulator": "Reserve Bank of India - RB-Integrated Ombudsman Scheme, 2026",
        "officer_terms": ["principal nodal officer", "nodal officer"],
        "retry_terms": ["grievance redressal officer", "escalation matrix", "contact us"],
        "law_docs": ["ios", "rbc", "rbc26", "cpa", "ccr"],
        "basis_query": "grounds of complaint act or omission of bank resulting in deficiency in service",
        "escalation": [
            {"name": "Bank grievance cell → Principal Nodal Officer", "channel": "email / bank complaint portal",
             "url": None, "phone": None,
             "when_to_use": "First. Complain in writing and keep the complaint number; escalate to the Principal "
                            "Nodal Officer if unresolved.", "typical_wait": "30 days",
             "reply_within": {"n": 30, "unit": "days", "kind": "statutory", "basis": "if the bank does not reply within 30 days of receiving your complaint, you can go to the RBI Ombudsman", "source": "Reserve Bank – Integrated Ombudsman Scheme, 2026, Clause 10(1)"}},
            {"name": "RBI Ombudsman (RB-IOS, 2026)", "channel": "RBI CMS portal / email",
             "url": "https://cms.rbi.org.in", "phone": "14448 (RBI contact centre)",
             "when_to_use": "No reply within 30 days, or the reply is unsatisfactory. Also crpc@rbi.org.in. No fee.",
             "typical_wait": "weeks"},
            {"name": "District Consumer Commission", "channel": "online filing (e-Jagriti)",
             "url": "https://e-jagriti.gov.in", "phone": None,
             "when_to_use": "Deficiency in banking service is also a consumer dispute.", "typical_wait": "months",
             "file_within": {"n": 2, "unit": "years", "from": "incident", "basis": "a consumer complaint must be filed within two years of the cause of action", "source": "Consumer Protection Act, 2019, Section 69(1)"}},
        ],
        "urgent": "For an unauthorised or fraudulent debit, tell the bank immediately and call the National Cyber "
                  "Crime Helpline 1930 (cybercrime.gov.in) - reporting quickly limits your liability.",
    },
    "telecom": {
        "label": "Mobile / broadband / DTH",
        "regulator": "TRAI - Telecom Consumers Complaint Redressal Regulations, 2012 · DoT",
        "officer_terms": ["appellate authority", "nodal officer"],
        "retry_terms": ["grievance officer", "customer care escalation", "contact us"],
        "law_docs": ["trai", "cpa", "ccr", "gen"],
        "basis_query": "complaint centre docket number time limit three days",
        "escalation": [
            {"name": "Operator complaint centre", "channel": "consumer care number / app",
             "url": None, "phone": "198 (complaints)",
             "when_to_use": "First. Note the docket number you are given.",
             "typical_wait": "TRAI QoS time limit (3 days where none is specified)",
             "reply_within": {"n": 3, "unit": "days", "kind": "statutory", "basis": "where TRAI's quality-of-service rules set no other time limit, a complaint must be addressed within three days", "source": "Telecom Consumers Complaint Redressal Regulations, 2012, Regulation 8"}},
            {"name": "Operator's Appellate Authority", "channel": "email / post / online",
             "url": None, "phone": None,
             "when_to_use": "Not satisfied: appeal within 30 days after the complaint-centre time limit expires.",
             "typical_wait": "per TRAI regulations",
             "file_within": {"n": 30, "unit": "days", "from": "first_reply", "basis": "an appeal must be filed within 30 days after the complaint centre's time limit ends", "source": "Telecom Consumers Complaint Redressal Regulations, 2012, Regulation 9(3)"}},
            {"name": "DoT CPGRAMS / Consumer Commission", "channel": "pgportal.gov.in / e-Jagriti",
             "url": "https://pgportal.gov.in", "phone": None,
             "when_to_use": "TRAI does not resolve individual complaints; escalate to DoT or file a consumer "
                            "complaint.", "typical_wait": "weeks to months"},
        ],
    },
    "insurance": {
        "label": "Insurance",
        "regulator": "IRDAI (Bima Bharosa) · Insurance Ombudsman (Insurance Ombudsman Rules, 2017)",
        "officer_terms": ["grievance redressal officer", "grievance officer"],
        "retry_terms": ["grievance cell", "customer care escalation", "contact us"],
        "law_docs": ["ins", "ins23", "cpa", "ccr"],
        "basis_query": "complaint to insurer representation rejected or no reply within one month",
        "escalation": [
            {"name": "Insurer's Grievance Redressal Officer", "channel": "email / grievance portal",
             "url": None, "phone": None,
             "when_to_use": "First. Quote policy and claim numbers.", "typical_wait": "1 month",
             "reply_within": {"n": 1, "unit": "months", "kind": "statutory", "basis": "if the insurer does not reply within one month of receiving your complaint, you can go to the Insurance Ombudsman", "source": "Insurance Ombudsman Rules, 2017, Rule 14(3)"}},
            {"name": "IRDAI Bima Bharosa", "channel": "portal / email / phone",
             "url": "https://bimabharosa.irdai.gov.in", "phone": "155255 / 1800 4254 732",
             "when_to_use": "Unresolved or unsatisfactory: register on Bima Bharosa (complaints@irdai.gov.in).",
             "typical_wait": "weeks"},
            {"name": "Insurance Ombudsman", "channel": "Council for Insurance Ombudsmen",
             "url": "https://www.cioins.co.in", "phone": None,
             "when_to_use": "Insurer rejected your complaint or did not reply within one month: complain within one "
                            "year (awards up to ₹50 lakh).", "typical_wait": "about 3 months",
             "file_within": {"n": 1, "unit": "years", "from": "first_reply", "basis": "complain to the Ombudsman within one year of the insurer's rejection or of its one-month reply period ending", "source": "Insurance Ombudsman Rules, 2017, Rule 14(3)(b)"}},
        ],
    },
    "loan_app": {
        "label": "Loan app / digital lending",
        "regulator": "Reserve Bank of India (Digital Lending Directions, 2025; RB-IOS, 2026) · Cyber Crime",
        "officer_terms": ["grievance redressal officer", "nodal officer"],
        "retry_terms": ["grievance officer", "contact us"],
        "law_docs": ["dl", "ios", "rbc"],
        "basis_query": "nodal grievance redressal officer digital lending complaints",
        "escalation": [
            {"name": "Lender's Grievance Redressal Officer (from RBI's DLA directory)", "channel": "email / phone",
             "url": None, "phone": None,
             "when_to_use": "If the app is in RBI's Digital Lending App directory, write to the GRO the lender "
                            "declared to RBI.", "typical_wait": "30 days",
             "reply_within": {"n": 30, "unit": "days", "kind": "statutory", "basis": "if the lender does not reply within 30 days of receiving your complaint, you can go to the RBI Ombudsman", "source": "RBI (Digital Lending) Directions, 2025, Para 11"}},
            {"name": "RBI Ombudsman (RB-IOS, 2026)", "channel": "RBI CMS portal",
             "url": "https://cms.rbi.org.in", "phone": "14448",
             "when_to_use": "For apps of RBI-regulated lenders, after 30 days without resolution.",
             "typical_wait": "weeks"},
            {"name": "Cyber Crime / Sachet", "channel": "cybercrime.gov.in · police · sachet.rbi.org.in",
             "url": "https://cybercrime.gov.in", "phone": "1930",
             "when_to_use": "Threats, harassment, misuse of photos/contacts, or an app NOT in RBI's directory "
                            "(report it on sachet.rbi.org.in).", "typical_wait": "immediate"},
        ],
        "urgent": "Harassment, threats or misuse of photos/contacts is a crime: report on cybercrime.gov.in / 1930.",
    },
    "govt_scheme": {
        "label": "Government scheme or public benefit",
        "regulator": "The scheme's administering department",
        "officer_terms": [],
        "retry_terms": [],
        "law_docs": [],
        "basis_query": "",
        "escalation": [
            {"name": "Scheme grievance portal", "channel": "Official portal listed in the case brief",
             "url": None, "phone": None,
             "when_to_use": "First. Use the administering department's official scheme or service grievance portal.",
             "typical_wait": "Follow the portal's acknowledgement and resolution timeline."},
            {"name": "Centralized Public Grievance Redress and Monitoring System (CPGRAMS)",
             "channel": "Department of Administrative Reforms and Public Grievances",
             "url": "https://pgportal.gov.in", "phone": None,
             "when_to_use": "Escalate an unresolved grievance about a central government department or scheme.",
             "typical_wait": "Use the timeline shown on CPGRAMS."},
        ],
    },
    "public_service": {
        "label": "Government department / municipal service / public utility",
        "regulator": "The responsible department · CPGRAMS / state public grievance portal",
        "officer_terms": ["public grievance officer", "grievance redressal officer"],
        "retry_terms": ["nodal officer", "citizen charter", "contact us"],
        "law_docs": [],
        "basis_query": "",
        "escalation": [
            {"name": "Department's public grievance officer / portal", "channel": "official portal / office",
             "url": None, "phone": None,
             "when_to_use": "First. Complain in writing to the office responsible and keep the acknowledgement "
                            "or registration number.", "typical_wait": "per the department's citizen charter"},
            {"name": "CPGRAMS or your state's public grievance portal",
             "channel": "pgportal.gov.in (central and many state departments) / state CM helpline",
             "url": "https://pgportal.gov.in", "phone": None,
             "when_to_use": "No action in the charter's time, or an unsatisfactory reply. Quote your first "
                            "complaint number.", "typical_wait": "Use the timeline shown on the portal."},
        ],
    },
    "police": {
        "label": "Police / crime / cyber fraud",
        "regulator": "Police (Bharatiya Nagarik Suraksha Sanhita, 2023) · National Cyber Crime Reporting Portal",
        "tier": "B",
        "officer_terms": [],
        "retry_terms": [],
        "law_docs": [],
        "basis_query": "",
        # Routing facts (not cited in letters): BNSS 2023 s.173 - information of a cognizable offence, at any police
        # station (zero FIR) or electronically, free copy to the informant; s.173(4) - on refusal, send the
        # substance in writing to the Superintendent of Police; s.175(3) - application to the Magistrate.
        "escalation": [
            {"name": "Station House Officer (SHO), [POLICE STATION]", "channel": "written complaint at the police "
             "station · e-FIR on your state police citizen portal where available", "url": None, "phone": "112",
             "when_to_use": "Give your written complaint at the police station. For a cognizable offence the police "
                            "must register an FIR; you can report at any "
                            "police station (zero FIR) and you are entitled to a free copy. Get a signed and stamped "
                            "receipt for your written complaint.", "typical_wait": "same day"},
            {"name": "Superintendent of Police / Deputy Commissioner of Police", "channel": "complaint in writing "
             "by registered post or in person", "url": None, "phone": None,
             "when_to_use": "If the SHO refuses to register an FIR: send the substance of your complaint in writing "
                            "to the SP/DCP (BNSS, 2023, s.173(4)). Keep the postal receipt.",
             "typical_wait": "about 2 weeks"},
            {"name": "Judicial Magistrate", "channel": "application through a lawyer or the District Legal Services "
             "Authority (free if eligible)", "url": "https://nalsa.gov.in", "phone": "15100",
             "when_to_use": "If the police still do not act: apply to the Magistrate to direct registration and "
                            "investigation (BNSS, 2023, s.175(3)).", "typical_wait": "weeks"},
        ],
        "cyber_step": {"name": "National Cyber Crime Helpline 1930 / cybercrime.gov.in",
                       "channel": "call 1930 · report on cybercrime.gov.in · call your bank to block the account/card",
                       "url": "https://cybercrime.gov.in", "phone": "1930",
                       "when_to_use": "Immediately — within hours if possible, so the money can be traced and frozen. "
                                      "Note the acknowledgement number and quote it in your police complaint.",
                       "typical_wait": "immediate"},
        "urgent": "Money lost to fraud? Call 1930 and your bank right now - the first hours matter most for "
                  "freezing the money.",
    },
    "legal": {
        "label": "Court case / property / family matter",
        "regulator": "Courts · District Legal Services Authority (free legal aid)",
        "tier": "C",
        "officer_terms": [],
        "retry_terms": [],
        "law_docs": [],
        "basis_query": "",
        "escalation": [
            {"name": "District Legal Services Authority (DLSA)", "channel": "DLSA office at your district court · "
             "NALSA legal-aid helpline", "url": "https://nalsa.gov.in", "phone": "15100",
             "when_to_use": "First. Free legal advice, and a free lawyer if you are eligible; DLSA also runs Lok "
                            "Adalats for settling disputes without a trial.", "typical_wait": "a few days"},
            {"name": "eCourts case status", "channel": "eCourts Services website / app",
             "url": "https://services.ecourts.gov.in", "phone": None,
             "when_to_use": "If you already have a case: check its status, next hearing date and orders with the "
                            "16-character CNR number, or with the court, case type, number and year.",
             "typical_wait": "instant"},
        ],
        # Legal Services Authorities Act, 1987, s.12: entitled to free legal services if ANY one applies.
        "eligibility": {"mode": "any", "label": "free legal aid", "conditions": [
            "You are a woman or a child.",
            "You belong to a Scheduled Caste or Scheduled Tribe.",
            "You are a person with a disability.",
            "You are an industrial workman.",
            "You are a victim of trafficking or forced labour (begar).",
            "You are a victim of a mass disaster, ethnic or caste violence, flood, drought, earthquake or industrial "
            "disaster.",
            "You are in custody (including a protective home, juvenile home or psychiatric hospital).",
            "Your annual income is below the limit your state has set for free legal aid.",
        ]},
    },
    "other": {
        "label": "Other consumer services (travel, edtech, real estate…)",
        "regulator": "Department of Consumer Affairs (National Consumer Helpline) · Consumer Commissions",
        "officer_terms": ["grievance officer", "customer care"],
        "retry_terms": ["nodal officer", "escalation", "contact us"],
        "law_docs": ["cpa", "ccr", "gen"],
        "basis_query": "deficiency means any fault imperfection shortcoming or inadequacy in the quality nature and "
                       "manner of performance of service",
        "escalation": [
            {"name": "Company grievance contact", "channel": "email / grievance page", "url": None, "phone": None,
             "when_to_use": "First. Complain in writing and keep proof.", "typical_wait": "30 days",
             "reply_within": {"n": 30, "unit": "days", "kind": "requested", "basis": "a reasonable time to resolve it (no rule fixes one for this service)"}},
            {"name": "National Consumer Helpline (NCH)", "channel": "portal / phone / WhatsApp",
             "url": "https://consumerhelpline.gov.in", "phone": "1915 · WhatsApp 8800001915",
             "when_to_use": "If the company does not resolve it.", "typical_wait": "about 30-45 days"},
            {"name": "District Consumer Disputes Redressal Commission", "channel": "online filing (e-Jagriti)",
             "url": "https://e-jagriti.gov.in", "phone": None,
             "when_to_use": "File within two years of the cause of action.", "typical_wait": "months",
             "file_within": {"n": 2, "unit": "years", "from": "incident", "basis": "a consumer complaint must be filed within two years of the cause of action", "source": "Consumer Protection Act, 2019, Section 69(1)"}},
        ],
    },
}

# Public helplines/portals named in playbooks - allowed in drafts without being "verified company contacts".
PUBLIC_CONTACTS = {"1915", "8800001915", "14448", "155255", "18004254732", "1930", "198", "crpc@rbi.org.in",
                   "complaints@irdai.gov.in", "112", "108", "1098", "181", "14416", "18008914416", "15100"}

# Tier A: draft and escalate · B: safety first, police complaint + urgent routing · C: explain and refer (no drafting
# of court documents).
TIERS = {"A": "Draft and escalate", "B": "Draft and route urgently", "C": "Explain and refer"}
CYBER_RE = re.compile(r"cyber|fraud|scam|otp|phishing|fake (?:call|link|website|app|customer care)|upi|online|"
                      r"sextortion|digital arrest|hack|ठगी|धोखा|thagi", re.I)


def tier_of(sector: str) -> str:
    return SECTORS.get(sector, SECTORS["other"]).get("tier", "A")


def forum_query(sector: str, city: str, complaint: str = "") -> tuple[str, str, str] | None:
    """(Google Maps query, title pattern, label) for the local office that matters for this domain, or None."""
    if sector == "police":
        if CYBER_RE.search(complaint):
            return f"Cyber Crime Police Station {city}", r"cyber|police", "Cyber crime police station"
        return f"Superintendent of Police office {city}", r"police|commissioner|superintendent|SP office", \
            "Senior police office"
    if sector == "legal":
        return f"District Legal Services Authority {city}", r"legal services|DLSA|legal aid", "Free legal aid (DLSA)"
    if sector in ("govt_scheme", "public_service"):
        return None
    return f"District Consumer Disputes Redressal Commission {city}", r"consumer|forum|commission", "Consumer commission"

ISSUE_QUERIES = {
    "refund": "refund of consideration within time; consumer right to seek redressal; unfair trade practice",
    "defective": "defective goods replacement refund; product liability; deficiency",
    "deficiency": "deficiency in service fault imperfection shortcoming inadequacy in quality of performance",
    "unauthorised_charge": "unauthorised charges deducted from account without consent; deficiency in service",
    "harassment": "recovery agents harassment intimidation; grievance redressal; data access contacts",
    "claim_rejection": "claim rejected repudiated by insurer; complaint to ombudsman",
    "misleading": "misleading advertisement unfair trade practice false representation",
    "data_privacy": "collection and sharing of personal data without consent; privacy",
    "cyber_fraud": "fraudulent electronic banking transaction reported by customer; limited liability; reversal",
    "other": "consumer rights to be heard and seek redressal against unfair trade practice",
}

# Facts each sector's complaint needs; missing ones become placeholders in the draft.
REQUIRED_FACTS = {
    "ecommerce": [("order_id", "[ORDER ID]"), ("date", "[ORDER DATE]"), ("amount", "[AMOUNT]")],
    "bank": [("reference_id", "[TRANSACTION / COMPLAINT REFERENCE]"), ("date", "[TRANSACTION DATE]"),
             ("amount", "[AMOUNT]")],
    "telecom": [("reference_id", "[MOBILE NUMBER / ACCOUNT ID]"), ("date", "[DATE]")],
    "insurance": [("reference_id", "[POLICY / CLAIM NUMBER]"), ("amount", "[CLAIM AMOUNT]")],
    "loan_app": [("reference_id", "[LOAN ACCOUNT NUMBER]"), ("date", "[DATE OF CALLS]")],
    "govt_scheme": [],
    "public_service": [("reference_id", "[APPLICATION / COMPLAINT NUMBER]"), ("date", "[DATE]")],
    "police": [("date", "[DATE AND TIME OF INCIDENT]"), ("place", "[PLACE OF INCIDENT]"),
               ("police_station", "[POLICE STATION]")],
    "legal": [],
    "other": [("reference_id", "[BOOKING / REFERENCE NUMBER]"), ("date", "[DATE]"), ("amount", "[AMOUNT]")],
}


def missing_facts_for(sector: str, facts: dict) -> list[dict]:
    """Required facts for the sector that the complaint doesn't state; they become [PLACEHOLDERS] in the draft."""
    return [{"fact": f, "placeholder": ph} for f, ph in REQUIRED_FACTS.get(sector, REQUIRED_FACTS["other"])
            if not (facts.get(f) or (f == "reference_id" and facts.get("order_id")))]


# --------------------------------------------------------------------------- the menu and learned playbooks

for _cfg in SECTORS.values():  # every seed step carries its source
    for _s in _cfg["escalation"] + ([_cfg["cyber_step"]] if _cfg.get("cyber_step") else []):
        _s.setdefault("source", SEED_SOURCE)


def playbook_menu() -> str:
    """What intake's LLM may match against: seed + learned playbooks (key, label, what it covers, regulator)."""
    return "\n".join(f"- {k}: {c['label']}" + (f" - {c['covers']}" if c.get("covers") else "")
                     + f" [regulator: {c['regulator']}]" + (" (learned from official sources)" if c.get("learned")
                                                             else "")
                     for k, c in SECTORS.items())


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:40] or "route"


def register_playbook(key: str, entry: dict, *, save: bool = True) -> str:
    """Add a learned playbook (already checked by route_research's guards). Never overwrites a seed playbook."""
    key = slug(key)
    if key in SECTORS and not SECTORS[key].get("learned"):
        key = f"{key}_learned"
    entry = {"officer_terms": ["grievance officer", "nodal officer"],
             "retry_terms": ["appellate authority", "contact us"], "law_docs": [], "basis_query": "",
             "tier": "A", "learned": True, "learned_at": date.today().isoformat(), **entry}
    SECTORS[key] = entry
    if save:
        (PLAYBOOK_DIR / f"{key}.json").write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
    return key


def load_learned() -> int:
    n = 0
    for path in sorted(PLAYBOOK_DIR.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if entry.get("escalation") and path.stem not in SECTORS:
            SECTORS[path.stem] = entry
            n += 1
    return n


load_learned()
