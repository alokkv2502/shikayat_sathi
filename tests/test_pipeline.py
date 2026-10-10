"""Graph tests on a FICTIONAL company (Kartify, *.example domains) with SerpApi and page fetches mocked."""
import json
import re
import types

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import saathi.nodes.grievance as grievance_mod
import saathi.tools.domains as domains_mod
from saathi import graph as G
from saathi import llm
from saathi.nodes import verifier as V
from saathi.nodes.legal_rag import strip_unsupported
from saathi.tools.serp import serp
from tests import fake_llm

SERP = {
    "Kartify official website": {
        "knowledge_graph": {"title": "Kartify", "website": "https://www.kartify.example/"},
        "organic_results": [{"position": 1, "title": "Kartify", "link": "https://www.kartify.example/"}]},
    "site:kartify.example grievance officer": {"organic_results": [
        {"position": 1, "title": "Grievance Redressal | Kartify", "link": "https://www.kartify.example/grievance",
         "snippet": "Grievance Officer Ms. Asha Rao"}]},
    "Kartify grievance officer email": {"organic_results": [
        {"position": 1, "title": "Kartify customer care number 24x7",
         "link": "https://kartify-customer-care.example/help", "snippet": "Call Kartify helpline 98765 43210"},
        {"position": 2, "title": "Kartify toll free numbers", "link": "https://tollfree-directory.example/kartify",
         "snippet": "Kartify refund desk kartify.refunds@gmail.com"}]},
    "Kartify refund complaint resolved": {"organic_results": [
        {"title": "Kartify refused refund - resolved after grievance officer email", "link": "https://forum.example/t/1",
         "source": "Forum", "snippet": "Escalated to Kartify grievance officer"}]},
    "District Consumer Disputes Redressal Commission Pune": {"local_results": [
        {"title": "District Consumer Disputes Redressal Commission, Pune", "address": "Example Road, Pune",
         "phone": "020 0000 0000"}]},
}
PAGES = {
    "https://www.kartify.example": "Kartify - shop online",
    "https://www.kartify.example/grievance": (
        "Grievance Redressal Mechanism. In accordance with the Consumer Protection (E-Commerce) Rules, 2020 the "
        "Grievance Officer details are: Name: Asha Rao, Senior Manager. Email: grievance [at] kartify [dot] example "
        "Phone: 080-41234567. Complaints are acknowledged within 48 hours and resolved within one month."),
    "https://www.kartify.example/contact-us": "Contact us. Nodal officer: nodal [at] kartify [dot] example",
    "https://kartify-customer-care.example/help": "Kartify customer care number: call 98765 43210 now for refunds.",
    "https://tollfree-directory.example/kartify": "Kartify refund helpline email kartify.refunds@gmail.com",
}


def fake_fetch(url, **_):
    from saathi.tools.fetch import deobfuscate
    text = PAGES.get(url.rstrip("/"))
    if text is None:
        return {"url": url, "final_url": url, "ok": False, "text": "", "mailtos": [], "tels": [], "error": "404"}
    return {"url": url, "final_url": url, "ok": True, "text": deobfuscate(text + " " * 80), "mailtos": [],
            "tels": [], "title": "page"}


@pytest.fixture
def mocked(monkeypatch):
    monkeypatch.setattr(serp, "search", lambda params, live=True, **_: SERP.get(params.get("q"), {}))
    monkeypatch.setattr(domains_mod, "fetch", fake_fetch)
    monkeypatch.setattr(grievance_mod, "fetch", fake_fetch)
    return monkeypatch


def run_graph(text, details: dict | None = None, stop_at_form: bool = False, **kw):
    """Run to the letter review. The details form (if any questions) is submitted with `details` (empty by default);
    stop_at_form=True returns at the form instead."""
    app = G.build(InMemorySaver())
    t = G.new_thread()
    nodes = []
    s = G.run(G.initial_state(text, **kw), t, on_update=lambda n, u: nodes.append(n), app=app)
    if not stop_at_form and (s.get("__interrupt__") or {}).get("stage") == "details":
        s = G.resume(t, details or {}, on_update=lambda n, u: nodes.append(n), app=app)
    return app, t, s, nodes


def test_graph_end_to_end_with_interrupt(mocked):
    app, t, s, nodes = run_graph("Kartify refused a refund for my damaged phone, order KT12345678, Rs 15,000.",
                                 company="Kartify", city="Pune")
    assert {"grievance", "precedents", "local_forum"} <= set(nodes)            # parallel Send fan-out
    assert nodes.index("verifier") > max(nodes.index(w) for w in ("grievance", "precedents", "local_forum"))
    assert "__interrupt__" in s and s["__interrupt__"]["draft"]               # paused at human_review
    assert s["sector"] == "ecommerce" and s["company_domain"] == "kartify.example"
    by_val = {c["value"]: c for c in s["verified_contacts"] + s["unverified_contacts"]}
    g = by_val["grievance@kartify.example"]
    assert g["verified"] and g["source_type"] == "official_domain" and "Asha Rao" in g["name"]
    assert not by_val["9876543210"]["verified"] and not by_val["kartify.refunds@gmail.com"]["verified"]
    assert s["recommended"]["value"] == "grievance@kartify.example"
    assert s["local_forum"]["name"].endswith("Pune") and s["precedents"]
    assert s["clauses"] and all("act" in c and "section" in c for c in s["clauses"])
    s = G.resume(t, {"fills": {"[ORDER DATE]": "15 Sep 2026"}, "complainant": {"name": "Asha"}}, app=app)
    f = s["final"]
    assert "grievance@kartify.example" in f["letter"] and "15 Sep 2026" in f["letter"] and "Asha" in f["letter"]
    assert f["cited"] and not f["removed_citations"]


def test_verifier_retry_loop(mocked, monkeypatch):
    monkeypatch.setattr(serp, "api_key", "test")
    monkeypatch.setitem(SERP, "site:kartify.example grievance officer", {})        # 1st pass: nothing official
    monkeypatch.setitem(SERP, "site:kartify.example nodal officer", {"organic_results": [  # retry phrasing finds it
        {"position": 1, "title": "Contact", "link": "https://www.kartify.example/contact-us", "snippet": ""}]})
    _, _, s, nodes = run_graph("Kartify refused a refund", company="Kartify")
    assert nodes.count("grievance") == 2 and nodes.count("verifier") == 2
    assert any("retrying search" in line for line in s["trace"])
    assert s["recommended"]["value"] == "nodal@kartify.example"


def test_critic_loop_sends_bad_draft_back(mocked, monkeypatch):
    drafts = iter([
        {"subject": "Complaint", "body": "Under Section 999 and [C9] you owe me for order ZZ99999999.",
         "remedy": "refund", "reply_deadline_days": 30, "summary": "s"},
        {"subject": "Complaint", "body": "Order KT12345678 [ORDER DATE] [C1]: please refund ₹500 by {by}.",
         "remedy": "refund", "reply_deadline_days": 30, "summary": "s"}])

    def fake_json_call(system, prompt, schema, **_):
        if "draft firm" not in system:
            return fake_llm.answer(system, prompt, schema)
        d = next(drafts)
        by = re.search(r"REPLY DEADLINE \(computed[^:]*\): (\d{1,2} \w+ \d{4})", prompt).group(1)  # the code's date
        return {**d, "body": d["body"].replace("{by}", by)}
    monkeypatch.setattr(llm, "json_call", fake_json_call)
    monkeypatch.setattr(llm, "label", lambda: "fake-llm")
    _, _, s, nodes = run_graph("Kartify refused a refund for order KT12345678 Rs 500", company="Kartify")
    assert nodes.count("drafter") == 2 and nodes.count("critic") == 2
    critic_lines = [x for x in s["trace"] if x.startswith("🧐")]
    assert "back to drafter" in critic_lines[0] and "passed" in critic_lines[1]
    assert s["draft_attempts"] == 2 and not s["critic_notes"]


def test_finalize_strips_unretrieved_citations():
    clauses = [{"tag": "C1", "ref": "Rule 4(4)", "citation": "E-Commerce Rules, Rule 4(4)", "title": "Duties"}]
    body, removed = strip_unsupported("Under Rule 4 [C1] and Section 999 of the Act [C7] you must refund.", clauses)
    assert "[C7]" not in body and "Section 999" not in body and "Rule 4 [C1]" in body
    assert set(removed) == {"[C7]", "Section 999"}


# Scheme knowledge lives ONLY in these fixtures (mocked search results, pages and LLM output) - the code has none.
SCHEMES = {
    "pm-vbry": {
        "entity": "PM-VBRY",
        "complaint": ("I joined as a first-time EPFO member on 1 August 2025. My gross monthly wages were Rs 50,000. "
                      "I completed six months of continuous employment on 1 February 2026, but have not received "
                      "the PM-VBRY first instalment."),
        "pages": {"https://pmvbry.epfindia.gov.in/first-time-employees/":
                  "First-time employees with gross wages up to Rs 1 lakh per month receive one month's EPF wage up "
                  "to Rs 15,000 in two instalments. UAN must be activated through face authentication on UMANG. "
                  "Grievances: https://epfigms.gov.in/"},
        "brief": {"subject": "PM Viksit Bharat Rozgar Yojana, Part A", "aliases": ["PM-VBRY", "ELI scheme"],
                  "administering_body": "EPFO", "what_it_is": "Employment incentive for first-time employees.",
                  "complaint_type": "govt_scheme", "entitlement": "One month's EPF wage up to Rs 15,000.",
                  "conditions": ["First-time EPFO member.", "Gross wages up to Rs 1 lakh per month.",
                                 "UAN activated through face authentication on UMANG."],
                  "common_failure_reasons": ["UAN not activated."], "documents_needed": ["UAN"],
                  "official_grievance_channel": "EPFiGMS: https://epfigms.gov.in/",
                  "sources": ["https://pmvbry.epfindia.gov.in/first-time-employees/",
                              "https://invented-by-the-model.example/"]},
        "body": "EPFO", "portal": "EPFiGMS", "url": "https://epfigms.gov.in/",
    },
    "fictional": {  # a made-up state scheme on a made-up .gov.in host: proves nothing is scheme-specific
        "entity": "Gramin Solar Pump Yojana",
        "complaint": "My subsidy under the Gramin Solar Pump Yojana has not come for 8 months after installation.",
        "pages": {"https://solarpump.agri.example.gov.in/guidelines":
                  "Farmers with up to 5 acres get a 60% subsidy after the pump is inspected. Grievances: "
                  "https://solarpump.agri.example.gov.in/grievance"},
        "brief": {"subject": "Gramin Solar Pump Yojana", "aliases": ["GSPY"],
                  "administering_body": "State Agriculture Department", "what_it_is": "Solar pump subsidy.",
                  "complaint_type": "govt_scheme", "entitlement": "60% subsidy on the pump.",
                  "conditions": ["Land holding up to 5 acres.", "Pump inspected after installation."],
                  "common_failure_reasons": [], "documents_needed": [],
                  "official_grievance_channel": "Scheme grievance cell: https://solarpump.agri.example.gov.in/grievance "
                                                "or https://not-in-sources.example/form",
                  "sources": ["https://solarpump.agri.example.gov.in/guidelines"]},
        "body": "State Agriculture Department", "portal": "Scheme grievance cell",
        "url": "https://solarpump.agri.example.gov.in/grievance",
    },
}


def mock_research(monkeypatch, tmp_path, case, calls=None):
    import saathi.nodes.case_research as research_mod

    monkeypatch.setattr(research_mod, "BRIEF_CACHE", tmp_path)
    url = next(iter(case["pages"]))
    results = {"organic_results": [{"position": 1, "title": "Guidelines", "link": url}]}

    def search(params, live=True, **_):
        (calls if calls is not None else []).append(params["q"])
        return results if case["entity"] in params["q"] else SERP.get(params.get("q"), {})
    monkeypatch.setattr(serp, "search", search)
    monkeypatch.setattr(research_mod, "fetch", lambda u, **_: {"url": u, "ok": True, "title": "Guidelines",
                                                               "text": case["pages"].get(u, "")})
    monkeypatch.setattr(llm, "json_call", lambda system, *a, **k: dict(case["brief"])
                        if system == research_mod.RESEARCH_SYSTEM else fake_llm.answer(system, *a, **k))


@pytest.mark.parametrize("key", list(SCHEMES))
def test_scheme_research_frames_letter_from_sources(mocked, monkeypatch, tmp_path, key):
    case = SCHEMES[key]
    mock_research(monkeypatch, tmp_path, case)
    app, thread, state, nodes = run_graph(case["complaint"])

    assert nodes.index("intake_planner") < nodes.index("case_research") < nodes.index("gap_check")
    assert case["entity"] in state["case_entities"]
    assert state["sector"] == "govt_scheme" and state["company"] == case["body"]
    assert state["clauses"] == [] and not any(p["worker"] == "grievance" for p in state["search_plan"])
    brief = state["case_brief"]
    assert brief["sources"] == [next(iter(case["pages"]))]                  # unfetched URLs are dropped
    assert "not-in-sources.example" not in brief["official_grievance_channel"]  # unsourced URLs are dropped
    assert state["escalation"][0]["name"].startswith(case["portal"]) and state["escalation"][0]["url"] == case["url"]
    assert len(state["gap_questions"]) == len(brief["conditions"])          # no LLM gap check: all unknown
    draft = state["draft"].casefold()
    assert "condition-by-condition review" in draft and case["brief"]["subject"].casefold() in draft
    assert "consumer protection" not in draft and "national consumer helpline" not in draft

    assert state["eligibility"]["status"] == "unconfirmed"
    first = state["eligibility_checks"][0]["condition"]
    # Answering a review question re-assesses the conditions and redrafts; the case pauses for review again.
    state = G.resume(thread, {"case_answers": {first: "Yes, confirmed on 3 March 2026."}}, app=app)
    assert "__interrupt__" in state and not state.get("final")
    assert state["eligibility_checks"][0]["status"] == "met" and len(state["gap_questions"]) == len(brief["conditions"]) - 1
    assert "confirmed on 3 march 2026" in state["draft"].casefold()          # written into the letter body
    assert state["__interrupt__"]["review_round"] == 2
    letter = G.resume(thread, {}, app=app)["final"]["letter"]                 # approve without new answers
    assert letter.count("3 March 2026") == 1 and next(iter(case["pages"])) in letter


def test_review_answer_that_fails_a_condition_is_flagged_not_claimed(mocked, monkeypatch, tmp_path):
    case = SCHEMES["fictional"]
    mock_research(monkeypatch, tmp_path, case)
    app, thread, state, _ = run_graph(case["complaint"])
    cond = state["eligibility_checks"][0]["condition"]
    state = G.resume(thread, {"case_answers": {cond: "No, I have 8 acres."}}, app=app)
    assert state["eligibility"]["status"] == "may_not_qualify" and cond.rstrip(".") in state["eligibility"]["message"]
    assert "explain the decision" in state["draft"]


def test_critic_blocks_unsupported_eligibility_claim():
    from saathi.nodes.critic import eligibility_issues

    checks = [{"condition": "Land holding up to 5 acres.", "status": "unknown"}]
    assert eligibility_issues({"eligibility_checks": checks}, "I am eligible for this subsidy.")
    assert not eligibility_issues({"eligibility_checks": checks}, "Please confirm whether I qualify.")
    assert not eligibility_issues({"eligibility_checks": [{**checks[0], "status": "met"}]}, "I am eligible.")


def test_case_brief_is_cached_under_every_name(monkeypatch, tmp_path):
    import saathi.nodes.case_research as research_mod

    case, calls = SCHEMES["pm-vbry"], []
    mock_research(monkeypatch, tmp_path, case, calls)
    state = {"case_entities": ["PM-VBRY"], "complaint": case["complaint"], "sector": "other", "live": True}
    assert research_mod.case_research(state)["case_brief"]["administering_body"] == "EPFO"
    n = len(calls)
    for alias in ("pm vbry", "PM Viksit Bharat Rozgar Yojana, Part A", "ELI scheme"):
        out = research_mod.case_research({**state, "case_entities": [alias]})
        assert out["case_brief"]["subject"] == case["brief"]["subject"] and "cached" in out["trace"][0]
    assert len(calls) == n                                                    # no further searches


def test_ai_unavailable_stops_the_run_instead_of_falling_back(mocked, monkeypatch):
    def down(*a, **k):
        raise llm.AIUnavailable("the AI service is not configured")
    monkeypatch.setattr(llm, "json_call", down)
    with pytest.raises(llm.AIUnavailable):
        run_graph("Kartify refused a refund for my damaged phone", company="Kartify")


def test_case_research_without_sources_assumes_nothing(monkeypatch, tmp_path):
    import saathi.nodes.case_research as research_mod
    monkeypatch.setattr(research_mod, "BRIEF_CACHE", tmp_path)
    monkeypatch.setattr(research_mod.serp, "search", lambda *a, **k: {})
    out = research_mod.case_research({"case_entities": ["PM-VBRY"], "complaint": "x", "sector": "other",
                                      "live": True})
    assert out["case_brief"] == {} and "nothing assumed" in out["trace"][0]


def test_critic_rejects_consumer_frame_for_government_scheme():
    from saathi.nodes.critic import case_specific_issues

    state = {"sector": "govt_scheme", "case_entities": ["PM-VBRY"],
             "case_brief": {"subject": "PM-VBRY", "complaint_type": "govt_scheme",
                            "official_grievance_channel": "EPFiGMS"},
             "facts": {"date": "1 August 2025", "amount": "50000"}}
    issues = case_specific_issues(
        state, "I claim deficiency in service under consumer protection and will complain to the National Consumer Helpline.")

    assert any("consumer-law/consumer-helpline frame" in issue for issue in issues)
    assert any("Name the researched" in issue for issue in issues)


def test_extraction_blocks_values_not_on_page(monkeypatch):
    page = {"final_url": "https://x.example/g", "text": "Grievance Officer Mr. Ravi Kumar email ravi@x.example"}
    monkeypatch.setattr(grievance_mod.llm, "json_call", lambda *a, **k: {"contacts": [
        {"organisation": "X", "belongs_to_named_company": True, "role": "grievance_officer", "name": "Ravi Kumar",
         "email": "ravi@x.example", "phone": "9999988888"},
        {"organisation": "X", "belongs_to_named_company": True, "role": "nodal_officer", "name": "Invented Person",
         "email": "nodal@x.example", "phone": ""}]})
    out, blocked, _ = grievance_mod.llm_contacts("X", page)
    assert [o["value"] for o in out] == ["ravi@x.example"] and blocked == 3


def test_decoys_all_caught():
    from eval.run_eval import decoy_eval
    r = decoy_eval()
    assert r["decoy_catch_rate"] == 100.0 and r["false_rejection_rate"] == 0.0


def test_verifier_two_independent_sources():
    obs = [{"type": "email", "value": "care@acme.example", "role": "customer_care", "name": "", "timeline": "",
            "evidence": "", "source_url": f"https://{d}/p", "source_domain": d, "via": "page", "page_title": ""}
           for d in ("news-a.example", "news-b.example")]
    c = V.verify("Acme", obs, ["acme.example"])[0]
    assert c["verified"] and "2 independent" in c["reason"]


def test_loan_app_registry_worker(mocked):
    from saathi.nodes import registry_worker
    if not registry_worker.available():
        pytest.skip("LoanLens registry not present")
    _, _, s, nodes = run_graph("KreditBee recovery agents are threatening my relatives")
    assert "registry" in nodes and s["sector"] == "loan_app"
    assert s["recommended"]["source_type"] == "registry"
    assert registry_worker.check_app("KreditBee Loan Pro")["status"] == "lookalike"


def test_search_failure_is_not_fatal(mocked, monkeypatch):
    from saathi.tools.serp import SerpApiError

    def flaky(params, live=True, **_):
        if params.get("q") == "Kartify grievance officer email":
            raise SerpApiError("SerpApi unreachable: ReadTimeout")
        return SERP.get(params.get("q"), {})
    monkeypatch.setattr(serp, "search", flaky)
    _, _, s, _ = run_graph("Kartify refused a refund", company="Kartify", city="Pune")
    assert any("ReadTimeout" in e for e in s["errors"]) and s["__interrupt__"]["draft"]
    assert s["recommended"]["value"] == "grievance@kartify.example"  # the site: query still worked


def test_failed_request_is_not_charged(monkeypatch, tmp_path):
    import requests

    import saathi.tools.serp as serp_mod
    client = serp_mod.SerpClient(api_key="k", cache_dir=tmp_path, max_credits=5, verbose=False)
    monkeypatch.setattr(serp_mod.time, "sleep", lambda s: None)

    def timeout(*a, **k):
        raise requests.exceptions.ReadTimeout("timed out")
    monkeypatch.setattr(serp_mod.requests, "get", timeout)
    with pytest.raises(serp_mod.SerpApiError):
        client.search({"engine": "google", "q": "x"})
    assert client.live_calls == 0


def test_search_cache_writes_unicode_as_utf8(monkeypatch, tmp_path):
    import saathi.tools.serp as serp_mod

    unicode_title = "Consumer Commission\u202f\u0926\u093f\u0932\u094d\u0932\u0940"
    response = {"local_results": [{"title": unicode_title}]}

    class FakeResponse:
        status_code = 200

        def json(self):
            return response

    monkeypatch.setattr(serp_mod.requests, "get", lambda *args, **kwargs: FakeResponse())
    client = serp_mod.SerpClient(api_key="test", cache_dir=tmp_path, verbose=False)
    params = {"engine": "google_maps", "q": "Commission Delhi"}

    assert client.search(params)["local_results"][0]["title"] == unicode_title
    assert unicode_title in client._path(params).read_text(encoding="utf-8")


def test_llm_calls_gemini_generate_content(monkeypatch, tmp_path):
    calls = []

    class FakeModels:
        def generate_content(self, **kw):
            calls.append(kw)
            if len(calls) == 1:  # first attempt fails -> exercises the retry
                raise RuntimeError("503 UNAVAILABLE")
            return types.SimpleNamespace(text='{"sector": "telecom"}')
    monkeypatch.setattr(llm, "client", lambda: types.SimpleNamespace(models=FakeModels(), interactions=object()))
    monkeypatch.setattr(llm, "LLM_PROVIDER", "")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm, "LLM_CACHE", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    schema = {"type": "object", "properties": {"sector": {"type": "string"}}, "required": ["sector"]}
    assert llm._real_json_call("sys", "Jio charged me", schema) == {"sector": "telecom"}
    assert len(calls) == 2 and llm.api() == "generate_content"
    kw = calls[-1]
    assert kw["model"] == llm.GEMINI_MODEL and kw["contents"] == "Jio charged me"
    assert kw["config"]["system_instruction"] == "sys" and kw["config"]["response_json_schema"] == schema
    assert kw["config"]["thinking_config"] == {"thinking_level": "low"}


def test_llm_calls_gemini_interactions_when_opted_in(monkeypatch, tmp_path):
    calls = []

    class FakeInteractions:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:  # first attempt fails -> exercises the retry
                raise RuntimeError("503 UNAVAILABLE")
            return types.SimpleNamespace(status="completed",
                                         output_text='{"sector": "telecom", "label": "\\u0939\\u093f\\u0928\\u094d\\u0926\\u0940\\u202ftext"}')
    monkeypatch.setattr(llm, "LLM_API", "interactions")
    monkeypatch.setattr(llm, "client", lambda: types.SimpleNamespace(interactions=FakeInteractions()))
    monkeypatch.setattr(llm, "LLM_PROVIDER", "")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm, "LLM_CACHE", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    schema = {"type": "object", "properties": {"sector": {"type": "string"}}, "required": ["sector"]}
    assert llm._real_json_call("sys", "Jio charged me", schema) == {
        "sector": "telecom", "label": "\u0939\u093f\u0928\u094d\u0926\u0940\u202ftext"}
    assert len(calls) == 2
    assert "\u0939\u093f\u0928\u094d\u0926\u0940\u202ftext" in next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    kw = calls[-1]
    assert kw["model"] == llm.GEMINI_MODEL and kw["input"] == "Jio charged me" and kw["system_instruction"] == "sys"
    assert kw["response_format"] == {"type": "text", "mime_type": "application/json", "schema": schema}
    assert kw["generation_config"] == {"temperature": 0.1, "thinking_level": "low"}


def test_gemini_schema_inlines_pydantic_refs():
    from saathi.state import IntakeOut, schema
    g = llm.gemini_schema(schema(IntakeOut))
    assert "$defs" not in json.dumps(g) and "$ref" not in json.dumps(g)
    assert g["properties"]["facts"]["properties"]["order_id"]["type"] == "string"


# --------------------------------------------------------------------------- safety router, domains and tiers

@pytest.mark.parametrize("text,cats", [
    ("My husband beats me every night and threatened to kill me", ["violence"]),
    ("मेरा बेटा कल से लापता है", ["missing_or_abducted"]),
    ("I want to end my life, the bank took all my money", ["self_harm"]),
    ("pati maarpeet karta hai", ["violence"]),
    ("Zomato gave me food poisoning, I want a refund", []),          # a consumer complaint, not an emergency
    ("The work has begun but the builder is late", []),
    ("Recovery agents are threatening me over a late EMI", []),      # urgent, but routed, not an emergency
])
def test_safety_router(text, cats):
    from saathi.nodes.safety import safety_check
    r = safety_check(text)
    assert r["categories"] == cats and (r["level"] == "emergency") == bool(cats)
    assert not cats or "112" in r["numbers"] or "14416" in r["numbers"]


def test_tiers_follow_the_playbook():
    from saathi.playbooks import tier_of
    assert (tier_of("police"), tier_of("legal"), tier_of("ecommerce"), tier_of("unknown_key")) == ("B", "C", "A", "A")


def test_tier_b_cyber_fraud_is_a_police_complaint_with_1930_first(mocked):
    _, thread, s, nodes = run_graph("A fake customer care caller took my OTP and Rs 40,000 was stolen from my "
                                    "account through UPI on 2 Oct 2026.", city="Pune")
    assert s["sector"] == "police" and s["tier"] == "B" and s["company"] == ""
    assert "grievance" not in nodes and s["clauses"] == []
    assert s["escalation"][0]["phone"] == "1930" and "Station House Officer" in s["escalation"][1]["name"]
    assert "FIR" in s["draft"] and "[POLICE STATION]" in s["draft"] and "consumer protection" not in s["draft"].lower()
    assert s["urgent_note"] and "1930" in s["urgent_note"]


def test_tier_b_letter_goes_to_the_sho(mocked):
    app, thread, s, _ = run_graph("Police at the station refused to register my FIR for the theft of my bike "
                                  "on 1 Oct 2026.")
    f = G.resume(thread, {"fills": {"[POLICE STATION]": "Kotwali"}}, app=app)["final"]
    assert f["tier"] == "B" and f["letter"].startswith("To,\nThe Station House Officer\nKotwali Police Station")
    assert "signed, stamped receipt" in f["letter"] and not f["cited"]


def test_tier_c_is_an_explainer_with_legal_aid_questions(mocked):
    app, thread, s, _ = run_graph("My brother is refusing to partition our ancestral property and the case is in "
                                  "civil court. Case CNR MHPU010012342021.", city="Pune")
    assert s["sector"] == "legal" and s["tier"] == "C" and s["clauses"] == []
    assert len(s["eligibility_checks"]) == 8 and s["eligibility"]["status"] == "unconfirmed"
    assert "MHPU010012342021" in s["draft"] and "15100" in s["draft"]
    woman = next(c["condition"] for c in s["eligibility_checks"] if "woman" in c["condition"])
    s = G.resume(thread, {"case_answers": {woman: "Yes"}}, app=app)          # one criterion is enough
    assert s["eligibility"]["status"] == "meets_listed" and "free legal aid" in s["eligibility"]["message"]
    f = G.resume(thread, {}, app=app)["final"]
    assert f["tier"] == "C" and "Dear Sir" not in f["letter"] and "not legal advice" in f["letter"]


def test_emergency_numbers_come_first(mocked):
    app, thread, s, nodes = run_graph("My husband beats me and threatened to kill me. Police did not register my "
                                      "complaint.")
    assert nodes.index("safety_router") < nodes.index("case_research") and s["safety"]["level"] == "emergency"
    assert s["__interrupt__"]["safety"]["numbers"][:2] == ["112", "181"]
    f = G.resume(thread, {}, app=app)["final"]
    assert f["markdown"].startswith("## ⚠️ If anyone is in danger right now") and "112" in f["markdown"]


def test_critic_rejects_court_drafting_for_tier_c():
    from saathi.nodes.critic import tier_issues
    issues = tier_issues({"sector": "legal"}, "IN THE COURT OF the Civil Judge. The petitioner humbly prays…")
    assert any("court filing" in i for i in issues) and any("Legal Services" in i for i in issues)
    assert not tier_issues({"sector": "legal"}, "Contact the District Legal Services Authority (15100).")


# --------------------------------------------------------------------------- deadlines and consistency (code, not model)

def _ladder(sector):
    return [dict(s) for s in __import__("saathi.playbooks", fromlist=["SECTORS"]).SECTORS[sector]["escalation"]]


def test_deadlines_are_computed_from_the_rules():
    from datetime import date

    from saathi import deadlines
    start = date(2026, 10, 9)
    steps = _ladder("ecommerce")
    dl = deadlines.annotate(steps, start=start, incident=date(2026, 9, 15))
    assert steps[0]["ack_by"] == "11 October 2026" and steps[0]["reply_by"] == "9 November 2026"   # 48 h, 1 month
    assert dl["date"] == "9 November 2026" and "Rule 4(5)" in dl["source"] and dl["kind"] == "statutory"
    assert steps[1]["from_date"] == "9 November 2026"
    assert steps[2]["file_by"] == "15 September 2028"                                             # CPA s.69: 2 years
    ins = _ladder("insurance")
    deadlines.annotate(ins, start=start)
    assert ins[0]["reply_by"] == "9 November 2026" and ins[2]["file_by"] == "9 November 2027"    # 1 month, then 1 year
    tel = _ladder("telecom")
    deadlines.annotate(tel, start=start)
    assert tel[0]["reply_by"] == "12 October 2026" and tel[1]["file_by"] == "11 November 2026"   # 3 days, +30 days
    assert deadlines.annotate(_ladder("police"), start=start) == {}                              # no rule, no date
    assert deadlines.fmt(deadlines.add_months(date(2026, 1, 31), 1)) == "28 February 2026"


def test_critic_flags_amounts_and_dates_that_do_not_match():
    from datetime import date

    from saathi.nodes.critic import consistency_issues
    state = {"complaint": "Myntra order on 28 Sep 2026 for Rs 21,499 arrived broken.", "sector": "ecommerce",
             "facts": {"amount": "21499", "date": "28 Sep 2026"},
             "deadline": {"date": "9 November 2026", "basis": "one month", "kind": "statutory"}}
    good = "I paid ₹21,499 on 28 September 2026. Please resolve it by 9 November 2026."
    assert consistency_issues(state, good, today=date(2026, 10, 9)) == []
    bad = consistency_issues(state, "I paid Rs. 25,000 on 30 Sep 2026. Please reply within 30 days.",
                             today=date(2026, 10, 9))
    assert any("25,000" in i for i in bad) and any("30 Sep 2026" in i for i in bad)
    assert any("9 November 2026" in i for i in bad)                                              # deadline missing


def test_template_letter_carries_the_computed_deadline(mocked):
    _, _, s, _ = run_graph("Kartify refused a refund for my damaged phone, order KT12345678, Rs 15,000.",
                           company="Kartify")
    assert s["deadline"]["kind"] == "statutory" and s["deadline"]["date"] in s["draft"]
    assert s["escalation"][0]["reply_by"] == s["deadline"]["date"] and "within 30 days" not in s["draft"]


# --------------------------------------------------------------------------- other organisations on the company's page

PARTNER_PAGE = ("Grievance Officer: Ms. Asha Rao, grievance@kartify.example. Lending partners of Kartify Pay: "
                "1. Axis Bank: nodal.officer@axisbank.com. 3. EarlySalary Services Private Limited: GRO Name: Mr. Amit "
                "Nosina, GRO Email: grievance@earlysalary.com, Customer care Number: 020-67639797.")


def test_other_organisations_contacts_on_official_page_are_dropped(monkeypatch):
    monkeypatch.setattr(grievance_mod, "fetch", lambda url, **_: {"url": url, "final_url": url, "ok": True,
                                                                  "text": PARTNER_PAGE, "mailtos": [], "tels": [],
                                                                  "title": "Partners"})
    monkeypatch.setattr(grievance_mod.llm, "json_call", lambda *a, **k: {"contacts": [
        {"organisation": "Kartify", "belongs_to_named_company": True, "role": "grievance_officer",
         "name": "Asha Rao", "email": "grievance@kartify.example"},
        {"organisation": "Axis Bank", "belongs_to_named_company": False, "role": "nodal_officer",
         "email": "nodal.officer@axisbank.com"},
        {"organisation": "EarlySalary Services Private Limited", "belongs_to_named_company": False,
         "role": "grievance_officer", "name": "Amit Nosina", "email": "grievance@earlysalary.com",
         "phone": "020-67639797"}]})
    ext = grievance_mod.extract_from_pages("Kartify", [{"url": "https://www.kartify.example/partners", "title": "x",
                                                        "snippet": "", "on_official": True, "position": 1}])
    vals = {o["value"] for o in ext["observations"]}
    assert "grievance@kartify.example" in vals
    assert not vals & {"grievance@earlysalary.com", "nodal.officer@axisbank.com"}
    assert not any("67639797" in v for v in vals) and len(ext["other_org"]) == 3


def test_off_brand_email_is_warned_and_not_preferred():
    obs = [{"type": "email", "value": v, "role": "grievance_officer", "name": "", "timeline": "", "evidence": "",
            "source_url": "https://www.kartify.example/partners", "source_domain": "kartify.example", "via": "page",
            "page_title": ""} for v in ("grievance@earlysalary.com", "care@kartify.example")]
    out = {c["value"]: c for c in V.verify("Kartify", obs, ["kartify.example"])}
    assert out["grievance@earlysalary.com"]["off_brand"] and out["grievance@earlysalary.com"]["warnings"]
    assert V.recommended(list(out.values()))["value"] == "care@kartify.example"   # own domain wins


# --------------------------------------------------------------------------- one details form, then one draft

def test_details_form_comes_before_drafting_and_asks_everything(mocked, monkeypatch, tmp_path):
    case = SCHEMES["fictional"]
    mock_research(monkeypatch, tmp_path, case)
    app, thread, s, nodes = run_graph(case["complaint"], stop_at_form=True)
    form = s["__interrupt__"]
    assert form["stage"] == "details" and "drafter" not in nodes                       # nothing drafted yet
    assert [q["condition"] for q in form["questions"]] == case["brief"]["conditions"]    # every open condition
    first = form["questions"][0]["key"]
    nodes2 = []
    s = G.resume(thread, {"case_answers": {first: "Yes, 3 acres."}}, app=app,
                 on_update=lambda n, u: nodes2.append(n))
    assert s["__interrupt__"]["stage"] == "review"
    assert nodes2.index("reassess") < nodes2.index("drafter") and nodes2.count("drafter") == 1
    assert s["eligibility_checks"][0]["status"] == "met" and "3 acres" in s["draft"]
    f = G.resume(thread, {}, app=app)["final"]
    assert f["letter"].count("3 acres") == 1                                             # not appended twice


def test_details_form_fills_missing_facts_before_drafting(mocked):
    app, thread, s, _ = run_graph("Kartify refused a refund for my damaged phone, order KT12345678, Rs 15,000.",
                                  company="Kartify", stop_at_form=True)
    form = s["__interrupt__"]
    assert form["stage"] == "details" and "[ORDER DATE]" in {m["placeholder"] for m in form["missing_facts"]}
    s = G.resume(thread, {"fills": {"[ORDER DATE]": "15 Sep 2026"}}, app=app)
    assert s["__interrupt__"]["stage"] == "review" and "15 Sep 2026" in s["draft"] and "[ORDER DATE]" not in s["draft"]
    assert not s["missing_facts"] and s["facts"]["date"] == "15 Sep 2026"


def test_no_form_when_nothing_is_missing(mocked):
    _, _, s, nodes = run_graph("Kartify refused a refund for my damaged phone, order KT12345678, Rs 15,000 on "
                               "15 Sep 2026.", company="Kartify", stop_at_form=True)
    assert s["__interrupt__"]["stage"] == "review" and "drafter" in nodes


def test_contacts_the_llm_did_not_attribute_are_not_verified(monkeypatch):
    text = ("Grievance Officer: grievance@kartify.example. Partner NBFC: help@smartcoin.co.in, 9380031807. "
            "Infringement: legal@kartify.example")
    monkeypatch.setattr(grievance_mod, "fetch", lambda url, **_: {"url": url, "final_url": url, "ok": True,
                                                                  "text": text, "mailtos": [], "tels": [],
                                                                  "title": "p"})
    monkeypatch.setattr(grievance_mod.llm, "json_call", lambda *a, **k: {"contacts": [   # sees only its own GRO
        {"organisation": "Kartify", "belongs_to_named_company": True, "role": "grievance_officer",
         "email": "grievance@kartify.example"}]})
    ext = grievance_mod.extract_from_pages("Kartify", [{"url": "https://www.kartify.example/p", "title": "x",
                                                        "snippet": "", "on_official": True, "position": 1}])
    out = {c["value"]: c for c in V.verify("Kartify", ext["observations"], ["kartify.example"])}
    assert out["grievance@kartify.example"]["status"] == "verified"
    assert out["legal@kartify.example"]["status"] == "verified"                  # unknown owner, but on-brand
    assert out["help@smartcoin.co.in"]["status"] == "unverified"                 # unknown owner, off-brand
    assert out["9380031807"]["status"] == "unverified"                           # unknown owner, bare number


def test_translation_happens_once_at_intake(mocked, monkeypatch):
    from saathi.nodes import intake as intake_mod
    from saathi.nodes import legal_rag as rag_mod
    calls, queries = [], []

    def fake(system, prompt, schema, **_):
        calls.append(system[:40])
        if system == intake_mod.INTAKE_SYSTEM:
            return {"language": "hi", "complaint_en": "Kartify did not refund Rs 15,000 for my damaged phone.",
                    "domain_free_text": "online shopping refund", "matched_playbook": "ecommerce",
                    "match_confidence": 0.9, "company": "Kartify", "issue_types": ["refund"],
                    "facts": {"issue": "Refund refused for a damaged phone", "amount": "15000",
                              "order_id": "KT12345678", "date": "15 Sep 2026"},
                    "urgent_fraud_or_harassment": False, "search_keywords": "refund damaged phone",
                    "case_entities": [], "follow_up_questions": ["क्या आपने कंपनी से पहले शिकायत की थी?"]}
        return fake_llm.answer(system, prompt, schema)
    monkeypatch.setattr(llm, "json_call", fake)
    real = rag_mod.retrieve
    monkeypatch.setattr(rag_mod, "retrieve", lambda q, *a, **k: (queries.append(q), real(q, *a, **k))[1])
    _, _, s, _ = run_graph("कार्टिफाई ने मेरे खराब फोन का 15,000 रुपये का रिफंड नहीं दिया। ऑर्डर KT12345678, 15 Sep 2026",
                           company="Kartify", stop_at_form=True)
    assert s["complaint_en"].startswith("Kartify did not refund")
    assert s["__interrupt__"]["questions"][0]["question"] == "क्या आपने कंपनी से पहले शिकायत की थी?"
    assert sum(c.startswith("You triage") for c in calls) == 1                   # the one translation
    assert not any(c.startswith("Translate") or c.startswith("Compare every") for c in calls)  # no re-translation,
    assert queries and all("Refund refused for a damaged phone" in q or not re.search("[ऀ-ॿ]", q)  # no gap LLM
                           for q in queries)


def test_intake_call_also_catches_danger_the_rules_missed(mocked, monkeypatch):
    from saathi.nodes import intake as intake_mod
    calls = []

    def fake(system, prompt, schema, **_):
        calls.append(system[:20])
        if system == intake_mod.INTAKE_SYSTEM:
            return {"language": "en", "complaint_en": prompt, "domain_free_text": "bank lost savings",
                    "matched_playbook": "bank", "match_confidence": 0.8, "company": "", "issue_types": ["other"],
                    "facts": {}, "urgent_fraud_or_harassment": False, "search_keywords": "lost savings",
                    "danger": ["self_harm"]}
        return fake_llm.answer(system, prompt, schema)
    monkeypatch.setattr(llm, "json_call", fake)
    text = "The bank lost all my savings. I can't go on anymore and see no point in anything."
    from saathi.nodes.safety import safety_check
    assert safety_check(text)["level"] == "none"                                   # the rules miss it
    _, _, s, _ = run_graph(text, stop_at_form=True)
    assert s["safety"]["level"] == "emergency" and "14416" in s["safety"]["numbers"]
    assert "rules + " in s["safety"]["method"] and calls.count(calls[0]) == 1 and calls[0] == "You triage Indian gr"
    assert not any(c.startswith("Decide whether") for c in calls)                    # no separate safety LLM call


# --------------------------------------------------------------------------- routing and the escalation planner

FLIGHT = "IndiGo cancelled my flight 6E-2143 on 2 Oct 2026 and has not refunded Rs 7,400 after 30 days."
ROUTE_PAGES = {
    "https://www.airsewa.gov.in/grievance": ("AirSewa - grievance redressal for air passengers, Ministry of Civil "
                                             "Aviation. If the airline does not resolve your complaint, file it on "
                                             "AirSewa at https://www.airsewa.gov.in . DGCA oversees airlines."),
    "https://flight-refund-help.example/blog": "Call FlightRefundHelp at our hotline for quick refunds!",
}


def fake_intake(**over):
    base = {"language": "en", "complaint_en": FLIGHT, "domain_free_text": "air travel - flight cancellation refund",
            "matched_playbook": "govt_scheme", "match_confidence": 0.3, "route_reason": "nearest bucket",
            "company": "IndiGo", "issue_types": ["refund"], "route_key": "airline", "state": "Maharashtra",
            "authority_type": "private_company", "route_queries": ["IndiGo refund complaint AirSewa"],
            "facts": {"issue": "Flight cancelled, refund not paid", "amount": "7400", "date": "2 Oct 2026"},
            "urgent_fraud_or_harassment": False, "search_keywords": "flight refund"}
    return base | over


def plan_answer(**over):
    steps = [
        {"order": 1, "authority": "AirSewa", "level": "sector_regulator_or_ombudsman", "channel": "portal",
         "url": "https://www.airsewa.gov.in", "when_to_use": "If the airline does not resolve it",
         "source_url_or_clause": "https://www.airsewa.gov.in/grievance"},
        {"order": 2, "authority": "DGCA", "level": "sector_regulator_or_ombudsman", "channel": "portal",
         "url": "https://dgca.gov.in/complaints", "when_to_use": "Regulator", "timeline": "15 days",
         "source_url_or_clause": "https://www.airsewa.gov.in/grievance"},
        {"order": 3, "authority": "FlightRefundHelp", "level": "sector_regulator_or_ombudsman", "channel": "phone",
         "when_to_use": "Anytime", "source_url_or_clause": "https://flight-refund-help.example/blog"}]
    return {"steps": steps, "notes": ""} | over


def mock_planner(monkeypatch, tmp_path, intake_outputs, calls, plan=None):
    from saathi.nodes import escalation
    from saathi.nodes import intake as intake_mod
    monkeypatch.setattr(escalation, "LADDER_DIR", tmp_path)
    outs = iter(intake_outputs)

    def fake(system, prompt, schema, **_):
        calls.append(system[:25])
        if system == intake_mod.INTAKE_SYSTEM:
            return next(outs)
        if system == escalation.PLAN_SYSTEM:
            return plan or plan_answer()
        return fake_llm.answer(system, prompt, schema)
    monkeypatch.setattr(llm, "json_call", fake)
    monkeypatch.setattr(serp, "search", lambda params, live=True, **_: {"organic_results": [
        {"link": u, "title": "t"} for u in ROUTE_PAGES]} if "AirSewa" in params.get("q", "") else {})
    monkeypatch.setattr(escalation, "fetch", lambda u, **_: {"url": u, "ok": True, "title": "t",
                                                             "text": ROUTE_PAGES.get(u, "")})


def test_unmatched_domain_gets_a_validated_ladder_from_evidence(mocked, monkeypatch, tmp_path):
    from saathi.nodes import escalation
    calls = []
    mock_planner(monkeypatch, tmp_path, [fake_intake(), fake_intake()], calls)
    _, _, s, nodes = run_graph(FLIGHT)
    assert s["route"]["mode"] == "research" and s["sector"] == "other"             # the forced match is not trusted
    assert "escalation_planner" in nodes and nodes.index("escalation_planner") < nodes.index("drafter")
    names = [x["name"] for x in s["escalation"]]
    assert names[0] == escalation.COMPANY_STEP["name"] and "AirSewa" in names
    assert "FlightRefundHelp" not in names                                         # non-official source: dropped
    dgca = next(x for x in s["escalation"] if x["name"] == "DGCA")
    assert dgca["url"] is None and dgca["timeline"].startswith("check the timeline in")  # no invented link / time
    assert all(x.get("source") for x in s["escalation"])
    assert s["escalation"][0].get("reply_by")                  # the company step keeps the requested reply deadline
    # Same kind of complaint again: the verified ladder comes from the cache - no planning call.
    n = calls.count(escalation.PLAN_SYSTEM[:25])
    _, _, s2, _ = run_graph(FLIGHT)
    assert calls.count(escalation.PLAN_SYSTEM[:25]) == n and s2["ladder"]["cached"]


def test_seed_ladder_survives_when_the_planner_finds_nothing(mocked):
    from saathi.playbooks import SECTORS
    _, _, s, nodes = run_graph("Kartify refused a refund for my damaged phone, order KT12345678, Rs 15,000.",
                               company="Kartify")
    assert s["sector"] == "ecommerce" and "escalation_planner" in nodes          # planner ran for a seeded sector
    assert [x["name"] for x in s["escalation"]] == [x["name"] for x in SECTORS["ecommerce"]["escalation"]]
    assert s["escalation"][0]["reply_by"]                                       # statutory deadline kept


def test_planner_only_adds_to_a_seed_ladder():
    from saathi.nodes.escalation import merge_with_seed
    from saathi.playbooks import SECTORS
    seed = SECTORS["bank"]["escalation"]
    planned = [{"name": "HDFC dispute desk", "level": "company", "source": "x"},
               {"name": "Reserve Bank Integrated Ombudsman", "level": "sector_regulator_or_ombudsman", "source": "y"},
               {"name": "Banking Codes and Standards Board", "level": "other", "source": "https://x.gov.in"}]
    merged, added = merge_with_seed(seed, planned)
    names = [x["name"] for x in merged]
    assert added == ["Banking Codes and Standards Board"]                       # company / duplicate steps skipped
    assert names[: len(seed) - 1] == [x["name"] for x in seed[:-1]]            # every seed step kept, in order
    assert names.index("Banking Codes and Standards Board") < names.index("District Consumer Commission")


def test_ladder_validation_rules():
    from saathi.nodes.escalation import validate
    ev = [{"url": "https://power.example.gov.in/cgrf", "official": True, "text": "CGRF decides within 45 days. "
           "Grievances may also go to the CM helpline. See the e-seva portal www.gas-seva.in"},
          {"url": "https://www.gas-seva.in/help", "official": False, "text": "e-Seva grievance portal"}]
    plan = {"steps": [
        {"order": 1, "authority": "CPGRAMS", "level": "central_portal", "channel": "pgportal.gov.in",
         "when_to_use": "x", "source_url_or_clause": "https://power.example.gov.in/cgrf"},
        {"order": 2, "authority": "CGRF", "level": "sector_forum", "channel": "office", "when_to_use": "x",
         "timeline": "45 days", "source_url_or_clause": "https://power.example.gov.in/cgrf"},
        {"order": 3, "authority": "Gas e-Seva", "level": "central_portal", "channel": "portal", "when_to_use": "x",
         "source_url_or_clause": "https://www.gas-seva.in/help"}]}
    state_steps, notes, _ = validate(plan, [dict(e) for e in ev], [], [], "state_utility_or_psu")
    assert "CPGRAMS" not in [x["name"] for x in state_steps] and any("CPGRAMS" in n for n in notes)
    assert state_steps[0]["name"] == "CGRF" and state_steps[0]["timeline"] == "45 days"   # sector forum first
    assert "Gas e-Seva" in [x["name"] for x in state_steps]          # vouched for by an official page
    central, _, _ = validate(plan, [dict(e) for e in ev], [], [], "central_government")
    assert "CPGRAMS" in [x["name"] for x in central]                 # fine for a central body


def test_general_route_is_labelled_when_nothing_is_sourced(mocked, monkeypatch, tmp_path):
    from saathi.nodes import escalation
    calls = []
    only_blog = {"steps": [plan_answer()["steps"][2]], "notes": ""}
    mock_planner(monkeypatch, tmp_path, [fake_intake(authority_type="central_government")], calls, plan=only_blog)
    _, _, s, _ = run_graph(FLIGHT)
    assert s["ladder"]["fallback"] and s["ladder"]["note"] == escalation.FALLBACK_NOTE
    assert not list(tmp_path.iterdir())                               # a fallback is never cached as "verified"


def test_very_unclear_complaint_gets_one_question(mocked, monkeypatch, tmp_path):
    calls = []
    unclear = fake_intake(matched_playbook=None, match_confidence=0, company="", needs_clarification=True,
                          clarifying_question="Which company or office is this about?")
    clear = fake_intake(matched_playbook="other", match_confidence=0.8)
    mock_planner(monkeypatch, tmp_path, [unclear, clear], calls)
    app, thread, s, nodes = run_graph("They did not give my money back.", stop_at_form=True)
    assert s["__interrupt__"]["stage"] == "clarify" and "Which company" in s["__interrupt__"]["question"]
    s = G.resume(thread, {"answer": "IndiGo, a cancelled flight"}, app=app)
    assert "IndiGo, a cancelled flight" in s["complaint"] and s["clarified"]
    assert (s.get("__interrupt__") or {}).get("stage") != "clarify"                  # asked only once


def test_missing_facts_merge_case_specific_with_the_domain_minimum():
    from saathi.playbooks import merge_missing
    extra = [{"fact": "pnr", "placeholder": "[PNR / BOOKING NUMBER]"},
             {"fact": "date", "placeholder": "[FLIGHT DATE]"},                   # already in the minimum: skipped
             {"fact": "flight_number", "placeholder": "flight number"}]          # normalised to a [PLACEHOLDER]
    out = merge_missing("other", {"amount": "7400", "flight_number": ""}, extra)
    # overlapping wording upgrades the generic placeholder (same fact key, so the critic's check still holds)
    assert [(m["fact"], m["placeholder"]) for m in out] == [("reference_id", "[PNR / BOOKING NUMBER]"),
                                                            ("date", "[FLIGHT DATE]"), ("flight_number", "[FLIGHT NUMBER]")]
    assert all(m["fact"] != "amount" for m in out)                               # a fact the complaint gives


# --------------------------------------------------------------------------- one language per letter

def test_language_check_allows_placeholders_ids_and_names_only():
    from saathi.letter import language_issue
    hi = "मेरा आवेदन [APPLICATION REFERENCE NUMBER] (ID BR1234567) चार महीने से लंबित है। कृपया Kartify इसे देखें।"
    assert language_issue(hi, "hi", ["Kartify"]) == ""
    assert language_issue("मेरा आवेदन pending है and nobody replies to my emails at all", "hi")
    assert language_issue("My application is pending. मेरा आवेदन लंबित है।", "en")
    assert language_issue("My application [आवेदन संख्या] is pending.", "en") == ""


def test_hindi_letter_is_hindi_end_to_end(mocked):
    from saathi.nodes.review import addressee_block
    from saathi.letter import labels
    app, thread, s, _ = run_graph("Kartify refused a refund for my damaged phone, order KT12345678, Rs 15,000.",
                                  company="Kartify", letter_language="hi")
    assert s["letter_language"] == "hi"
    letter = G.resume(thread, {}, app=app)["final"]["letter"]
    for label in ("सेवा में,", "दिनांक:", "विषय:", "महोदय/महोदया,", "भवदीय,"):
        assert label in letter
    for english in ("To,", "Date:", "Subject:", "Dear Sir/Madam", "Yours faithfully"):
        assert english not in letter
    L = labels("en")
    block = addressee_block({"company": "Food and Civil Supplies Department", "route": {"state": "Bihar"},
                             "addressee_lines": ["State-specific Food and Civil Supplies Department portals"]},
                            None, "A", L)
    assert block == ["The Grievance Redressal Officer", "Food and Civil Supplies Department, Bihar"]  # no portal
