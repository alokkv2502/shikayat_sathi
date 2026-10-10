"""The Shikayat Saathi LangGraph.

START → safety_router (rules, instant: emergency numbers first) → intake_planner (ONE LLM call: domain + tier,
        facts, English translation, follow-up questions, and the LLM's danger check)
→ (very unclear: clarify - ONE question, then intake again)
→ case_research (route research if no verified playbook fits; scheme / product research)
→ frame (brief decides sector / tier / addressee, no LLM)
frame ─[Send]→ grievance · precedents · local_forum · gap_check · (registry if loan_app)   (parallel fan-out)
all of them → verifier                                                                      (fan-in via reducers)
verifier ── no verified contact & verify_attempts < 2 ──→ grievance (alternative phrasing)  (self-correcting loop 1)
verifier ── otherwise ──→ legal_rag → ask_details (interrupt: ONE form with every missing fact / question)
ask_details ── answers ──→ reassess → drafter;  nothing asked / answered ──→ drafter → critic
critic ── issues & draft_attempts < 2 ──→ drafter                                           (self-correcting loop 2)
critic ── otherwise ──→ human_review (interrupt: approve or edit the letter)
human_review ── (API only) new answers ──→ reassess → drafter → critic → human_review     (loop 3)
human_review ── approved ──→ finalize → END

Checkpointed with SqliteSaver (data/checkpoints.db); one thread_id per complaint, so a run pauses at
ask_details / human_review and resumes later with Command(resume=...).
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from functools import lru_cache
from typing import Callable, Iterator

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from config import DATA_DIR
from saathi import timing
from saathi.nodes.case_research import case_research
from saathi.nodes.critic import critic, route_after_critic
from saathi.nodes.drafter import drafter
from saathi.nodes.gap_check import frame, gap_check, reassess
from saathi.nodes.grievance import grievance
from saathi.nodes.intake import clarify, fan_out, intake_planner, route_after_intake
from saathi.nodes.legal_rag import legal_rag
from saathi.nodes.local_forum import local_forum
from saathi.nodes.precedents import precedents
from saathi.nodes.registry_worker import registry
from saathi.nodes.review import ask_details, finalize, human_review, route_after_details, route_after_review
from saathi.nodes.safety import safety_router
from saathi.nodes.verifier import route_after_verify, verifier
from saathi.state import ComplaintState

WORKERS = ["grievance", "precedents", "local_forum", "registry", "gap_check"]


def build(checkpointer=None):
    g = StateGraph(ComplaintState)
    g.add_node("safety_router", safety_router)
    g.add_node("intake_planner", intake_planner)
    g.add_node("clarify", clarify)
    g.add_node("case_research", case_research)
    g.add_node("frame", frame)
    g.add_node("gap_check", gap_check)
    g.add_node("grievance", grievance)
    g.add_node("precedents", precedents)
    g.add_node("local_forum", local_forum)
    g.add_node("registry", registry)
    g.add_node("verifier", verifier)
    g.add_node("legal_rag", legal_rag)
    g.add_node("drafter", drafter)
    g.add_node("critic", critic)
    g.add_node("ask_details", ask_details)
    g.add_node("human_review", human_review)
    g.add_node("reassess", reassess)
    g.add_node("finalize", finalize)

    g.add_edge(START, "safety_router")                 # rules only: instant
    g.add_edge("safety_router", "intake_planner")      # one LLM call: domain, facts, translation, danger, questions
    g.add_conditional_edges("intake_planner", route_after_intake, ["clarify", "case_research"])
    g.add_edge("clarify", "intake_planner")  # once: very unclear complaints get ONE question
    g.add_edge("case_research", "frame")
    g.add_conditional_edges("frame", fan_out, WORKERS)
    for w in WORKERS:
        g.add_edge(w, "verifier")
    g.add_conditional_edges("verifier", route_after_verify, ["grievance", "legal_rag"])
    g.add_edge("legal_rag", "ask_details")
    g.add_conditional_edges("ask_details", route_after_details, ["reassess", "drafter"])
    g.add_edge("drafter", "critic")
    g.add_conditional_edges("critic", route_after_critic, ["drafter", "human_review"])
    g.add_conditional_edges("human_review", route_after_review, ["reassess", "finalize"])
    g.add_edge("reassess", "drafter")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


def build_contacts():
    """Subgraph for the evaluation: resolve domain → grievance (+ registry for loan apps) → verifier, with the same
    verifier retry loop. No drafting, no review."""
    from langgraph.types import Send

    from saathi.nodes.common import live, node
    from saathi.tools.domains import resolve_official
    from saathi.tools.serp import serp

    @node("🌐 Resolve domain")
    def resolve(state: dict) -> dict:  # the caller (eval) owns the credit budget for the whole run
        off = resolve_official(serp, state["company"], live=live(state))
        return {"company_domain": off.get("domain"), "official_domains": off.get("domains") or [],
                "domain_basis": off.get("basis"), "verify_attempts": 0, "grievance_attempts": 0,
                "trace": [f"🌐 Official domain: {off.get('domain') or 'not resolved'}"]}

    def fan(state: dict):
        return [Send("grievance", state)] + ([Send("registry", state)] if state["sector"] == "loan_app" else [])

    g = StateGraph(ComplaintState)
    g.add_node("resolve", resolve)
    g.add_node("grievance", grievance)
    g.add_node("registry", registry)
    g.add_node("verifier", verifier)
    g.add_edge(START, "resolve")
    g.add_conditional_edges("resolve", fan, ["grievance", "registry"])
    g.add_edge("grievance", "verifier")
    g.add_edge("registry", "verifier")
    g.add_conditional_edges("verifier", lambda s: "grievance" if route_after_verify(s) == "grievance" else END,
                            ["grievance", END])
    return g.compile()


@lru_cache(maxsize=1)
def checkpointer() -> SqliteSaver:
    return SqliteSaver(sqlite3.connect(DATA_DIR / "checkpoints.db", check_same_thread=False))


@lru_cache(maxsize=1)
def graph():
    return build(checkpointer())


# --------------------------------------------------------------------------- run helpers

def new_thread() -> str:
    return uuid.uuid4().hex[:12]


def initial_state(complaint: str, *, company: str | None = None, city: str | None = None,
                  complainant: dict | None = None, live: bool = True) -> dict:
    return {"complaint": complaint, "company_hint": company or None, "city": city or None,
            "complainant": complainant or {}, "live": live, "contacts": [], "precedents": [], "pages": [],
            "errors": [], "trace": [], "live_calls": 0, "blocked_hallucinations": 0}


def stream(inp, thread_id: str, on_update: Callable[[str, dict], None] | None = None,
           app=None) -> Iterator[tuple[str, dict]]:
    """Stream node updates (stream_mode="updates"). `inp` is an initial state or Command(resume=...).
    Yields (node, update); an interrupt arrives as ("__interrupt__", payload)."""
    app = app or graph()
    cfg = {"configurable": {"thread_id": thread_id}}
    for chunk in app.stream(inp, cfg, stream_mode="updates"):
        for node_name, update in chunk.items():
            if node_name == "__interrupt__":
                update = update[0].value if update else {}
            if on_update:
                on_update(node_name, update or {})
            yield node_name, update or {}


def run(inp, thread_id: str, on_update=None, app=None) -> dict:
    """Run until the graph pauses (human_review) or ends. Returns the current state values (+ '__interrupt__')."""
    app = app or graph()
    paused = None
    since, t0 = timing.mark(), time.perf_counter()
    timing.log.info(f"── thread {thread_id}: {'resumed' if isinstance(inp, Command) else 'started'} ──")
    for node_name, update in stream(inp, thread_id, on_update, app):
        if node_name == "__interrupt__":
            paused = update
    timing.log_run(since, f"── thread {thread_id}: {'paused for review' if paused is not None else 'finished'} "
                          f"after {time.perf_counter() - t0:.1f}s ──")
    values = dict(app.get_state({"configurable": {"thread_id": thread_id}}).values)
    if paused is not None:
        values["__interrupt__"] = paused
    return values


def resume(thread_id: str, edits: dict | None = None, on_update=None, app=None) -> dict:
    # Never resume with an empty dict: LangGraph reads `resume={}` as an (empty) interrupt-id map and re-pauses.
    return run(Command(resume=edits or {"approved": True}), thread_id, on_update, app)
