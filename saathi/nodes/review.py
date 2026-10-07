"""human_review: pause with interrupt() so the user can fill placeholders and edit the draft; finalize: build the
final letter (never sent automatically)."""

import json
from datetime import date

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from config import CASES_DIR
from saathi.nodes.common import node
from saathi.nodes.legal_rag import strip_unsupported


def review_payload(state: dict) -> dict:
    return {"subject": state.get("subject"), "draft": state.get("draft"), "summary": state.get("summary"),
            "missing_facts": state.get("missing_facts") or [], "critic_notes": state.get("critic_notes") or [],
            "recommended": state.get("recommended"),
            "verified_contacts": state.get("verified_contacts") or [],
            "unverified_contacts": state.get("unverified_contacts") or []}


@node("🙋 Human review")
def human_review(state: dict) -> dict:
    """Resume with Command(resume={"draft": <edited body>?, "subject": ..?, "fills": {"[ORDER ID]": "OD1…"},
    "complainant": {"name":…, "phone":…, "email":…, "address":…}}) - or {} to approve as is."""
    edits = interrupt(review_payload(state)) or {}
    filled = [k for k, v in (edits.get("fills") or {}).items() if v]
    return {"user_edits": edits,
            "trace": ["🙋 Human review: approved" + (f", filled {', '.join(filled)}" if filled else "")
                      + (", edited the draft" if edits.get("draft") and edits["draft"] != state.get("draft") else "")]}


@node("📦 Finalize")
def finalize(state: dict, config: RunnableConfig | None = None) -> dict:
    edits = state.get("user_edits") or {}
    body = edits.get("draft") or state.get("draft") or ""
    subject = edits.get("subject") or state.get("subject") or "Consumer complaint"
    for ph, val in (edits.get("fills") or {}).items():
        if val:
            body, subject = body.replace(ph, val), subject.replace(ph, val)
    clauses = state.get("clauses") or []
    body, removed = strip_unsupported(body, clauses)
    by_tag = {c["tag"]: c for c in clauses}
    used = [t for t in by_tag if f"[{t}]" in body]
    rec = state.get("recommended")
    me = {**(state.get("complainant") or {}), **(edits.get("complainant") or {})}
    to_name = (rec or {}).get("name") or (state.get("escalation") or [{}])[0].get("name", "Grievance Officer")
    header = (f"To,\n{to_name}\n{state.get('company') or ''}\n" + (f"Email: {rec['value']}\n" if rec else "")
              + f"\nDate: {date.today().strftime('%d %B %Y')}\n\nSubject: {subject}\n\nDear Sir/Madam,\n\n")
    footer = (f"\n\nYours faithfully,\n{me.get('name') or '[YOUR NAME]'}\n{me.get('phone') or '[PHONE]'}\n"
              f"{me.get('email') or '[EMAIL]'}\n{me.get('address') or '[ADDRESS]'}")
    basis = "\n".join(f"[{t}] {by_tag[t]['citation']} — {by_tag[t]['title']}" for t in used)
    letter = header + body + footer + (f"\n\nLegal basis cited:\n{basis}" if basis else "")
    ladder = "\n".join(f"{i}. **{s['name']}** ({s['channel']}) — {s['when_to_use']}"
                       + (f" {s['url']}" if s.get("url") else "") + (f" · {s['phone']}" if s.get("phone") else "")
                       for i, s in enumerate(state.get("escalation") or [], 1))
    markdown = (f"# {subject}\n\n**To:** {to_name}" + (f" <{rec['value']}>" if rec else " (no verified email - use "
                                                       "the official complaint form)")
                + f"\n\n{state.get('summary') or ''}\n\n```\n{letter}\n```\n\n## If there's no reply\n{ladder}\n\n"
                  "_Information, not legal advice._\n")
    final = {"to": rec["value"] if rec else None, "to_name": to_name, "subject": subject, "letter": letter,
             "markdown": markdown, "summary": state.get("summary"), "escalation": state.get("escalation"),
             "cited": [by_tag[t] for t in used], "removed_citations": removed,
             "unresolved_critic_notes": state.get("critic_notes") or []}
    thread = ((config or {}).get("configurable") or {}).get("thread_id") or "case"
    record = {k: v for k, v in state.items() if k not in ("pages",)} | {"final": final, "thread_id": thread}
    (CASES_DIR / f"{thread}.json").write_text(json.dumps(record, indent=1, ensure_ascii=False, default=str))
    return {"final": final, "trace": [f"📦 Finalize: letter ready for {final['to'] or 'the official complaint form'}"
                                      + (f"; stripped {len(removed)} unsupported citation(s)" if removed else "")
                                      + " — nothing is sent automatically"]}
