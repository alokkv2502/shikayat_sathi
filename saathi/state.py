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
    complaint_en: str                                # the complaint in English - translated ONCE, at intake
    letter_language: str                             # en | hi - the WHOLE letter is in this one language
    addressee_lines: list[str]                       # the drafter's addressee block, in the letter's language
    legal_query: str                                 # intake's English search query for the applicable law
    extra_missing: list[dict]                        # intake's case-specific missing facts (beyond the domain minimum)
    sector: str                                      # the domain: a key of playbooks.SECTORS
    tier: str                                        # A draft & escalate · B route urgently · C explain & refer
    route: dict                                      # {domain_free_text, matched_playbook, confidence, reason, mode}
    ladder: dict                                     # escalation_planner: {steps, fallback, note, notes, sources}
    clarified: bool                                  # the one clarifying question was asked already
    safety: dict                                     # safety_router: {level, categories, helplines, messages}
    case_entities: list[str]
    case_brief: dict
    eligibility_checks: list[dict]                   # [{condition, status: met|not_met|unknown, evidence, question}]
    gap_questions: list[str]                         # questions for unconfirmed conditions
    extra_questions: list[str]                       # other facts the letter needs that the complaint doesn't state
    eligibility: dict                                # {status: meets_listed|may_not_qualify|unconfirmed|no_conditions, message}
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
    deadline: dict                                   # {date, basis, source, kind} the letter asks a reply by (code)
    # human_review / reassess / finalize
    user_edits: dict | None
    case_answers: dict                               # {condition or question: answer}, merged across review rounds
    fills: dict                                      # {placeholder: value}, merged across review rounds
    drafted_answers: dict                            # answers the current draft was written with
    user_draft: str                                  # the complainant's own edit of the last draft, if any
    review_rounds: int
    redraft: bool
    final: dict | None
    trace: Annotated[list[str], operator.add]        # human-readable step log for the UI
    live_calls: Annotated[int, operator.add]


# --------------------------------------------------------------------------- structured LLM outputs

IssueT = Literal["refund", "defective", "deficiency", "unauthorised_charge", "harassment", "claim_rejection",
                 "misleading", "data_privacy", "cyber_fraud", "other"]


class Facts(BaseModel):
    product_or_service: str = Field("", description="What was bought / the service (in English)")
    issue: str = Field("", description="One neutral English sentence describing what went wrong")
    what_user_wants: str = Field("", description="Remedy wanted, in English: refund, replacement, reversal…")
    order_id: str = Field("", description="Order ID exactly as written, else ''")
    reference_id: str = Field("", description="Transaction / policy / claim / complaint / account ref, else ''")
    amount: str = Field("", description="Amount in rupees, digits only, else ''")
    date: str = Field("", description="Date as written, else ''")
    city: str = ""
    place: str = Field("", description="Where it happened (for a crime / incident), else ''")
    police_station: str = Field("", description="Police station named by the complainant, else ''")
    details: dict[str, str] = Field(default_factory=dict,
                                    description="Additional case-specific facts stated by the complainant")


class MissingFact(BaseModel):
    fact: str = Field(description="snake_case key, e.g. booking_reference, meter_number, ticket_number")
    placeholder: str = Field(description="What the letter shows until it is filled, in capitals and brackets, "
                                         "e.g. [PNR / BOOKING NUMBER]")


class IntakeOut(BaseModel):
    language: Literal["en", "hi", "hinglish"]
    complaint_en: str = Field(description="The complaint translated faithfully into plain English (the same text if "
                                          "it is already English). Keep names, numbers, IDs and dates as written.")
    # Route: the LLM understands the complaint freely, then matches a verified playbook ONLY if one truly fits.
    domain_free_text: str = Field(description="What kind of problem this is, in your own words, e.g. 'air travel - "
                                              "flight cancellation refund'")
    matched_playbook: str | None = Field(None, description="Key of the playbook from the PLAYBOOKS list that fits "
                                                           "this complaint, or null if none of them really fits")
    match_confidence: float = Field(description="0-1: how well the matched playbook fits (0 if none)")
    route_reason: str = Field("", description="One line: why this playbook, or why none fits")
    needs_clarification: bool = Field(False, description="True only if it is impossible to tell what or whom the "
                                                         "complaint is about")
    clarifying_question: str = Field("", description="If needs_clarification: ONE short question, in the "
                                                     "complainant's language")
    company: str = Field(description="Company / app / bank / insurer / department the complaint is against")
    issue_types: list[IssueT]
    facts: Facts
    urgent_fraud_or_harassment: bool
    search_keywords: str = Field(description="2-4 English words for finding similar complaints, e.g. 'refund "
                                             "damaged phone'")
    case_entities: list[str] = Field(default_factory=list,
                                     description="Named schemes, plans, policies or services (including acronyms, "
                                                 "spelled as written) whose rules or eligibility need research")
    danger: list[Literal["self_harm", "violence", "sexual_violence", "missing_or_abducted", "medical"]] = Field(
        default_factory=list, description="IMMEDIATE risk to someone's life or safety described in the complaint "
                                          "(ongoing violence or a threat to kill, sexual violence, a missing or "
                                          "abducted person, suicide or self-harm risk, a medical emergency). Past, "
                                          "resolved events and ordinary fraud or harassment are not. Else [].")
    legal_query: str = Field("", description="A short English search query (8-15 words) for the Indian consumer / "
                                             "regulatory law that applies to THIS complaint, e.g. 'refund not given "
                                             "for cancelled flight deficiency in service consumer complaint'")
    missing_facts: list[MissingFact] = Field(default_factory=list,
                                             description="Up to 4 facts THIS letter needs that the complaint does "
                                                         "not give (a booking / consumer / policy number, a date, "
                                                         "an amount). Never ones it already states.")
    route_key: str = Field("", description="Short snake_case name of the sector, stable across similar complaints: "
                                           "e.g. electricity, water_supply, piped_gas, airline, university, "
                                           "municipal_services, ecommerce, banking")
    state: str = Field("", description="Indian state / UT where this happened or of the authority involved, if "
                                       "stated or clear from the city; else ''")
    authority_type: Literal["private_company", "central_government", "state_government", "state_utility_or_psu",
                            "unknown"] = Field("unknown", description="Who the complaint is against")
    route_queries: list[str] = Field(default_factory=list,
                                     description="2-4 web searches that would find the OFFICIAL grievance route for "
                                                 "this complaint (the body's grievance cell, the sector forum, "
                                                 "ombudsman or regulator), e.g. 'JVVNL consumer grievance redressal "
                                                 "forum', 'electricity ombudsman Rajasthan'")
    follow_up_questions: list[str] = Field(default_factory=list,
                                           description="Up to 3 short questions, in the complainant's language, "
                                                       "about facts a strong letter needs that the complaint does "
                                                       "not state (what was already tried, replies received, "
                                                       "reference numbers). Not order ID / date / amount.")


ComplaintTypeT = Literal["consumer_service", "govt_scheme", "banking", "insurance", "telecom", "loan_app"]


class CaseBrief(BaseModel):
    subject: str = Field(description="Full official name of the scheme / policy / product, e.g. with its part")
    aliases: list[str] = Field(default_factory=list, description="Other names and abbreviations used in the sources")
    administering_body: str = Field("", description="Department, agency or company that runs it and handles "
                                                    "grievances, as named in the sources")
    what_it_is: str = Field(description="2-3 plain sentences")
    complaint_type: ComplaintTypeT
    entitlement: str = Field(description="What the complainant should receive, how much and when, per the sources")
    conditions: list[str] = Field(description="Each eligibility / payment condition as a separate item")
    common_failure_reasons: list[str] = Field(description="Reasons the sources give for payments or claims failing")
    official_grievance_channel: str = Field(description="Official grievance portal / address named in the sources, "
                                                        "with its URL only if the URL appears in the sources")
    documents_needed: list[str]
    sources: list[str] = Field(description="URLs (from the SOURCE lines) that support the facts above")


LevelT = Literal["company", "sector_forum", "sector_regulator_or_ombudsman", "court_or_commission", "state_portal",
                 "central_portal", "other"]


class EscalationStep(BaseModel):
    order: int
    authority: str = Field(description="As the source names it, e.g. 'CGRF, Jaipur Vidyut Vitran Nigam Ltd'")
    level: LevelT = Field(description="company = the body's own grievance cell; sector_forum = a statutory forum of "
                                      "this sector (e.g. an electricity CGRF); sector_regulator_or_ombudsman; "
                                      "court_or_commission (consumer commission, courts); state_portal (a state "
                                      "public-grievance portal / CM helpline); central_portal (CPGRAMS)")
    when_to_use: str = Field(description="When to go here, per the source")
    channel: str = Field(description="Portal / email / address / phone, as the source states it")
    url: str = Field("", description="Its link, ONLY if that exact link appears in the sources")
    timeline: str | None = Field(None, description="Only if a source states it, e.g. '45 days'; else null")
    source_url_or_clause: str = Field(description="A SOURCE url from the list, or a clause tag like [C2]")


class EscalationPlan(BaseModel):
    steps: list[EscalationStep] = Field(description="The ladder, in order, each step backed by a source")
    notes: str = Field("", description="Anything the complainant should know about this route")


class ConditionAssessment(BaseModel):
    condition: str
    status: Literal["met", "not_met", "unknown"]
    evidence: str = ""
    question: str = ""


class GapCheckOut(BaseModel):
    assessments: list[ConditionAssessment] = Field(default_factory=list)
    other_questions: list[str] = Field(default_factory=list,
                                       description="Up to 3 questions about facts the letter needs that the complaint "
                                                   "doesn't state (dates, reference numbers, what was already tried, "
                                                   "replies received). Not about the listed conditions or placeholders.")


class ExtractedContact(BaseModel):
    organisation: str = Field(description="The organisation this contact belongs to, as the page states it (e.g. "
                                          "a lending partner, seller, bank or group company listed on the page)")
    belongs_to_named_company: bool = Field(description="True only if this is the named company's OWN contact")
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
    addressee_lines: list[str] = Field(default_factory=list,
                                       description="2-3 lines naming the receiving office, in the letter's language: "
                                                   "the officer's title, the organisation / department, and the state "
                                                   "or city if relevant (no portal or website names)")
    outline: list[str] = Field(default_factory=list,
                               description="Plan first: the ordered points of this specific case's document (who "
                                           "the complainant is, what they are owed and why, what happened, what "
                                           "they already checked, the exact request, deadline, next step). Then "
                                           "write the body from it.")
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
