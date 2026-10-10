"""Escalation-planner test set: real intake call + real search + real LLM, then the code checks.

For each complaint (mostly sectors with NO seed playbook) it checks that the validated ladder
  * contains a sector-specific authority we expect (any of `expect`),
  * avoids what it must not use (e.g. CPGRAMS for a state body),
  * has a source on every step,
and reports what the validator dropped or changed, whether the honest general route had to be used, and the time.

    python eval/escalation_eval.py [--only N] [--keep-cache]
Writes eval/escalation_report.json and eval/ESCALATION_REPORT.md. Uses live SerpApi credits and Gemini calls.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES = [
    {"id": 1, "complaint": "My electricity bill from JVVNL in Jaipur is Rs 18,400 for one month though my usual bill "
                           "is about Rs 1,200. The sub-division office is not correcting it.",
     "company": "JVVNL", "city": "Jaipur", "expect": ["cgrf", "consumer grievance redressal forum", "ombudsman"],
     "forbid": ["cpgrams", "pgportal"]},
    {"id": 2, "complaint": "Delhi Jal Board has not fixed the sewer overflow in my lane in Rohini for 3 weeks despite "
                           "two complaints.", "company": "Delhi Jal Board", "city": "Delhi",
     "expect": ["delhi jal board", "djb", "pgms", "public grievance"], "forbid": ["cpgrams", "pgportal"]},
    {"id": 3, "complaint": "Indraprastha Gas Limited (IGL) has billed me Rs 6,000 for piped gas without any meter "
                           "reading for 4 months in Noida.", "company": "IGL", "city": "Noida",
     "expect": ["pngrb", "petroleum and natural gas regulatory board", "mopng", "e-seva"], "forbid": []},
    {"id": 4, "complaint": "Delhi University has not declared my semester result for 5 months and the examination "
                           "branch does not reply to emails.", "company": "Delhi University", "city": "Delhi",
     "expect": ["ombudsperson", "ugc", "grievance redressal committee", "student grievance"], "forbid": []},
    {"id": 5, "complaint": "IndiGo cancelled my flight 6E-2143 on 2 Oct 2026 and has not refunded Rs 7,400 after "
                           "30 days.", "company": "IndiGo", "city": "Mumbai", "expect": ["airsewa", "dgca"],
     "forbid": []},
    {"id": 6, "complaint": "BBMP has not repaired the broken street light and the potholes on my road in "
                           "Bengaluru for two months.", "company": "BBMP", "city": "Bengaluru",
     "expect": ["bbmp", "sahaaya", "karnataka"], "forbid": ["cpgrams", "pgportal"]},
    {"id": 7, "complaint": "Flipkart refused a refund for my damaged phone, order OD123456789, Rs 18,999.",
     "company": "Flipkart", "city": "Pune", "expect": ["consumer helpline", "consumer commission", "grievance"],
     "forbid": [], "control": True},
]


# Sectors that HAVE a seed playbook: does the planner match or beat the hand-checked route?
SEEDED = [
    {"id": 11, "complaint": "HDFC Bank debited Rs 2,500 from my account through a UPI transaction I did not make. I "
                            "complained 35 days ago and got no reply.", "company": "HDFC Bank", "city": "Mumbai",
     "expect": ["ombudsman", "cms.rbi", "reserve bank"], "forbid": []},
    {"id": 12, "complaint": "Star Health rejected my hospitalisation claim of Rs 85,000 saying it was a pre-existing "
                            "disease, though I declared everything.", "company": "Star Health", "city": "Chennai",
     "expect": ["insurance ombudsman", "bima bharosa", "cioins"], "forbid": []},
    {"id": 13, "complaint": "Jio charged me Rs 299 for a plan I never activated and customer care says nothing can "
                            "be done.", "company": "Jio", "city": "Pune",
     "expect": ["appellate", "nodal"], "forbid": []},
]


def ladder_text(steps: list[dict]) -> str:
    return " ".join(f"{s['name']} {s['channel']} {s.get('url') or ''}" for s in steps).lower()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=int, help="run one case id")
    ap.add_argument("--keep-cache", action="store_true", help="reuse cached ladders (default: plan from scratch)")
    ap.add_argument("--seeded", action="store_true", help="run the sectors that have a seed playbook, and compare")
    a = ap.parse_args()

    from saathi import graph as G
    from saathi import llm
    from saathi.nodes import escalation
    from saathi.nodes.intake import intake_planner
    from saathi.tools.serp import serp
    if not llm.enabled():
        raise SystemExit("Needs the AI service: set GEMINI_API_KEY.")
    if not a.keep_cache:
        escalation.LADDER_DIR = Path(tempfile.mkdtemp(prefix="ladders-"))

    rows = []
    from saathi.playbooks import SECTORS
    cases = SEEDED if a.seeded else CASES
    for case in [c for c in cases if not a.only or c["id"] == a.only]:
        t0 = time.perf_counter()
        calls0 = serp.live_calls
        st = G.initial_state(case["complaint"], company=case["company"], city=case["city"])
        st |= intake_planner(st)
        t_intake = time.perf_counter() - t0
        res = escalation.plan_escalation(st)
        dt = time.perf_counter() - t0
        text = ladder_text(res["steps"])
        row = {"id": case["id"], "company": case["company"], "route": st["route"],
               "expect_hit": any(k in text for k in case["expect"]),
               "forbid_hit": [k for k in case["forbid"] if k in text],
               "all_sourced": all(s.get("source") for s in res["steps"]),
               "fallback": res["fallback"], "steps": res["steps"], "notes": res.get("notes", []),
               "note": res.get("note", ""), "official_pages": res.get("official_pages"),
               "clauses": res.get("clauses", []), "seconds": round(dt, 1), "intake_s": round(t_intake, 1),
               "serp_calls": serp.live_calls - calls0, "control": case.get("control", False)}
        row["pass"] = row["expect_hit"] and not row["forbid_hit"] and row["all_sourced"]
        rows.append(row)
        print(f"\n=== #{case['id']} {case['company']} · route_key={st['route'].get('route_key')} "
              f"state={st['route'].get('state')} type={st['route'].get('authority_type')} · {dt:.1f}s "
              f"({row['serp_calls']} searches) · {'PASS' if row['pass'] else 'FAIL'}"
              + (" · GENERAL ROUTE" if res["fallback"] else ""))
        for i, s in enumerate(res["steps"], 1):
            print(f"  {i}. [{s['level']}] {s['name']} — {s['channel']}" + (f" {s['url']}" if s.get("url") else "")
                  + (f" · {s['timeline']}" if s.get("timeline") else "") + f"\n       source: {s['source']}")
        for n in res.get("notes", []):
            print(f"  ✗ {n}")
        if a.seeded:
            seed = SECTORS.get(st.get("sector") or "other", {}).get("escalation") or []
            print(f"  seed ladder ({st.get('sector')}): " + " → ".join(x["name"] for x in seed))
            row["seed"] = [x["name"] for x in seed]
            row["would_fall_back_to_seed"] = res["fallback"]
    n = len(rows)
    summary = {"cases": n, "passed": sum(r["pass"] for r in rows),
               "expected_authority_found": sum(r["expect_hit"] for r in rows),
               "forbidden_used": sum(bool(r["forbid_hit"]) for r in rows),
               "all_steps_sourced": sum(r["all_sourced"] for r in rows),
               "general_route_used": sum(r["fallback"] for r in rows),
               "avg_seconds": round(sum(r["seconds"] for r in rows) / n, 1) if n else None,
               "avg_searches": round(sum(r["serp_calls"] for r in rows) / n, 1) if n else None}
    print("\nSUMMARY", json.dumps(summary))
    if a.seeded:
        (ROOT / "eval" / "escalation_seeded_report.json").write_text(json.dumps({"summary": summary, "rows": rows},
                                                                               indent=1, ensure_ascii=False),
                                                                    encoding="utf-8")
        return
    (ROOT / "eval" / "escalation_report.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1,
                                                                     ensure_ascii=False), encoding="utf-8")
    md = ["# Escalation planner - test set", "",
          f"{summary['passed']}/{n} pass · expected authority found {summary['expected_authority_found']}/{n} · "
          f"forbidden route used {summary['forbidden_used']} · every step sourced {summary['all_steps_sourced']}/{n} "
          f"· general route {summary['general_route_used']} · avg {summary['avg_seconds']} s, "
          f"{summary['avg_searches']} searches", "",
          "| # | Against | State | Ladder (validated) | Dropped / changed | Result |", "|---|---|---|---|---|---|"]
    for r in rows:
        ladder = " → ".join(s["name"] for s in r["steps"])
        md.append(f"| {r['id']} | {r['company']} | {r['route'].get('state') or '-'} | {ladder} | "
                  f"{len(r['notes'])} | {'✔' if r['pass'] else '✘'}{' (general route)' if r['fallback'] else ''} |")
    (ROOT / "eval" / "ESCALATION_REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
