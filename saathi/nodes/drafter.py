"""drafter: the complaint email to the verified grievance contact, the escalation ladder (from playbooks.py)
and a plain-language summary in the user's language. Missing facts become [PLACEHOLDERS] - never invented.
On a critic loop it receives the critic's notes and must fix them."""
from __future__ import annotations

from saathi import llm
from saathi.nodes.common import node
from saathi.playbooks import SECTORS
from saathi.state import DraftOut, schema

DRAFT_SYSTEM = (
    "You draft firm, polite consumer complaints for Indian consumers. Use ONLY the facts given. For every missing "
    "fact write the exact placeholder supplied (e.g. [ORDER ID]) - never invent order IDs, dates, amounts, names, "
    "emails or phone numbers. Legal grounds: cite ONLY the supplied clauses, by their tag [C1], [C2]…, and only "
    "where the clause text supports the point; never write section/rule numbers yourself and never cite any other "
    "law. Structure: facts; what went wrong; the rule(s) engaged; the specific remedy; a reply deadline; that you "
    "will escalate to the named next forum if unresolved. Under 300 words, no markdown, no greeting or sign-off "
    "(they are added later). The summary is for the consumer, in their language (en = English, hi = Hindi in "
    "Devanagari, hinglish = Hinglish)."
)


def escalation_for(state: dict) -> list[dict]:
    steps = [dict(s) for s in SECTORS[state["sector"]]["escalation"]]
    rec = state.get("recommended")
    if rec:
        steps[0]["channel"] = f"{steps[0]['channel']} — {rec['value']}"
    reg = state.get("registry") or {}
    if state["sector"] == "loan_app" and reg.get("status") in ("not_listed", "lookalike"):
        steps = [steps[2]] + steps[:2]  # unlisted / lookalike app: cyber-crime reporting comes first
        steps[0]["when_to_use"] = "Now: the app is not in RBI's Digital Lending App directory (or imitates a listed " \
                                  "app), so report it rather than writing to it. " + steps[0]["when_to_use"]
    return steps


def template_draft(state: dict) -> dict:
    """Deterministic letter used when no LLM is configured - written to pass the critic's checks."""
    f = state.get("facts") or {}
    ph = {m["fact"]: m["placeholder"] for m in state.get("missing_facts") or []}
    clauses = state.get("clauses") or []
    nxt = escalation_for(state)[1]["name"] if len(escalation_for(state)) > 1 else "the appropriate forum"
    ref = f.get("order_id") or f.get("reference_id") or ph.get("order_id") or ph.get("reference_id") or "[REFERENCE]"
    amount = f"₹{f['amount']}" if f.get("amount") else ph.get("amount", "")
    date = f.get("date") or ph.get("date", "")
    lines = [f"I am writing to formally complain about {f.get('product_or_service') or 'your product/service'} "
             f"(reference {ref}" + (f", dated {date}" if date else "") + (f", amount {amount}" if amount else "") + ")."]
    lines.append(f"What happened: {state['complaint'].strip()}")
    if clauses:
        lines.append("This is contrary to the consumer-protection rules that apply to you, including: "
                     + "; ".join(f"{c['title']} [{c['tag']}]" for c in clauses[:3]) + ".")
    lines.append(f"I request: {f.get('what_user_wants') or '[REMEDY - e.g. full refund / replacement / reversal]'}.")
    lines.append(f"Please acknowledge this complaint and resolve it within 30 days. If it is not resolved, I will "
                 f"escalate it to {nxt}.")
    subject = f"Complaint: {(f.get('issue') or state['complaint'])[:70]} — {ref}"
    lang = state.get("language", "en")
    step1 = escalation_for(state)[0]["name"]
    summary = (f"आपकी शिकायत {state.get('company') or 'कंपनी'} के ख़िलाफ़ है। पहले {step1} को यह पत्र भेजें; "
               f"30 दिन में जवाब न मिले तो {nxt} पर जाएँ।" if lang in ("hi", "hinglish") else
               f"Your complaint is against {state.get('company') or 'the company'}. Send this letter to the "
               f"{step1} first; if there's no fix in 30 days, go to {nxt}.")
    return {"subject": subject, "body": "\n\n".join(lines), "remedy": f.get("what_user_wants") or "",
            "reply_deadline_days": 30, "summary": summary, "method": "template"}


@node("✍️ Drafter")
def drafter(state: dict) -> dict:
    attempt = state.get("draft_attempts", 0) + 1
    clauses = state.get("clauses") or []
    rec = state.get("recommended")
    esc = escalation_for(state)
    facts = state.get("facts") or {}
    placeholders = "\n".join(f"- {m['fact']}: write {m['placeholder']}" for m in state.get("missing_facts") or [])
    notes = state.get("critic_notes") or []
    prompt = (f"LANGUAGE OF THE CONSUMER: {state.get('language', 'en')}\n"
              f"COMPLAINT (consumer's words):\n{state['complaint']}\n\n"
              f"FACTS: {facts}\nCOMPANY: {state.get('company')}\n"
              f"MISSING FACTS - use these placeholders:\n{placeholders or '- none'}\n\n"
              f"ADDRESSED TO: {(rec or {}).get('name') or esc[0]['name']}"
              + (f" <{rec['value']}>" if rec else "") + "\n"
              f"NEXT FORUM IF UNRESOLVED: {esc[1]['name']} — {esc[1]['when_to_use']}\n\n"
              "CLAUSES YOU MAY CITE:\n"
              + "\n\n".join(f"[{c['tag']}] {c['citation']} — {c['title']}\n{c['text'][:1200]}" for c in clauses)
              + (("\n\nA REVIEWER FOUND THESE PROBLEMS IN YOUR PREVIOUS DRAFT - FIX ALL OF THEM:\n- "
                  + "\n- ".join(notes) + f"\n\nPREVIOUS DRAFT:\n{state.get('draft', '')}") if notes else ""))
    out = llm.json_call(DRAFT_SYSTEM, prompt, schema(DraftOut), effort="medium")
    if out:
        try:
            out = DraftOut.model_validate(out).model_dump() | {"method": llm.label()}
        except Exception:
            out = None
    out = out or template_draft(state)
    return {"draft": out["body"], "subject": out["subject"], "summary": out["summary"], "escalation": esc,
            "draft_attempts": attempt, "draft_method": out["method"],
            "trace": [f"✍️ Drafter #{attempt} ({out['method']})" + (f": revising for {len(notes)} critic note(s)"
                                                                   if notes and attempt > 1 else "")]}
