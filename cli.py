"""Shikayat Saathi CLI.

  python cli.py "Flipkart ne damaged phone ka refund reject kar diya, order 15 Sep ko kiya tha" --city Pune
  python cli.py "…" --yes                 # approve the human-review step without prompting
  python cli.py --resume <thread_id>      # resume a paused complaint from data/checkpoints.db
  python cli.py index                     # index the legal corpus into Qdrant
  python cli.py eval [--offline]          # evaluation (see eval/run_eval.py)
"""
from __future__ import annotations

import argparse
import json
import sys

from saathi import llm
from saathi.deadlines import step_dates
from saathi.tools.serp import serp


def print_update(node: str, update: dict) -> None:
    for line in update.get("trace", []) if isinstance(update, dict) else []:
        print(line, flush=True)


def print_safety(safety: dict) -> None:
    if (safety or {}).get("level") == "emergency":
        print("\n" + "!" * 80 + "\n🆘 IF ANYONE IS IN DANGER RIGHT NOW, CALL FOR HELP FIRST")
        for m in safety.get("messages") or []:
            print("   " + m)
        for h in safety.get("helplines") or []:
            print("   ☎ " + h)
        print("!" * 80)


def ask_details(payload: dict, interactive: bool) -> dict:
    """Stage 1: every missing fact and question at once, before the letter is written."""
    print_safety(payload.get("safety") or {})
    print("\n" + "─" * 80 + "\n📝 A FEW DETAILS FIRST — the letter is written with your answers\n" + "─" * 80)
    elig = payload.get("eligibility") or {}
    if elig.get("message"):
        print(f"{'⚠️' if elig['status'] == 'may_not_qualify' else 'ℹ️'}  Eligibility: {elig['message']}")
    for c in payload.get("eligibility_checks") or []:
        if c["status"] != "unknown":
            print(f"   {'✅' if c['status'] == 'met' else '⚠️'} {c['condition']}")
    fills, answers = {}, {}
    for m in payload.get("missing_facts") or []:
        v = input(f"   {m['fact'].replace('_', ' ')} {m['placeholder']} (Enter to skip): ").strip() if interactive \
            else ""
        if v:
            fills[m["placeholder"]] = v
        elif not interactive:
            print(f"   · {m['placeholder']}")
    for q in payload.get("questions") or []:
        v = input(f"   {q['question']} (Enter to skip): ").strip() if interactive else ""
        if v:
            answers[q["key"]] = v
        elif not interactive:
            print(f"   ? {q['question']}")
    if not interactive:
        print("(skipped - run without --yes in a terminal to answer)")
    return {"fills": fills, "case_answers": answers}


def ask_review(payload: dict, auto: bool) -> dict:
    interactive = not auto and sys.stdin.isatty()
    if payload.get("stage") == "details":
        return ask_details(payload, interactive)
    print_safety(payload.get("safety") or {})
    print("\n" + "─" * 80 + f"\n🙋 HUMAN REVIEW — draft subject: {payload.get('subject')}\n" + "─" * 80)
    print(payload.get("draft", ""))
    if (payload.get("deadline") or {}).get("date"):
        dl = payload["deadline"]
        print(f"\n📅 Reply due by {dl['date']} — {dl['basis']}" + (f" ({dl['source']})" if dl.get("source") else ""))
    if payload.get("critic_notes"):
        print("\nCritic notes still open:\n  - " + "\n  - ".join(payload["critic_notes"]))
    if interactive:
        input("\nPress Enter to approve (edit the final letter afterwards if needed) ")
    else:
        print("(auto-approved)")
    return {}


def print_final(s: dict) -> None:
    f = s.get("final") or {}
    rec = s.get("recommended")
    print_safety(s.get("safety") or {})
    print("\n" + "═" * 80)
    print(f"Sector: {s.get('sector')} · Company: {s.get('company')} · Language: {s.get('language')} · "
          f"Official domain: {s.get('company_domain') or 'not resolved'}")
    if s.get("urgent_note"):
        print(f"🚨 {s['urgent_note']}")
    print("\n✅ Verified contacts:")
    for c in s.get("verified_contacts") or []:
        print(f"   {c['value']:<38} {c['role']:<24} {c.get('name', '')[:22]:<22} — {c['reason']}")
    if not s.get("verified_contacts"):
        print("   none")
    if s.get("unverified_contacts"):
        print("⚠️  Unverified — use with caution:")
        for c in s["unverified_contacts"][:8]:
            print(f"   {c['value']:<38} {c['reason']}" + (f" · {c['warnings'][0]}" if c.get("warnings") else ""))
    print(f"\n📧 Send to: {rec['value'] if rec else 'no verified email - use the official complaint form'}")
    for p in (s.get("precedents") or [])[:3]:
        print(f"📰 {p['title']} <{p['url']}>" + (f" — {p['how_resolved']}" if p.get("how_resolved") else ""))
    if s.get("local_forum"):
        lf = s["local_forum"]
        print(f"📍 {lf['name']} — {lf.get('address') or ''} {lf.get('phone') or ''}")
    print("\n" + "─" * 80 + "\n" + f.get("letter", "") + "\n" + "─" * 80)
    print("\n🪜 Escalation:")
    for i, st in enumerate(s.get("escalation") or [], 1):
        print(f"   {i}. {st['name']} ({st['channel']}) — {st['when_to_use']}"
              + (f" {st['url']}" if st.get("url") else "") + (f" · {st['phone']}" if st.get("phone") else "")
              + (f"\n      📅 {step_dates(st)}" if step_dates(st) else ""))
    if s.get("summary"):
        print(f"\n💬 {s['summary']}")
    print(f"\nLive SerpApi calls this complaint: {s.get('live_calls', 0)} · {serp.summary()} · LLM: {llm.label()}")
    print("Information, not legal advice. Nothing was sent.")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "index":
        from saathi.rag.ingest import build_index

        print(build_index())
        return
    if len(sys.argv) > 1 and sys.argv[1] == "eval":
        from eval.run_eval import main as run_eval

        sys.argv = [sys.argv[0]] + sys.argv[2:]
        run_eval()
        return
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("complaint", nargs="?")
    ap.add_argument("--company")
    ap.add_argument("--city")
    ap.add_argument("--name")
    ap.add_argument("--offline", action="store_true", help="cache only - no live SerpApi calls")
    ap.add_argument("--max-credits", type=int, help="live SerpApi calls for this complaint (0 = no cap)")
    ap.add_argument("--yes", action="store_true", help="approve the human-review step without prompting")
    ap.add_argument("--resume", metavar="THREAD_ID", help="resume a paused complaint")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not a.complaint and not a.resume:
        ap.error("give a complaint text or --resume THREAD_ID")

    from saathi import graph as G

    serp.offline = a.offline
    if a.max_credits is not None:
        serp.max_credits = a.max_credits or None
    if a.resume:
        thread = a.resume
        from saathi.nodes.review import review_payload

        state = dict(G.graph().get_state({"configurable": {"thread_id": thread}}).values)
        state = G.resume(thread, ask_review(review_payload(state), a.yes), on_update=print_update)
        while "__interrupt__" in state:  # answers trigger a redraft and another review round
            state = G.resume(thread, ask_review(state["__interrupt__"], a.yes), on_update=print_update)
    else:
        thread = G.new_thread()
        print(f"thread_id: {thread}")
        state = G.run(G.initial_state(a.complaint, company=a.company, city=a.city,
                                      complainant={"name": a.name} if a.name else None, live=not a.offline),
                      thread, on_update=print_update)
        while "__interrupt__" in state:  # answers trigger a redraft and another review round
            state = G.resume(thread, ask_review(state["__interrupt__"], a.yes), on_update=print_update)
    if a.json:
        print(json.dumps({k: v for k, v in state.items() if k != "pages"}, indent=1, ensure_ascii=False,
                         default=str))
    else:
        print_final(state)


if __name__ == "__main__":
    main()
