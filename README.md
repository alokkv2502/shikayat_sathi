# Shikayat Saathi (शिकायत साथी)

**One front door for every grievance in India — with depth in consumer, banking, government-scheme, police and
court matters.**

People lose money and time not because they lack rights, but because they can't find the path to use them. Shikayat
Saathi takes a complaint written in English, Hindi or Hinglish ("Flipkart ne damaged phone ka refund reject kar
diya") and returns:

- a **verified** grievance contact for the organisation, with the page it was found on;
- the **escalation ladder**, every step traceable to an official source, with **deadlines computed in code**;
- the **exact legal clauses** that back the complaint (retrieved from the indexed law, never invented);
- a ready-to-send letter in **one language** (English or Hindi), checked before you see it.

**ChatGPT is smart; Shikayat Saathi is accountable.** The LLM understands the complaint and proposes; code checks
every contact, route step, citation, amount, date and deadline before it reaches a letter.

It is a **LangGraph** multi-agent system: workers run in parallel through `Send`, three loops correct their own
output, the user answers one form and reviews the letter through `interrupt()`, and every complaint is checkpointed
in SQLite. Search uses SerpApi (Google, Forums, News, Maps); the law is retrieved from a local Qdrant index.

_Information, not legal advice. Nothing is ever sent automatically._

## Quick start and Installation Steps

```bash
cd shikayat_sathi
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # GEMINI_API_KEY and SERPAPI_API_KEY (see Configuration)
docker compose up -d                                   # local Qdrant (lets the app and the CLI share the index)
python cli.py index                                    # index the legal corpus (first time, and after adding PDFs)
streamlit run app.py
```

From the CLI:

```bash
python cli.py "Flipkart ne damaged phone ka refund reject kar diya, order 15 Sep ko kiya tha" --city Pune
python cli.py "…" --letter-language hi    # the whole letter in Hindi (default: Hindi for Hindi complaints)
python cli.py "…" --yes                   # skip the questions and approve the letter without prompting
python cli.py --resume <thread_id>        # resume a paused complaint from data/checkpoints.db
python eval/run_eval.py                   # contacts / complaints / decoys → eval/REPORT.md (needs the AI)
python eval/escalation_eval.py            # escalation planner test set → eval/ESCALATION_REPORT.md
python eval/latency.py "<complaint>" --company X --city Y --warm   # per-node latency profile
pytest -q tests/                          # 55 tests, offline (a deterministic stand-in for the LLM)
```

## What happens to a complaint

```mermaid
flowchart TD
  S([START]) --> SR[🛟 safety_router<br/>rules, instant]
  SR --> I[🧭 intake_planner<br/>ONE LLM call]
  I -- "very unclear" --> Q[❓ clarify<br/>one question] --> I
  I --> CR[🔎 case_research<br/>named schemes / products]
  CR --> FR[🗺️ frame]
  FR -- Send --> G[🔎 grievance]
  FR -- Send --> P[📰 precedents]
  FR -- Send --> L[📍 local_forum]
  FR -- Send --> GC[🧩 gap_check]
  FR -- "Send (tier A)" --> EP[🪜 escalation_planner]
  FR -- "Send (loan apps)" --> R[🏛️ registry]
  G & P & L & GC & EP & R --> V[🛡️ verifier]
  V -- "no verified contact<br/>& attempts < 2" --> G
  V --> RAG[📚 legal_rag]
  RAG --> AD[📝 ask_details<br/>interrupt: ONE form]
  AD -- answers --> RA[🔁 reassess] --> D
  AD --> D[✍️ drafter]
  D --> C[🧐 critic]
  C -- "issues & drafts < 2" --> D
  C --> H[🙋 human_review<br/>interrupt: approve / edit]
  H --> F[📦 finalize] --> E([END])
```

1. **🛟 safety_router** (rules, instant). Immediate danger — violence, a missing or abducted person, self-harm
   risk, sexual violence, a medical emergency — shows emergency numbers (112, 181, 1098, 14416, 108) before
   anything else. The app runs the same check the moment you submit.
2. **🧭 intake_planner** — one Gemini call that does all the understanding: language; the complaint **translated
   to English once** (everything downstream uses it); company and facts; issue types (e.g. `cyber_fraud`); the kind
   of problem in its own words and the matching verified playbook **only if one genuinely fits** (code trusts the
   match only at ≥ 0.6); the state, authority type and search queries for the escalation planner; an English legal
   search query; the facts this letter still needs; follow-up questions; and any danger the rules missed (it can
   add, never remove). Code then resolves the official domain and plans the searches.
3. **❓ clarify** — only for a very unclear complaint: one question, then intake runs again (asked once).
4. **🔎 case_research** — for a named scheme, policy or product (e.g. PM-VBRY): searches official sources in the
   complainant's state, and the LLM builds a case brief (conditions, entitlement, grievance channel) from the
   fetched pages only. Cached per entity and state.
5. **🗺️ frame** — the brief can re-frame the case (e.g. a government scheme → the administering department).
6. **In parallel** (`Send` fan-out):
   - **grievance** — searches the official domain and the web; rules find every email / phone on a page, the LLM
     labels them and says **whose** contact each is (a company's page often lists partners' officers).
   - **precedents** — forums and news on how similar complaints were resolved (8 s budget).
   - **local_forum** — the local office that matters: consumer commission, cyber-crime police station, SP office,
     or the District Legal Services Authority.
   - **gap_check** — the LLM checks published conditions (a scheme's, or free legal aid) against your facts.
   - **escalation_planner** — the ladder, proposed from evidence by the LLM and validated by code (below).
   - **registry** — loan apps only: RBI's Digital Lending App directory (via LoanLens).
7. **🛡️ verifier** (code) — a contact is verified only if it is on the official domain, in a regulator registry, or
   on two independent sources. Nothing verified → one more grievance search with other wording.
8. **📚 legal_rag** — clauses from the indexed law, using the sector's base query and intake's legal query.
9. **📝 ask_details** — the first pause: **one form** with every missing fact, open condition and follow-up
   question, your name, and the letter language. Skipped when there is nothing to ask.
10. **🔁 reassess** — folds your answers into the facts and re-checks the conditions (only if you answered).
11. **✍️ drafter** — one LLM call plans and writes the letter: cites law only as `[C#]` tags, uses the reply-by date
    computed in code, follows the tier rules, writes in the chosen language.
12. **🧐 critic** — code checks, then an LLM review; problems send the letter back (at most two drafts).
13. **🙋 human_review** — the second pause: approve or edit. **14. 📦 finalize** — builds the letter, re-dates the
    deadlines for today, strips anything uncited, and saves the case.

If the AI is unavailable at any step, the run stops and the app / CLI say so plainly — there is no rule-based
stand-in that would quietly write a worse letter.

### Three tiers

| Tier | Domains | What you get |
|---|---|---|
| **A — draft and escalate** | consumer / e-commerce, banking, telecom, insurance, loan apps, government schemes, public services, and any other sector (the planner finds its route) | Verified contact, cited rules, letter, dated escalation ladder |
| **B — draft and route urgently** | police: cyber fraud and scams, theft, threats, police refusing to register a complaint | Safety check first; a written complaint to the SHO asking for an FIR; 1930 / cybercrime.gov.in first for money lost online; what to do if the police refuse (SP in writing, then the Magistrate) |
| **C — explain and refer** | court cases, property, family and tenancy disputes | A plain-language explainer (never a petition or plaint), eCourts case status by CNR, and the District Legal Services Authority — with the free-legal-aid criteria asked as questions |

### Escalation: the LLM proposes, code validates

`escalation_planner` runs for every tier-A complaint, alongside the contact search (25 s budget). It searches with
the queries intake wrote, fetches pages, retrieves the law, and the LLM proposes an ordered ladder where every step
names its source. Code then:

- keeps a step only if its source is an **official page we fetched** (`*.gov.in`, `*.nic.in`, the organisation's
  own domain, or a domain an official page links to) or a **retrieved clause**; if the LLM cited an aggregator but an
  official page we already have names the same authority, that page becomes the source; an authority found only on
  aggregators gets one direct "official" search;
- keeps a link only if it is official **and** appears in the evidence;
- replaces a timeline (or a duration in a description) its source does not state with "check the timeline in
  <source>";
- orders it: the body's own grievance cell → sector forum / regulator / ombudsman → helplines and portals →
  commissions and courts;
- never routes a state matter to CPGRAMS (the state's own portal instead) — on the final ladder, the letter and
  the sources.

**Sectors with a hand-checked seed** (e-commerce, banking, telecom, insurance, loan apps, government schemes) keep
the seed — with its statutory deadlines — as the base; the planner may only **add** official steps it lacks, never
replace or remove one. Other sectors use the planner's ladder. If the planner fails or times out, the seed or the
labelled general route is used ("We couldn't find a sector-specific route. This is the general government route.").
Verified ladders are cached in `data/ladders/` by sector + state (+ company).

### Deadlines and consistency, in code

- **Deadlines** come from rules checked against the indexed law: E-Commerce Rules Rule 4(5) (acknowledge in 48 h,
  resolve in a month), RB-IOS Clause 10(1) (30 days, then the RBI Ombudsman), TRAI Reg 8 / 9(3), Insurance Ombudsman
  Rule 14(3), Digital Lending Directions Para 11, CPA s.69 (file within two years). Code turns them into dates —
  "reply due by 9 November 2026", "last date to file: 28 September 2028" — for the letter and the ladder. Sectors
  without a statutory rule get no invented date (or a clearly labelled reasonable one).
- **Consistency**: every ₹ amount and date in the letter (English or Hindi — "₹7,400", "7,400 रुपये", "9 नवंबर
  2026") must match one the complainant gave or a computed deadline; the letter must carry the reply-by date.

### Letters: one language, professional layout

The whole letter is in **English or Hindi** — chosen in the form (default: Hindi for Hindi complaints). The drafter
writes the subject, the addressee and the body in that language; the code adds the rest in the same language
(सेवा में / To, दिनांक / Date, विषय / Subject, महोदय/महोदया / Dear Sir/Madam, भवदीय / Yours faithfully, Hindi dates and
headings). The addressee is an office (officer, organisation, district / state), never a portal name. The critic
rejects a mixed-language letter (only placeholders, IDs, abbreviations and the names the user wrote may stay in
Latin script), and finalize drops English glosses in brackets from a Hindi letter.

### Live view: watch it work

While a complaint is being prepared, the main page shows one progress line (the step running now) and a
**🔍 Watch it live ↗** link that opens a second tab. Nothing else changes on the main page, and nothing waits for the
Live view. The second tab shows:

- the flow, phase by phase, with each step marked done / running / waiting for you / pending / may run / skipped / failed;
  each step's card has a **Logs & output** panel with its log lines, errors, the calls it made, what it asked you (if
  it paused) and the state it returned (long text and lists are shortened; one section per run when a step re-ran);
- what is still to do;
- an activity feed with every step's notes and every web search, page read, AI call and law lookup (with timings and
  cache hits);
- counters for steps, working time, searches, pages, AI calls and law lookups.

Finished complaints can be replayed: use "See how it was prepared ↗" on the result, or "Replay timeline ↗" under Past
complaints.

How it works: the run executes on a background thread. The node wrapper (`nodes/common.py`), `timing.span` (every
external call, including pool threads) and `graph.run` append events to `data/runs/<thread_id>.jsonl`
(`saathi/events.py`). The Live view only reads that file once a second. A failed write is ignored, so a complaint is
never affected. The log contains the complaint text and stays local, like `data/cases/`.

## Guardrails: what is code, and why

| Code (checks the LLM) | Where |
|---|---|
| Verified contacts: official domain / registry / two independent sources; lookalike and webmail detection; partners' contacts on a company page are not the company's | `nodes/verifier.py`, `nodes/grievance.py`, `tools/domains.py` |
| Escalation ladder validation (sources, links, timelines, order, CPGRAMS rule) | `nodes/escalation.py`, `nodes/drafter.py` |
| Case briefs: only fetched sources; grievance links must be in the evidence | `nodes/case_research.py` |
| Critic: uncited legal references, unverified contacts, invented IDs, amounts and dates, missing placeholders, eligibility claims the checks don't support (English and Hindi), consumer-law frame on a government scheme, FIR request for police, no court drafting, one language | `nodes/critic.py`, `letter.py` |
| Citations: anything not in the retrieved clauses is stripped at finalize | `nodes/legal_rag.py`, `nodes/review.py` |
| Safety keywords (instant, works even if the LLM is down) | `nodes/safety.py` |

| Data (verified facts with a citation) | Where |
|---|---|
| Seed playbooks for 10 domains, statutory deadline rules, legal-aid criteria (LSA Act s.12), helplines | `playbooks.py`, `nodes/safety.py` |

Everything else — understanding the complaint, routing, research, conditions, questions, drafting, review — is
the LLM, with the code above checking it.

## Code layout

```
config.py  app.py  cli.py  docker-compose.yml  .env.example
saathi/
  state.py         ComplaintState (TypedDict + reducers) and the pydantic models for structured LLM output
  graph.py         StateGraph: safety → intake → fan-out → verifier → RAG → form → drafter ⇄ critic → review
  playbooks.py     seed playbooks (verified routes, deadline rules, required facts, tiers)
  deadlines.py     deadline arithmetic; date and ₹ parsing (English and Hindi)
  letter.py        one-language letter layout (English / Hindi labels, language check)
  llm.py           Gemini via google-genai (generate_content, JSON schema, thinking "low", 1 retry); AIUnavailable
  timing.py        per-node / per-call latency log in the terminal (SAATHI_TIMING)
  events.py        Live view event log (data/runs/<thread_id>.jsonl): emit, read, summarize, feed
  nodes/           safety · intake (+ clarify) · case_research · gap_check (+ frame, reassess) · escalation ·
                   grievance · precedents · local_forum · registry_worker · verifier · legal_rag · drafter ·
                   critic · review (ask_details, human_review, finalize) · common (node wrapper)
  tools/           serp.py (disk cache, credit cap) · fetch.py · domains.py (official-domain resolution)
  rag/             ingest.py (English-only chunks by section, Qdrant) · store.py (retrieval)
eval/              run_eval.py · escalation_eval.py · latency.py · companies.csv · complaints.jsonl · decoys.json
tests/             test_pipeline.py · fake_llm.py (deterministic LLM stand-in, tests only) · conftest.py
raw/legal/         the legal corpus (SOURCES.md)
```

## Legal corpus

Indexed in Qdrant (860 English chunks; Hindi halves of bilingual gazettes are dropped — a Hindi complaint is
translated once at intake). Add a PDF to `raw/legal/`, add an entry to `DOCS` in `rag/ingest.py`, then run
`python cli.py index`.

| Key | Document |
|---|---|
| `cpa` | Consumer Protection Act, 2019 |
| `ecom`, `ecom26` | Consumer Protection (E-Commerce) Rules, 2020; (Amendment) Rules, 2026 |
| `ccr`, `gen` | Consumer Protection (Consumer Disputes Redressal Commissions) Rules, 2020; (General) Rules, 2020 |
| `ios` | Reserve Bank – Integrated Ombudsman Scheme, 2026 |
| `rbc`, `rbc26` | RBI (Commercial Banks – Responsible Business Conduct) Directions, 2025; Third Amendment Directions, 2026 (customer liability for unauthorised e-banking transactions from 1 Jan 2027) |
| `dl` | RBI (Digital Lending) Directions, 2025 |
| `trai` | Telecom Consumers Complaint Redressal Regulations, 2012 |
| `ins`, `ins23` | Insurance Ombudsman Rules, 2017; (Amendment) Rules, 2023 |

## SerpApi usage

| Engine | Used for |
|---|---|
| `google` | Official domains; grievance pages (`site:<official domain>`); route and scheme research; one direct check per unverified authority |
| `google_forums`, `google_news` | How similar complaints were resolved |
| `google_maps` | The local office (consumer commission, cyber-crime police station, DLSA) |

The cap is `SERPAPI_MAX_CREDITS` (default 12) live calls per complaint. Every response is cached in
`data/cache/serp/`, so a repeated complaint costs nothing, and failed requests aren't charged.

## Speed

Measured with `eval/latency.py` on a real, uncached complaint: **100.5 s → 26.1 s**. What changed: Gemini through
`generate_content` (≈1.5 s per call vs ≈4.5 s through the Interactions API); grievance extraction only on pages
worth reading, in parallel; the safety check and the follow-up questions folded into intake's single LLM call;
condition checks and escalation planning run alongside the searches; plan and write in one drafter call;
precedents time-boxed. Warm (cached) runs take well under a second.

Set `SAATHI_TIMING=calls` to see every search, fetch and LLM call in the terminal; the default (`nodes`) prints one
line per node and a summary table at each pause.

## Evaluation

| Script | What it measures | Latest result |
|---|---|---|
| `eval/escalation_eval.py` | Escalation planner on sectors **without** a seed (electricity, water, gas, university, airline, municipal, plus an e-commerce control): expected sector authority found, CPGRAMS never used for a state matter, every step sourced | **7/7** found the expected authority (CGRF + Electricity Ombudsman, MoPNG e-Seva, UGC, AirSewa, Karnataka's grievance portal, NCH) · 0 CPGRAMS on state matters · 7/7 every step sourced |
| `eval/escalation_eval.py --seeded` | The same for sectors **with** a seed (bank, insurance, telecom), compared side by side with the seed | The planner found the right regulator 3/3, but missed steps the seeds have (Principal Nodal Officer, Bima Bharosa) — hence "seed as the base, planner may only add" |
| `eval/run_eval.py` | A. contacts against hand-labelled `companies.csv`; B. complaints (`complaints.jsonl`: sector, language, critic pass, uncited claims, case-specific facts used, correct legal frame, template overlap between letters); C. decoys | C: **11/11** decoys rejected, **5/5** genuine contacts kept. A and B need a fresh run with the AI (`eval/REPORT.md` still shows an earlier rule-based run) |
| `eval/latency.py` | Time per node and per external call, cold and warm | See *Speed* |

## Configuration

| Variable | Default | |
|---|---|---|
| `GEMINI_API_KEY` | — | Required (also read from `GOOGLE_API_KEY`) |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Thinking level is "low" for every call |
| `LLM_API` | `generate_content` | `interactions` opts into the slower Interactions API |
| `SERPAPI_API_KEY` | — | Required for live search |
| `SERPAPI_MAX_CREDITS` | `12` | Live calls per complaint (`0` / `none` = no cap) |
| `QDRANT_URL` | `http://localhost:6333` | Falls back to embedded Qdrant in `data/qdrant/` |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Local embeddings (fastembed) |
| `SAATHI_TIMING` | `nodes` | `calls` / `off` |
| `LOANLENS_DIR` | `../Loanlens` | RBI Digital Lending App directory for loan-app complaints |
| `SAATHI_DATA_DIR` | `./data` | Caches, checkpoints, cases, ladders |

## Design choices that differ from the brief

| Brief | This build | Why |
|---|---|---|
| chromadb / FAISS | **Local Qdrant** (Docker), with an embedded fallback | Embedded mode lets only one process open the index, so the server is recommended for the app + CLI together |
| `langchain-anthropic` | **Gemini via the google-genai SDK** (`generate_content`, `gemini-3.1-flash-lite`) | The team's API; measured fastest of the two Gemini APIs |
| `registry/` copied into this repo | The **LoanLens** registry in the sibling folder (`LOANLENS_DIR`) | One source of RBI data |
| A fixed list of sectors | Seed playbooks **plus** an evidence-based escalation planner for any sector | Generalises to sectors never built (water boards, gas, universities…) while keeping hand-checked routes where they exist |

## Limitations

- Many state government sites are JavaScript-heavy or poorly indexed, so official evidence is sometimes missing; the
  ladder then stays thin (or uses the labelled general route) rather than citing an unofficial page. In the test set
  this happened for Delhi Jal Board before official-page re-sourcing was added.
- The seed ladders are traceable only to the playbook, not to a clause or URL per step.
- "Two independent sources" could be gamed by coordinated spam — phone numbers seen only on third-party sites always
  carry a warning.
- The agents need the AI: without a Gemini key, or if Gemini fails after its retry, nothing is drafted.

## AI tools used

- **Claude Code**: built the project.
- **Gemini (`gemini-3.1-flash-lite`) via google-genai `generate_content`**: intake, case research, condition checks,
  escalation planning, contact extraction, precedent relevance, drafting and review — all with schema-constrained
  JSON validated by pydantic, and checked by the code guards above.
- **BAAI/bge-small-en-v1.5** (fastembed, local): embeds the legal corpus in Qdrant.
