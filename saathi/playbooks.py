"""Sector -> regulator playbooks, encoded as data (not prompts).

Each escalation step: {name, channel, url, phone, when_to_use, typical_wait}. Channels, numbers and waiting periods
were checked against the regulators' own texts (raw/legal/) and portals on 2026-10-07; they change, so re-check
before relying on them. Legal citations in a draft never come from here - only from retrieved clauses.
"""
from __future__ import annotations

import re

SECTORS: dict[str, dict] = {
    "ecommerce": {
        "label": "E-commerce / online shopping",
        "keywords": r"order(ed)?|delivery|delivered|replacement|seller|flipkart|amazon|myntra|meesho|ajio|nykaa|"
                    r"snapdeal|swiggy|zomato|blinkit|zepto|bigbasket|tata cliq|jiomart|damaged|defective|"
                    r"fake product|wrong item|cash on delivery|online purchase|e-?commerce|app order|ऑर्डर|डिलीवरी",
        "regulator": "Department of Consumer Affairs (National Consumer Helpline) · Consumer Commissions",
        "officer_terms": ["grievance officer", "\"grievance redressal\""],
        "retry_terms": ["nodal officer", "customer care escalation", "contact us"],
        "law_docs": ["ecom", "cpa"],
        "basis_query": "e-commerce grievance officer acknowledge 48 hours redress one month",
        "escalation": [
            {"name": "Company grievance officer", "channel": "email / grievance page",
             "url": None, "phone": None,
             "when_to_use": "First. E-commerce entities must display their grievance officer; the officer must "
                            "acknowledge within 48 hours and redress within one month.",
             "typical_wait": "48 h acknowledgement, 1 month resolution"},
            {"name": "National Consumer Helpline (NCH)", "channel": "portal / phone / WhatsApp",
             "url": "https://consumerhelpline.gov.in", "phone": "1915 (toll-free) · WhatsApp 8800001915",
             "when_to_use": "If the company does not resolve it in a month. NCH forwards the docket to the company.",
             "typical_wait": "about 30-45 days"},
            {"name": "District Consumer Disputes Redressal Commission", "channel": "online filing (e-Jagriti)",
             "url": "https://e-jagriti.gov.in", "phone": None,
             "when_to_use": "Still unresolved: file a consumer complaint (claims up to ₹50 lakh) within two years "
                            "of the cause of action. e-Jagriti replaced e-Daakhil.", "typical_wait": "months"},
        ],
    },
    "bank": {
        "label": "Bank / NBFC / UPI / credit card",
        "keywords": r"bank|account|upi|debit card|credit card|atm|neft|imps|rtgs|cheque|loan emi|nbfc|kyc|"
                    r"net ?banking|wallet|paytm|phonepe|gpay|google pay|unauthori[sz]ed (debit|transaction)|"
                    r"hdfc|icici|sbi|axis|kotak|yes bank|pnb|bank of baroda|canara|idfc|bajaj finance|बैंक|खाता",
        "regulator": "Reserve Bank of India - RB-Integrated Ombudsman Scheme, 2026",
        "officer_terms": ["principal nodal officer", "nodal officer"],
        "retry_terms": ["grievance redressal officer", "escalation matrix", "contact us"],
        "law_docs": ["ios", "cpa"],
        "basis_query": "grounds of complaint act or omission of bank resulting in deficiency in service",
        "escalation": [
            {"name": "Bank grievance cell → Principal Nodal Officer", "channel": "email / bank complaint portal",
             "url": None, "phone": None,
             "when_to_use": "First. Complain in writing and keep the complaint number; escalate to the Principal "
                            "Nodal Officer if unresolved.", "typical_wait": "30 days"},
            {"name": "RBI Ombudsman (RB-IOS, 2026)", "channel": "RBI CMS portal / email",
             "url": "https://cms.rbi.org.in", "phone": "14448 (RBI contact centre)",
             "when_to_use": "No reply within 30 days, or the reply is unsatisfactory. Also crpc@rbi.org.in. No fee.",
             "typical_wait": "weeks"},
            {"name": "District Consumer Commission", "channel": "online filing (e-Jagriti)",
             "url": "https://e-jagriti.gov.in", "phone": None,
             "when_to_use": "Deficiency in banking service is also a consumer dispute.", "typical_wait": "months"},
        ],
        "urgent": "For an unauthorised or fraudulent debit, tell the bank immediately and call the National Cyber "
                  "Crime Helpline 1930 (cybercrime.gov.in) - reporting quickly limits your liability.",
    },
    "telecom": {
        "label": "Mobile / broadband / DTH",
        "keywords": r"jio|airtel|vodafone|\bvi\b|bsnl|mtnl|sim|recharge|prepaid|postpaid|mobile plan|data pack|"
                    r"network|broadband|fiber|fibre|dth|tata play|call drop|port(ing)?|mnp|value added service|vas|"
                    r"\bplan\b|caller tune|\bdata\b|रिचार्ज|सिम",
        "regulator": "TRAI - Telecom Consumers Complaint Redressal Regulations, 2012 · DoT",
        "officer_terms": ["appellate authority", "nodal officer"],
        "retry_terms": ["grievance officer", "customer care escalation", "contact us"],
        "law_docs": ["trai", "cpa"],
        "basis_query": "complaint centre docket number time limit three days",
        "escalation": [
            {"name": "Operator complaint centre", "channel": "consumer care number / app",
             "url": None, "phone": "198 (complaints)",
             "when_to_use": "First. Note the docket number you are given.",
             "typical_wait": "TRAI QoS time limit (3 days where none is specified)"},
            {"name": "Operator's Appellate Authority", "channel": "email / post / online",
             "url": None, "phone": None,
             "when_to_use": "Not satisfied: appeal within 30 days after the complaint-centre time limit expires.",
             "typical_wait": "per TRAI regulations"},
            {"name": "DoT CPGRAMS / Consumer Commission", "channel": "pgportal.gov.in / e-Jagriti",
             "url": "https://pgportal.gov.in", "phone": None,
             "when_to_use": "TRAI does not resolve individual complaints; escalate to DoT or file a consumer "
                            "complaint.", "typical_wait": "weeks to months"},
        ],
    },
    "insurance": {
        "label": "Insurance",
        "keywords": r"insurance|insurer|policy|claim|premium|health cover|mediclaim|cashless|tpa|lic\b|star health|"
                    r"hdfc ergo|icici lombard|bajaj allianz|niva bupa|care health|term plan|motor claim|बीमा|क्लेम",
        "regulator": "IRDAI (Bima Bharosa) · Insurance Ombudsman (Insurance Ombudsman Rules, 2017)",
        "officer_terms": ["grievance redressal officer", "grievance officer"],
        "retry_terms": ["grievance cell", "customer care escalation", "contact us"],
        "law_docs": ["ins", "cpa"],
        "basis_query": "complaint to insurer representation rejected or no reply within one month",
        "escalation": [
            {"name": "Insurer's Grievance Redressal Officer", "channel": "email / grievance portal",
             "url": None, "phone": None,
             "when_to_use": "First. Quote policy and claim numbers.", "typical_wait": "1 month"},
            {"name": "IRDAI Bima Bharosa", "channel": "portal / email / phone",
             "url": "https://bimabharosa.irdai.gov.in", "phone": "155255 / 1800 4254 732",
             "when_to_use": "Unresolved or unsatisfactory: register on Bima Bharosa (complaints@irdai.gov.in).",
             "typical_wait": "weeks"},
            {"name": "Insurance Ombudsman", "channel": "Council for Insurance Ombudsmen",
             "url": "https://www.cioins.co.in", "phone": None,
             "when_to_use": "Insurer rejected your complaint or did not reply within one month: complain within one "
                            "year (awards up to ₹50 lakh).", "typical_wait": "about 3 months"},
        ],
    },
    "loan_app": {
        "label": "Loan app / digital lending",
        "keywords": r"loan app|instant loan|digital loan|recovery agent|harass|morphed|contacts? (list )?access|"
                    r"threat|abusive call|lending app|cash ?advance app|kreditbee|moneyview|navi|fibe|kissht|slice|"
                    r"truebalance|mpokket|pocketly|stashfin|लोन|ऋण|धमकी",
        "regulator": "Reserve Bank of India (Digital Lending Directions, 2025; RB-IOS, 2026) · Cyber Crime",
        "officer_terms": ["grievance redressal officer", "nodal officer"],
        "retry_terms": ["grievance officer", "contact us"],
        "law_docs": ["dl", "ios"],
        "basis_query": "nodal grievance redressal officer digital lending complaints",
        "escalation": [
            {"name": "Lender's Grievance Redressal Officer (from RBI's DLA directory)", "channel": "email / phone",
             "url": None, "phone": None,
             "when_to_use": "If the app is in RBI's Digital Lending App directory, write to the GRO the lender "
                            "declared to RBI.", "typical_wait": "30 days"},
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
    "other": {
        "label": "Other consumer services (travel, edtech, real estate, utilities…)",
        "keywords": r"flight|airline|indigo|air india|spicejet|akasa|irctc|train|ticket|hotel|booking|ola|uber|"
                    r"cab|makemytrip|edtech|course|coaching|builder|flat|possession|electricity|gym|subscription|"
                    r"warranty|service centre|repair|courier",
        "regulator": "Department of Consumer Affairs (National Consumer Helpline) · Consumer Commissions",
        "officer_terms": ["grievance officer", "customer care"],
        "retry_terms": ["nodal officer", "escalation", "contact us"],
        "law_docs": ["cpa"],
        "basis_query": "deficiency means any fault imperfection shortcoming or inadequacy in the quality nature and "
                       "manner of performance of service",
        "escalation": [
            {"name": "Company grievance contact", "channel": "email / grievance page", "url": None, "phone": None,
             "when_to_use": "First. Complain in writing and keep proof.", "typical_wait": "30 days"},
            {"name": "National Consumer Helpline (NCH)", "channel": "portal / phone / WhatsApp",
             "url": "https://consumerhelpline.gov.in", "phone": "1915 · WhatsApp 8800001915",
             "when_to_use": "If the company does not resolve it.", "typical_wait": "about 30-45 days"},
            {"name": "District Consumer Disputes Redressal Commission", "channel": "online filing (e-Jagriti)",
             "url": "https://e-jagriti.gov.in", "phone": None,
             "when_to_use": "File within two years of the cause of action.", "typical_wait": "months"},
        ],
    },
}

# Public helplines/portals named in playbooks - allowed in drafts without being "verified company contacts".
PUBLIC_CONTACTS = {"1915", "8800001915", "14448", "155255", "18004254732", "1930", "198", "crpc@rbi.org.in",
                   "complaints@irdai.gov.in"}

ISSUE_QUERIES = {
    "refund": "refund of consideration within time; consumer right to seek redressal; unfair trade practice",
    "defective": "defective goods replacement refund; product liability; deficiency",
    "deficiency": "deficiency in service fault imperfection shortcoming inadequacy in quality of performance",
    "unauthorised_charge": "unauthorised charges deducted from account without consent; deficiency in service",
    "harassment": "recovery agents harassment intimidation; grievance redressal; data access contacts",
    "claim_rejection": "claim rejected repudiated by insurer; complaint to ombudsman",
    "misleading": "misleading advertisement unfair trade practice false representation",
    "data_privacy": "collection and sharing of personal data without consent; privacy",
    "other": "consumer rights to be heard and seek redressal against unfair trade practice",
}
ISSUE_KEYWORDS = {
    "refund": r"refund|money back|return(ed)?|not credited|reversal|रिफंड|पैसे वापस",
    "defective": r"defect|damaged|broken|not working|faulty|fake|counterfeit|wrong (item|product)|खराब|टूटा",
    "unauthorised_charge": r"unauthori[sz]ed|without (my )?consent|deducted|charged .* (didn.?t|did not|never)|"
                           r"didn.?t activate|fraudulent (debit|transaction)|kaat liya|काट लिया",
    "harassment": r"harass|threat|abus|morph|recovery agent|calling my (contacts|family|relatives)|dhamki|धमकी|"
                  r"परेशान",
    "claim_rejection": r"claim (was )?(rejected|repudiated|denied)|rejected (my )?claim|claim reject|क्लेम .*रिजेक्ट",
    "misleading": r"mislead|false (claim|promise|ad)|advertis",
    "data_privacy": r"privacy|personal data|contacts? access|leak",
    "deficiency": r"deficien|poor service|not (resolved|delivered)|delay|no response|ignored",
}

# Facts each sector's complaint needs; missing ones become placeholders in the draft.
REQUIRED_FACTS = {
    "ecommerce": [("order_id", "[ORDER ID]"), ("date", "[ORDER DATE]"), ("amount", "[AMOUNT]")],
    "bank": [("reference_id", "[TRANSACTION / COMPLAINT REFERENCE]"), ("date", "[TRANSACTION DATE]"),
             ("amount", "[AMOUNT]")],
    "telecom": [("reference_id", "[MOBILE NUMBER / ACCOUNT ID]"), ("date", "[DATE]")],
    "insurance": [("reference_id", "[POLICY / CLAIM NUMBER]"), ("amount", "[CLAIM AMOUNT]")],
    "loan_app": [("reference_id", "[LOAN ACCOUNT NUMBER]"), ("date", "[DATE OF CALLS]")],
    "other": [("reference_id", "[BOOKING / REFERENCE NUMBER]"), ("date", "[DATE]"), ("amount", "[AMOUNT]")],
}


def classify_rules(text: str) -> tuple[str, list[str]]:
    """Keyword sector + issue types (English, Hindi, Hinglish); the fallback when no LLM is configured."""
    t = text.lower()
    scores = {s: len(re.findall(cfg["keywords"], t)) for s, cfg in SECTORS.items()}
    if scores["loan_app"] and re.search(r"loan app|lending app|recovery agent|harass|लोन", t):
        scores["loan_app"] += 3  # a loan *app* complaint beats generic banking words
    sector = max(scores, key=scores.get) if max(scores.values()) else "other"
    issues = [k for k, pat in ISSUE_KEYWORDS.items() if re.search(pat, t)] or ["other"]
    return sector, issues


HINGLISH = re.compile(r"\b(ne|ka|ki|ke|ko|kar|kiya|diya|liya|hai|hain|tha|nahi|nahin|mera|meri|mujhe|paisa|paise|"
                      r"wapas|kab|kyun|abhi|bhi|raha|rahi|gaya|gayi|bola|bol)\b", re.I)


def detect_language(text: str) -> str:
    if re.search(r"[ऀ-ॿ]", text):
        return "hi"
    return "hinglish" if len(HINGLISH.findall(text)) >= 2 else "en"
