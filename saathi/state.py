"""Graph state and the pydantic models used for structured LLM output.

Fields written by parallel workers use reducers (operator.add) so fan-in merges them; everything else has a
single writer. Every contact dict carries the brief's schema
    {role, name, email, phone, url, source_url, source_type: official_domain|registry|third_party, verified, reason}
plus internal keys the verifier uses (type, value, source_domain, via, evidence, timeline, designation).
"""
from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field


class ComplaintState(TypedDict, total=False):
    # input
    complaint: str
    company_hint: str | None
    city: str | None
    complainant: dict | None
    live: bool
    # intake_planner
    language: str                                    # "en" | "hi" | "hinglish"
    sector: str                                      # ecommerce | bank | telecom | insurance | loan_app | other
    company: str
    company_domain: str | None                       # resolved official domain
    official_domains: list[str]                      # company_domain + redirect aliases
    domain_basis: str | None
    facts: dict                                      # order_id, amount, date, product, issue, what_user_wants…
    issue_types: list[str]
    missing_facts: list[dict]                        # [{fact, placeholder}]
    search_plan: list[dict]
    search_keywords: str
    urgent_note: str | None
    # workers (fan-in via reducers)
    contacts: Annotated[list[dict], operator.add]
    precedents: Annotated[list[dict], operator.add]
    pages: Annotated[list[dict], operator.add]
    local_forum: dict | None
    registry: dict | None
    blocked_hallucinations: Annotated[int, operator.add]
    errors: Annotated[list[str], operator.add]
    # verifier
    verified_contacts: list[dict]
    unverified_contacts: list[dict]
    recommended: dict | None
    verify_attempts: int
    grievance_attempts: int
    # legal_rag / drafter / critic
    clauses: list[dict]
    draft: str
    subject: str
    summary: str                                     # plain-language summary in the user's language
    escalation: list[dict]
    critic_notes: list[str]
    draft_attempts: int
    draft_method: str
    # human_review / finalize
    user_edits: dict | None
    final: dict | None
    trace: Annotated[list[str], operator.add]        # human-readable step log for the UI
    live_calls: Annotated[int, operator.add]


# --------------------------------------------------------------------------- structured LLM outputs

SectorT = Literal["ecommerce", "bank", "telecom", "insurance", "loan_app", "other"]
IssueT = Literal["refund", "defective", "deficiency", "unauthorised_charge", "harassment", "claim_rejection",
                 "misleading", "data_privacy", "other"]


class Facts(BaseModel):
    product_or_service: str = Field("", description="What was bought / the service")
    issue: str = Field("", description="One neutral sentence describing what went wrong")
    what_user_wants: str = Field("", description="Remedy wanted: refund, replacement, reversal…")
    order_id: str = Field("", description="Order ID exactly as written, else ''")
    reference_id: str = Field("", description="Transaction / policy / claim / complaint / account ref, else ''")
    amount: str = Field("", description="Amount in rupees, digits only, else ''")
    date: str = Field("", description="Date as written, else ''")
    city: str = ""


class IntakeOut(BaseModel):
    language: Literal["en", "hi", "hinglish"]
    sector: SectorT
    company: str = Field(description="Company / app / bank / insurer the complaint is against")
    issue_types: list[IssueT]
    facts: Facts
    urgent_fraud_or_harassment: bool
    search_keywords: str = Field(description="2-4 English words for finding similar complaints, e.g. 'refund "
                                             "damaged phone'")


class ExtractedContact(BaseModel):
    role: Literal["appellate_authority", "principal_nodal_officer", "nodal_officer", "grievance_officer",
                  "grievance_cell", "customer_care", "other"]
    name: str = ""
    designation: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    response_timeline: str = ""
    evidence_quote: str = Field("", description="Verbatim text from the page containing the contact")


class ExtractOut(BaseModel):
    contacts: list[ExtractedContact]


class PrecedentJudgement(BaseModel):
    index: int
    relevant: bool
    how_resolved: str = Field("", description="Which channel worked, in one short phrase, or ''")


class PrecedentsOut(BaseModel):
    results: list[PrecedentJudgement]


class DraftOut(BaseModel):
    subject: str
    body: str = Field(description="Letter body. Cite rules ONLY as [C1], [C2]…; use [PLACEHOLDERS] for missing facts")
    remedy: str
    reply_deadline_days: int
    summary: str = Field(description="3-4 sentence plain-language summary for the user, in the user's language")


class CriticOut(BaseModel):
    issues: list[str] = Field(description="Concrete problems: unsupported legal claims, invented facts, tone. "
                                          "Empty if the draft is fine.")


def schema(model: type[BaseModel]) -> dict:
    return model.model_json_schema()
