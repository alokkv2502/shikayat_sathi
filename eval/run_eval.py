"""Evaluation of the Shikayat Saathi graph.

A. Contacts (eval/companies.csv, hand-labelled rows): run the grievance + verifier subgraph (with its retry loop)
   per company and report: contact found rate, exact-email accuracy vs ground truth, verifier precision (share of
   shown "verified" emails that are correct = the gold email or on the company's official domain), unverified
   contacts caught, average live SerpApi calls and latency. Costs ~4-6 SerpApi calls per company (cached after).
B. Complaints (eval/complaints.jsonl): sector + language classification accuracy, and the full graph run with
   search off - every draft must pass the critic with zero uncited claims in the final letter. Letter quality:
   specificity (how many of the complainant's own facts the letter uses), legal frame (a government scheme cites no
   consumer law; a consumer case cites only its sector's law) and template overlap (the largest share of a letter's
   5-word phrases found in any other letter - near-identical letters score high).
C. Decoys (eval/decoys.json): synthetic scam-pattern contacts the verifier must reject (offline).

    python eval/run_eval.py [--offline] [--max-credits N] [--all] [--skip-contacts]
Writes eval/report.json and eval/REPORT.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from saathi import graph as G  # noqa: E402
from saathi import llm  # noqa: E402
from saathi.nodes import verifier as V  # noqa: E402
from saathi.nodes.critic import case_fact_values, facts_used  # noqa: E402
from saathi.playbooks import SECTORS  # noqa: E402
from saathi.tools.domains import registrable  # noqa: E402
from saathi.tools.serp import serp  # noqa: E402

EVAL_DIR = ROOT / "eval"


def pct(n, d):
    return round(100 * n / d, 1) if d else None


# --------------------------------------------------------------------------- A. contacts

def contacts_eval(rows: list[dict], *, live: bool) -> dict:
    sub = G.build_contacts()
    out = []
    for g in rows:
        t0 = time.time()
        s = sub.invoke({**G.initial_state("", live=live), "company": g["company"], "sector": g["sector"]})
        latency = time.time() - t0
        if not live and not s.get("company_domain") and not s.get("pages") and not s.get("registry"):
            out.append({"company": g["company"], "skipped": True})  # nothing cached for this company
            continue
        official = {g["official_domain"]} | set(s.get("official_domains") or [])
        gold = (g.get("grievance_email") or "").strip().lower()
        rec = (s.get("recommended") or {}).get("value")
        ver = [c for c in s.get("verified_contacts") or [] if c["type"] == "email"]
        unv = [c for c in s.get("unverified_contacts") or [] if c["type"] == "email"]

        def correct(e: str) -> bool:
            dom = e.split("@")[-1]
            return e == gold or any(dom == d or dom.endswith("." + d) for d in official)
        out.append({"company": g["company"], "sector": g["sector"], "labelled": bool(gold),
                    "domain_ok": g["official_domain"] in ({registrable(s.get("company_domain") or "")}
                                                         | set(s.get("official_domains") or [])),
                    "found": bool(rec), "recommended": rec, "exact": bool(gold) and rec == gold,
                    "verified_emails": len(ver), "verified_correct": sum(correct(c["value"]) for c in ver),
                    "unverified_caught": sum(not correct(c["value"]) for c in unv),
                    "retries": max(0, (s.get("grievance_attempts") or 1) - 1),
                    "live_calls": s.get("live_calls") or 0, "latency_s": round(latency, 1),
                    "blocked_hallucinations": s.get("blocked_hallucinations") or 0})
    sc = [r for r in out if not r.get("skipped")]
    lab = [r for r in sc if r["labelled"]]
    ve = sum(r["verified_emails"] for r in sc)
    return {"n": len(sc), "n_labelled": len(lab), "skipped": len(out) - len(sc),
            "domain_accuracy": pct(sum(r["domain_ok"] for r in sc), len(sc)),
            "contact_found_rate": pct(sum(r["found"] for r in sc), len(sc)),
            "exact_email_accuracy": pct(sum(r["exact"] for r in lab), len(lab)),
            "verifier_precision": pct(sum(r["verified_correct"] for r in sc), ve),
            "unverified_caught": sum(r["unverified_caught"] for r in sc),
            "retries": sum(r["retries"] for r in sc),
            "avg_live_calls": round(sum(r["live_calls"] for r in sc) / len(sc), 1) if sc else None,
            "avg_latency_s": round(sum(r["latency_s"] for r in sc) / len(sc), 1) if sc else None,
            "blocked_hallucinations": sum(r["blocked_hallucinations"] for r in sc), "rows": out}


# --------------------------------------------------------------------------- B. complaints

CONSUMER_FRAME = re.compile(r"consumer protection|deficiency in service|national consumer helpline|consumer commission|"
                            r"consumer court|\bNCH\b", re.I)


def letter_body(letter: str) -> str:
    """The body the drafter wrote, without the shared header / footer / citation list."""
    body = letter.split("Dear Sir/Madam,", 1)[-1]
    return body.split("Yours faithfully,", 1)[0].strip()


def frame_ok(s: dict, body: str) -> bool:
    cited = (s.get("final") or {}).get("cited") or []
    if s.get("sector") == "govt_scheme":
        return not cited and not CONSUMER_FRAME.search(body)
    allowed = set(SECTORS[s.get("sector") or "other"]["law_docs"])
    return bool(cited) and all(c.get("doc_id") in allowed for c in cited)


def shingles(text: str, n: int = 5) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.casefold())
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def max_overlap(bodies: list[str]) -> list[float]:
    """For each letter, the largest share of its 5-word phrases that also appear in one other letter."""
    sh = [shingles(b) for b in bodies]
    return [round(100 * max((len(a & b) / len(a) for j, b in enumerate(sh) if j != i and a), default=0), 1)
            for i, a in enumerate(sh)]


def complaints_eval() -> dict:
    items = [json.loads(x) for x in (EVAL_DIR / "complaints.jsonl").read_text(encoding="utf-8").splitlines()
             if x.strip()]
    rows = []
    was_offline = serp.offline
    serp.offline = True  # drafting check runs with search off: it tests intake, RAG, drafter and critic
    try:
        for it in items:
            t = f"eval-{it['id']}-{int(time.time())}"
            s = G.run(G.initial_state(it["complaint"], live=False), t)
            while "__interrupt__" in s:  # details form (submitted empty), then the letter review
                s = G.resume(t, {})
            final = s.get("final") or {}
            body = letter_body(final.get("letter") or "")
            values = case_fact_values(s)
            names = [n for n in [*(s.get("case_entities") or []), (s.get("case_brief") or {}).get("subject") or ""]
                     if n]
            rows.append({"facts_available": len(values), "facts_used": len(facts_used(values, body)),
                         "names_scheme": any(n.casefold() in body.casefold() for n in names) if names else None,
                         "frame_ok": frame_ok(s, body), "_body": body,"id": it["id"], "language": it["language"], "expected_sector": it["sector"],
                         "sector": s.get("sector"), "sector_ok": s.get("sector") == it["sector"],
                         "language_ok": s.get("language") == it["language"],
                         "critic_passed": not s.get("critic_notes"), "draft_attempts": s.get("draft_attempts"),
                         "uncited_in_final": len(final.get("removed_citations") or []),
                         "clauses": len(s.get("clauses") or [])})
    finally:
        serp.offline = was_offline
    for r, ov in zip(rows, max_overlap([r.pop("_body") for r in rows])):
        r["max_overlap"] = ov
    n = len(rows)
    return {"n": n, "sector_accuracy": pct(sum(r["sector_ok"] for r in rows), n),
            "language_accuracy": pct(sum(r["language_ok"] for r in rows), n),
            "critic_pass_rate": pct(sum(r["critic_passed"] for r in rows), n),
            "zero_uncited_rate": pct(sum(r["uncited_in_final"] == 0 for r in rows), n),
            "avg_facts_used": round(sum(r["facts_used"] for r in rows) / n, 1) if n else None,
            "avg_facts_available": round(sum(r["facts_available"] for r in rows) / n, 1) if n else None,
            "frame_ok_rate": pct(sum(r["frame_ok"] for r in rows), n),
            "avg_max_overlap": round(sum(r["max_overlap"] for r in rows) / n, 1) if n else None, "rows": rows}


# --------------------------------------------------------------------------- C. decoys

def decoy_eval() -> dict:
    cases = json.loads((EVAL_DIR / "decoys.json").read_text(encoding="utf-8"))["cases"]
    rows, tp, fn, tn, fp = [], 0, 0, 0, 0
    for c in cases:
        obs = [{"type": o["type"], "value": o["value"], "role": "grievance_officer", "name": "", "timeline": "",
                "evidence": "", "source_url": o["source_url"],
                "source_domain": o.get("source_domain") or registrable(o["source_url"]), "via": o["via"],
                "page_title": ""} for o in c["observations"]]
        out = {(x["type"], x["value"]): x for x in V.verify(c["company"], obs, c["official_domains"])}
        expected = {}
        for o in c["observations"]:  # a value is expected verified if any observation says so
            k = (o["type"], o["value"])
            expected[k] = "verified" if o["expect"] == "verified" or expected.get(k) == "verified" else "unverified"
        for k, exp in expected.items():
            got = out[k]["status"] if k in out else "dropped"
            pattern = next((o.get("pattern") for o in c["observations"] if (o["type"], o["value"]) == k), None)
            if exp == "unverified":
                tn += got != "verified"
                fp += got == "verified"
            else:
                tp += got == "verified"
                fn += got != "verified"
            rows.append({"company": c["company"], "value": k[1], "expected": exp, "got": got, "pattern": pattern,
                         "ok": (got == "verified") == (exp == "verified")})
    return {"decoys": tn + fp, "decoys_caught": tn, "decoy_catch_rate": pct(tn, tn + fp), "genuine": tp + fn,
            "genuine_kept": tp, "false_rejection_rate": pct(fn, tp + fn), "rows": rows}


# --------------------------------------------------------------------------- report

def markdown(rep: dict) -> str:
    a, b, c = rep.get("contacts"), rep["complaints"], rep["decoys"]
    L = [f"# Shikayat Saathi — evaluation ({rep['run_at']})", "",
         "## A. Grievance contacts (grievance + verifier subgraph)", ""]
    if a and a["n"]:
        L += ["| Metric | Value |", "|---|---|",
              f"| Companies run (hand-labelled) | {a['n']} ({a['n_labelled']}) |",
              f"| Official domain correct | {a['domain_accuracy']}% |",
              f"| Contact found rate (verified email recommended) | {a['contact_found_rate']}% |",
              f"| Exact-email accuracy vs ground truth | {a['exact_email_accuracy']}% |",
              f"| Verifier precision (shown verified emails that are correct) | {a['verifier_precision']}% |",
              f"| Unverified wrong contacts caught | {a['unverified_caught']} |",
              f"| Verifier retries triggered | {a['retries']} |",
              f"| Avg live SerpApi calls / company | {a['avg_live_calls']} |",
              f"| Avg latency / company | {a['avg_latency_s']} s |",
              f"| LLM values blocked (not on page) | {a['blocked_hallucinations']} |", "",
              "| Company | Recommended | Exact | Verified ok/shown | Unverified caught | Calls |",
              "|---|---|---|---|---|---|"]
        for r in a["rows"]:
            if not r.get("skipped"):
                exact = "✔" if r["exact"] else ("✘" if r["labelled"] else "—")
                L.append(f"| {r['company']} | {r['recommended'] or '-'} | {exact} | {r['verified_correct']}/"
                         f"{r['verified_emails']} | {r['unverified_caught']} | {r['live_calls']} |")
    else:
        L.append("_Not run (no SerpApi key / cache, or --skip-contacts)._")
    L += ["", "## B. Complaints (intake → RAG → drafter → critic, search off)", "",
          "| Metric | Value |", "|---|---|", f"| Complaints | {b['n']} (incl. Hindi & Hinglish) |",
          f"| Sector accuracy | {b['sector_accuracy']}% |", f"| Language detection | {b['language_accuracy']}% |",
          f"| Drafts passing the critic | {b['critic_pass_rate']}% |",
          f"| Final letters with zero uncited claims | {b['zero_uncited_rate']}% |",
          f"| Specificity: case-specific facts used per letter | {b['avg_facts_used']} of {b['avg_facts_available']} "
          f"available |",
          f"| Correct legal frame (scheme → no consumer law; consumer → own sector's law) | {b['frame_ok_rate']}% |",
          f"| Template overlap: avg max share of 5-word phrases shared with another letter (lower is better) | "
          f"{b['avg_max_overlap']}% |", "",
          "| # | Lang | Expected | Got | Critic | Drafts | Facts used | Frame | Overlap |",
          "|---|---|---|---|---|---|---|---|---|"]
    L += [f"| {r['id']} | {r['language']} | {r['expected_sector']} | {r['sector']} {'✔' if r['sector_ok'] else '✘'} | "
          f"{'pass' if r['critic_passed'] else 'issues'} | {r['draft_attempts']} | {r['facts_used']}/"
          f"{r['facts_available']} | {'✔' if r['frame_ok'] else '✘'} | {r['max_overlap']}% |" for r in b["rows"]]
    L += ["", "## C. Decoy test (synthetic scam patterns)", "",
          f"Decoys rejected **{c['decoys_caught']}/{c['decoys']}** · genuine kept **{c['genuine_kept']}/"
          f"{c['genuine']}**", "", "| Company | Contact | Pattern | Expected | Got |", "|---|---|---|---|---|"]
    L += [f"| {r['company']} | `{r['value']}` | {r['pattern'] or 'genuine'} | {r['expected']} | "
          f"{'✔' if r['ok'] else '✘'} {r['got']} |" for r in c["rows"]]
    return "\n".join(L) + "\n"


def main() -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="cache only for the contacts part")
    ap.add_argument("--max-credits", type=int, help="live SerpApi calls for the whole contacts part (0 = no cap)")
    ap.add_argument("--all", action="store_true", help="also run unlabelled companies")
    ap.add_argument("--skip-contacts", action="store_true")
    a = ap.parse_args()
    serp.offline = a.offline
    with (EVAL_DIR / "companies.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not a.all:
        rows = [r for r in rows if r.get("grievance_email")]
    rep = {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if not a.skip_contacts:
        serp.new_budget(a.max_credits or None)
        if a.max_credits == 0:
            serp.max_credits = None
        rep["contacts"] = contacts_eval(rows, live=not a.offline and bool(serp.api_key))
    if not llm.enabled():  # the complaint part measures the AI pipeline; there is no rule-based stand-in to score
        raise SystemExit("The complaints evaluation needs the AI service: set GEMINI_API_KEY (and LLM_PROVIDER).")
    rep["complaints"] = complaints_eval()
    rep["decoys"] = decoy_eval()
    (EVAL_DIR / "report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    md = markdown(rep)
    (EVAL_DIR / "REPORT.md").write_text(md, encoding="utf-8")
    print(md)
    print(serp.summary())
    return rep


if __name__ == "__main__":
    main()
