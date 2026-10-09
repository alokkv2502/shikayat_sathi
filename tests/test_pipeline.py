"""Graph tests on a FICTIONAL company (Kartify, *.example domains) with SerpApi and page fetches mocked."""
import json
import types

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import saathi.nodes.grievance as grievance_mod
import saathi.tools.domains as domains_mod
from saathi import graph as G
from saathi import llm
from saathi.nodes import verifier as V
from saathi.nodes.intake import rule_intake
from saathi.nodes.legal_rag import strip_unsupported
from saathi.playbooks import detect_language
from saathi.tools.serp import serp

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


def run_graph(text, **kw):
    app = G.build(InMemorySaver())
    t = G.new_thread()
    nodes = []
    s = G.run(G.initial_state(text, **kw), t, on_update=lambda n, u: nodes.append(n), app=app)
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
        {"subject": "Complaint", "body": "Order KT12345678 [ORDER DATE] [C1]: please refund within 30 days.",
         "remedy": "refund", "reply_deadline_days": 30, "summary": "s"}])

    def fake_json_call(system, prompt, schema, **_):
        return next(drafts) if "draft firm" in system else None
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


@pytest.mark.parametrize("text,sector,lang", [
    ("Flipkart refused refund for damaged phone", "ecommerce", "en"),
    ("Flipkart ne damaged phone ka refund reject kar diya, order 15 Sep ko kiya tha", "ecommerce", "hinglish"),
    ("Jio charged me for a plan I didn't activate", "telecom", "en"),
    ("मेरे SBI खाते से 5000 रुपये काट लिए गए", "bank", "hi"),
    ("Star Health rejected my claim", "insurance", "en"),
    ("Loan app recovery agents are harassing my contacts", "loan_app", "en"),
    ("IndiGo cancelled my flight and has not refunded me", "other", "en"),
])
def test_intake_rules(text, sector, lang):
    assert rule_intake(text, None)["sector"] == sector and detect_language(text) == lang


def test_extraction_blocks_values_not_on_page(monkeypatch):
    page = {"final_url": "https://x.example/g", "text": "Grievance Officer Mr. Ravi Kumar email ravi@x.example"}
    monkeypatch.setattr(grievance_mod.llm, "json_call", lambda *a, **k: {"contacts": [
        {"role": "grievance_officer", "name": "Ravi Kumar", "email": "ravi@x.example", "phone": "9999988888"},
        {"role": "nodal_officer", "name": "Invented Person", "email": "nodal@x.example", "phone": ""}]})
    out, blocked = grievance_mod.llm_contacts("X", page)
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


def test_llm_calls_gemini_interactions(monkeypatch, tmp_path):
    calls = []

    class FakeInteractions:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:  # first attempt fails -> exercises the retry
                raise RuntimeError("503 UNAVAILABLE")
            return types.SimpleNamespace(status="completed", output_text='{"sector": "telecom"}')
    monkeypatch.setattr(llm, "client", lambda: types.SimpleNamespace(interactions=FakeInteractions()))
    monkeypatch.setattr(llm, "LLM_PROVIDER", "")
    monkeypatch.setattr(llm, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm, "LLM_CACHE", tmp_path)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    schema = {"type": "object", "properties": {"sector": {"type": "string"}}, "required": ["sector"]}
    assert llm.json_call("sys", "Jio charged me", schema) == {"sector": "telecom"} and len(calls) == 2
    kw = calls[-1]
    assert kw["model"] == llm.GEMINI_MODEL and kw["input"] == "Jio charged me" and kw["system_instruction"] == "sys"
    assert kw["response_format"] == {"type": "text", "mime_type": "application/json", "schema": schema}
    assert kw["generation_config"] == {"temperature": 0.1, "thinking_level": "low"}
    assert llm.label().startswith("Gemini")


def test_gemini_schema_inlines_pydantic_refs():
    from saathi.state import IntakeOut, schema
    g = llm.gemini_schema(schema(IntakeOut))
    assert "$defs" not in json.dumps(g) and "$ref" not in json.dumps(g)
    assert g["properties"]["facts"]["properties"]["order_id"]["type"] == "string"
