"""Shikayat Saathi - Streamlit UI driving the LangGraph (streamed node updates, interrupt-based review).
Run: streamlit run app.py"""
import html
import json
import urllib.parse

import pandas as pd
import streamlit as st

from config import CASES_DIR, ROOT, SERPAPI_API_KEY
from saathi import llm
from saathi.tools.serp import serp

st.set_page_config(page_title="Shikayat Saathi · शिकायत साथी", page_icon="📣", layout="wide")

st.markdown("""
<style>
:root { --navy:#0F1B33; --navy2:#1E3A6E; --saffron:#F59E0B; --saffron2:#FB923C; --green:#16A34A;
        --amber:#D97706; --ink:#0F1B33; --muted:#5B6478; --card:#FFFFFF; --line:#E3E7F0; }
.block-container { padding-top: 3.2rem; max-width: 1180px; }
.hero { background: radial-gradient(1200px 400px at 85% -20%, rgba(245,158,11,.35), transparent 60%),
        linear-gradient(135deg, var(--navy) 0%, var(--navy2) 100%);
        color:#fff; border-radius: 22px; padding: 30px 34px 26px; margin-bottom: 18px;
        box-shadow: 0 18px 40px -18px rgba(15,27,51,.55); }
.hero h1 { font-size: 2.6rem; line-height: 1.05; margin: 0 0 6px; color:#fff; letter-spacing:-.02em; padding:0; }
.hero h1 .accent { background: linear-gradient(90deg, var(--saffron), var(--saffron2));
        -webkit-background-clip: text; background-clip: text; color: transparent; }
.hero .hindi { font-size: 1.15rem; opacity: .92; margin-bottom: 10px; }
.hero p { font-size: 1.02rem; opacity: .86; max-width: 760px; margin: 0 0 16px; color:#fff; }
.stats { display:flex; gap:10px; flex-wrap:wrap; }
.stat { background: rgba(255,255,255,.10); border:1px solid rgba(255,255,255,.18); border-radius: 999px;
        padding: 6px 14px; font-size: .88rem; }
.stat b { color: var(--saffron); }
.pipe { display:grid; grid-template-columns: repeat(auto-fit, minmax(92px, 1fr)); gap:10px; margin: 6px 0 14px; }
.stage { background: var(--card); border:1px solid var(--line); border-radius: 14px; padding: 12px 10px;
         text-align:center; transition: all .25s ease; }
.stage .ico { font-size: 1.5rem; } .stage .nm { font-weight: 650; font-size: .9rem; color: var(--ink); }
.stage .st { font-size: .74rem; color: var(--muted); margin-top: 2px; }
.stage.done { border-color: rgba(22,163,74,.45); background: linear-gradient(180deg,#F0FDF4,#fff); }
.stage.done .st { color: var(--green); font-weight:600; }
.stage.warn { border-color: rgba(217,119,6,.5); background: linear-gradient(180deg,#FFFBEB,#fff); }
.stage.warn .st { color: var(--amber); font-weight:600; }
.stage.run { border-color: var(--saffron); box-shadow: 0 0 0 3px rgba(245,158,11,.18); }
.stage.run .st { color: var(--saffron); font-weight:600; }
.stage.todo { opacity: .55; }
.sendcard { border-radius: 18px; padding: 20px 22px; margin: 6px 0 14px; color:#fff;
            background: linear-gradient(135deg, #15803D, #16A34A 55%, #22C55E);
            box-shadow: 0 14px 30px -16px rgba(22,163,74,.7); }
.sendcard.none { background: linear-gradient(135deg, #B45309, #D97706); box-shadow: 0 14px 30px -16px rgba(217,119,6,.7); }
.sendcard .k { text-transform: uppercase; letter-spacing: .08em; font-size: .75rem; opacity: .85; }
.sendcard .v { font-size: 1.55rem; font-weight: 750; margin: 2px 0 4px; word-break: break-all; }
.sendcard .why { font-size: .9rem; opacity: .92; }
.route { display:inline-flex; align-items:center; gap:8px; background:#fff; border:1px solid var(--line);
         border-radius: 999px; padding: 6px 14px; font-weight: 600; font-size: .92rem; margin: 0 6px 8px 0; }
.route .dot { width:9px; height:9px; border-radius:50%; background: var(--saffron); }
.urgent { background: #FEF2F2; border:1px solid #FECACA; color: #991B1B; border-radius: 14px;
          padding: 12px 16px; font-weight: 600; margin-bottom: 12px; }
.ccard { background: var(--card); border:1px solid var(--line); border-left: 5px solid var(--green);
         border-radius: 14px; padding: 12px 14px; margin-bottom: 10px; }
.ccard.unv { border-left-color: var(--amber); background: #FFFDF7; }
.ccard .top { display:flex; justify-content:space-between; gap:10px; align-items:center; }
.ccard .val { font-weight: 700; font-size: 1.02rem; word-break: break-all; }
.badge { border-radius: 999px; padding: 2px 10px; font-size: .74rem; font-weight: 700; white-space: nowrap; }
.badge.ok { background:#DCFCE7; color:#166534; } .badge.no { background:#FEF3C7; color:#92400E; }
.ccard .meta { color: var(--muted); font-size: .85rem; margin-top: 3px; }
.ccard .warn { color: #B45309; font-size: .82rem; margin-top: 4px; }
.timeline { position: relative; margin: 4px 0 0 14px; padding-left: 26px; border-left: 3px solid var(--line); }
.tstep { position: relative; margin-bottom: 18px; }
.tstep .num { position:absolute; left:-41px; top:-2px; width:28px; height:28px; border-radius:50%;
              background: var(--navy); color:#fff; display:flex; align-items:center; justify-content:center;
              font-weight:700; font-size:.85rem; box-shadow: 0 0 0 4px #F7F8FC; }
.tstep:first-child .num { background: var(--saffron); }
.tstep .ttl { font-weight: 700; } .tstep .wait { color: var(--muted); font-size: .82rem; }
.tstep .how { font-size: .92rem; margin-top: 3px; }
.paper { background: #fff; border: 1px solid var(--line); border-radius: 12px; padding: 26px 30px;
         font-family: Georgia, 'Times New Roman', serif; font-size: .98rem; line-height: 1.55; white-space: pre-wrap;
         box-shadow: 0 10px 28px -18px rgba(15,27,51,.35); max-height: 560px; overflow-y: auto; color: var(--ink); }
.law { background:#fff; border:1px solid var(--line); border-radius: 12px; padding: 12px 14px; margin-bottom: 10px; }
.law .tag { display:inline-block; background: var(--navy); color:#fff; border-radius: 6px; padding: 1px 8px;
            font-size: .78rem; font-weight: 700; margin-right: 8px; }
.law .cit { font-weight: 700; } .law .ttl { color: var(--muted); font-size: .88rem; }
.sim { background:#fff; border:1px solid var(--line); border-radius: 12px; padding: 10px 14px; margin-bottom: 8px; }
.sim .kind { font-size:.72rem; font-weight:700; text-transform:uppercase; color: var(--navy2); letter-spacing:.06em; }
.muted { color: var(--muted); font-size: .86rem; }
.tlog { background:#0F1B33; color:#E6EAF5; border-radius:12px; padding:12px 16px; font-family: ui-monospace, Menlo, monospace;
        font-size:.8rem; line-height:1.55; margin-bottom:14px; max-height: 300px; overflow-y:auto; }
div[data-testid="stForm"] { background:#fff; border:1px solid var(--line); border-radius: 18px; padding: 18px 20px; }
.stTabs [data-baseweb="tab"] { font-weight: 600; }
</style>
""", unsafe_allow_html=True)

E = html.escape
STAGES = [("intake_planner", "🧭", "Intake"), ("registry", "🏛️", "Registry"), ("grievance", "🔎", "Grievance"),
          ("precedents", "📰", "Precedents"), ("local_forum", "📍", "Forum"), ("verifier", "🛡️", "Verify"),
          ("legal_rag", "📚", "Law"), ("drafter", "✍️", "Draft"), ("critic", "🧐", "Critic"),
          ("human_review", "🙋", "Review"), ("finalize", "📦", "Final")]
ROLE_LABEL = {"grievance_officer": "Grievance Officer", "principal_nodal_officer": "Principal Nodal Officer",
              "nodal_officer": "Nodal Officer", "appellate_authority": "Appellate Authority",
              "grievance_cell": "Grievance cell", "customer_care": "Customer care", "other": "Contact"}
EXAMPLES = {
    "🛒 Flipkart refund (Hinglish)": ("Flipkart ne damaged phone ka refund reject kar diya, order 15 Sep ko kiya "
                                     "tha", "Flipkart", "Bengaluru"),
    "📵 Loan app harassment": ("Recovery agents of the KreditBee app are calling my relatives and threatening me "
                              "though my EMI is only 3 days late.", "KreditBee", "Hyderabad"),
    "🏦 Unauthorised UPI debit": ("₹12,500 was debited from my ICICI Bank account through a UPI transaction I did "
                                 "not make. I complained 35 days ago and got no reply.", "ICICI Bank", "Mumbai"),
    "🩺 Claim rejected (हिंदी)": ("स्टार हेल्थ ने मेरा 85,000 रुपये का क्लेम रिजेक्ट कर दिया।", "Star Health", "Chennai"),
}


def eval_stats() -> dict:
    try:
        rep = json.loads((ROOT / "eval" / "report.json").read_text())
        return {"decoys": f"{rep['decoys']['decoys_caught']}/{rep['decoys']['decoys']}",
                "critic": rep["complaints"]["critic_pass_rate"], "sector": rep["complaints"]["sector_accuracy"]}
    except Exception:
        return {}


def hero() -> None:
    ev = eval_stats()
    extra = (f"<span class='stat'>🛡️ <b>{ev['decoys']}</b> scam-pattern contacts blocked</span>"
             f"<span class='stat'>🎯 <b>{ev['sector']}%</b> sector accuracy</span>") if ev else ""
    st.markdown(f"""
<div class="hero">
  <h1>Shikayat <span class="accent">Saathi</span></h1>
  <div class="hindi">आपकी शिकायत, सही जगह तक — <i>a complaints lawyer's first hour, free, in your language.</i></div>
  <p>Describe what went wrong. A LangGraph team of agents finds the right regulator, a <b>verified</b> grievance
     officer (never a fake helpline), the exact law on your side, drafts the complaint — and waits for your OK.</p>
  <div class="stats"><span class="stat">🕸️ <b>11</b> agents · 2 self-correcting loops</span>
    <span class="stat">⚖️ <b>6</b> laws · <b>288</b> clauses</span>{extra}
    <span class="stat">🔎 Google · Forums · News · Maps via SerpApi</span></div>
</div>""", unsafe_allow_html=True)


def pipeline_html(counts: dict, current: str | None, problems: set, sector: str | None) -> str:
    cards = []
    for node, ico, name in STAGES:
        if node == "registry" and sector != "loan_app" and not counts.get("registry"):
            continue
        n = counts.get(node, 0)
        if node in problems:
            cls, label = "warn", "partial"
        elif node == current:
            cls, label = "run", "working…"
        elif n:
            cls, label = "done", "✓ done" + (f" ×{n}" if n > 1 else "")
        else:
            cls, label = "todo", "waiting"
        cards.append(f"<div class='stage {cls}'><div class='ico'>{ico}</div><div class='nm'>{name}</div>"
                     f"<div class='st'>{label}</div></div>")
    return f"<div class='pipe'>{''.join(cards)}</div>"


def contact_card(x: dict) -> str:
    ok = x["verified"]
    name = (f" · {E(x['name'])}" + (" ✓" if x.get("name_verified") else "")) if x.get("name") else ""
    warns = "".join(f"<div class='warn'>⚠ {E(w)}</div>" for w in x.get("warnings") or [])
    src = f" · <a href='{E(x['source_url'])}' target='_blank'>source ↗</a>" if (x.get("source_url") or "").startswith(
        "http") else ""
    icon = "✉️" if x["type"] == "email" else "📞"
    return (f"<div class='ccard {'' if ok else 'unv'}'><div class='top'><div class='val'>{icon} {E(x['value'])}</div>"
            f"<span class='badge {'ok' if ok else 'no'}'>{'VERIFIED' if ok else 'UNVERIFIED'}</span></div>"
            f"<div class='meta'>{E(ROLE_LABEL.get(x['role'], x['role']))}{name} — "
            f"{'verified because ' if ok else ''}{E(x['reason'])}{src}</div>{warns}</div>")


def ladder_html(steps: list[dict]) -> str:
    items = []
    for i, s in enumerate(steps, 1):
        link = f" <a href='{E(s['url'])}' target='_blank'>{E(s['url'].split('//')[-1])} ↗</a>" if s.get("url") else ""
        phone = f" · ☎ {E(s['phone'])}" if s.get("phone") else ""
        wait = f"<div class='wait'>⏳ {E(s['typical_wait'])}</div>" if s.get("typical_wait") else ""
        items.append(f"<div class='tstep'><div class='num'>{i}</div><div class='ttl'>{E(s['name'])}</div>{wait}"
                     f"<div class='how'><b>{E(s['channel'])}</b> — {E(s['when_to_use'])}{link}{phone}</div></div>")
    return f"<div class='timeline'>{''.join(items)}</div>"


def mailto(to: str, subject: str, body: str) -> str:
    body = body if len(body) < 1800 else body[:1800] + "\n…"  # mail clients cap mailto length
    return f"mailto:{to}?" + urllib.parse.urlencode({"subject": subject, "body": body}, quote_via=urllib.parse.quote)


def header_chips(s: dict) -> None:
    from saathi.playbooks import SECTORS

    f = s.get("facts") or {}
    chips = [f"<span class='route'><span class='dot'></span>{E(SECTORS[s['sector']]['label'])}</span>",
             f"<span class='route'>⚖️ {E(SECTORS[s['sector']]['regulator'])}</span>",
             f"<span class='route'>🗣️ {E(s.get('language', ''))}</span>"]
    if s.get("company"):
        chips.append(f"<span class='route'>🏢 {E(s['company'])}"
                     + (f" · {E(s['company_domain'])}" if s.get("company_domain") else "") + "</span>")
    if f.get("amount"):
        chips.append(f"<span class='route'>₹ {E(f['amount'])}</span>")
    st.markdown("".join(chips), unsafe_allow_html=True)
    if s.get("urgent_note"):
        st.markdown(f"<div class='urgent'>🚨 {E(s['urgent_note'])}</div>", unsafe_allow_html=True)
    if s.get("errors"):
        st.warning("Some searches didn't complete, so results may be incomplete: "
                   + "; ".join(e.split(":")[0] for e in s["errors"][:4]) + ". Re-running is cheap — results are cached.")


def contacts_section(s: dict) -> None:
    ver, unv = s.get("verified_contacts") or [], s.get("unverified_contacts") or []
    a, b = st.columns(2, gap="large")
    with a:
        st.markdown(f"##### ✅ Verified ({len(ver)})")
        st.markdown("".join(contact_card(x) for x in ver[:8]) or "<div class='muted'>No verified contact found — "
                    "use the company's official complaint form.</div>", unsafe_allow_html=True)
    with b:
        st.markdown(f"##### ⚠️ Unverified — use with caution ({len(unv)})")
        st.markdown("".join(contact_card(x) for x in unv[:8]) or "<div class='muted'>Nothing suspicious found.</div>",
                    unsafe_allow_html=True)
    if s.get("registry"):
        r = s["registry"]
        icon = {"listed": "✅", "lookalike": "⛔", "not_listed": "⚠️"}.get(r.get("status"), "ℹ️")
        st.markdown(f"**{icon} RBI Digital Lending App directory** (data as on {r.get('data_as_on')}): "
                    f"`{r.get('status')}`")
        if r.get("lenders"):
            st.dataframe(pd.DataFrame(r["lenders"]), hide_index=True, width="stretch")


def review_panel(s: dict, payload: dict) -> dict | None:
    st.markdown("### 🙋 Your review — the graph is paused here")
    st.caption("Nothing is sent. Fill in what's missing, edit anything, then approve to finalise.")
    rec = s.get("recommended")
    if rec:
        st.success(f"Will be addressed to **{rec['value']}**" + (f" ({rec['name']})" if rec.get("name") else "")
                   + f" — verified because {rec['reason']}")
    else:
        st.warning("No verified email — the letter will tell you to use the official complaint form.")
    if payload.get("critic_notes"):
        st.info("Critic notes still open: " + " · ".join(payload["critic_notes"][:4]))
    with st.form("review_form"):
        fills = {}
        if payload.get("missing_facts"):
            st.markdown("**Missing facts** (leave blank to keep the placeholder)")
            cols = st.columns(min(3, len(payload["missing_facts"])))
            for i, m in enumerate(payload["missing_facts"]):
                fills[m["placeholder"]] = cols[i % len(cols)].text_input(m["placeholder"], "")
        subject = st.text_input("Subject", payload.get("subject") or "")
        draft = st.text_area("Draft", payload.get("draft") or "", height=280)
        c1, c2, c3 = st.columns(3)
        me = {"name": c1.text_input("Your name", (s.get("complainant") or {}).get("name", "")),
              "phone": c2.text_input("Your phone", ""), "email": c3.text_input("Your email", "")}
        ok = st.form_submit_button("✅ Approve & finalise", type="primary", width="stretch")
    if ok:
        return {"draft": draft, "subject": subject, "fills": {k: v for k, v in fills.items() if v},
                "complainant": {k: v for k, v in me.items() if v}}
    return None


def final_view(s: dict) -> None:
    f = s.get("final") or {}
    rec = s.get("recommended")
    if rec:
        st.markdown(f"""<div class="sendcard"><div class="k">✅ Send your complaint to</div>
<div class="v">{E(rec['value'])}</div><div class="why">{E(rec.get('name') or '')} · verified because
{E(rec['reason'])}</div></div>""", unsafe_allow_html=True)
    else:
        st.markdown(f"""<div class="sendcard none"><div class="k">⚠️ No verified email</div>
<div class="v">Use {E(s.get('company') or 'the company')}'s official complaint form</div>
<div class="why">We only show contacts verified on the official site, a regulator registry or two independent
sources.</div></div>""", unsafe_allow_html=True)
    if s.get("summary"):
        st.info("💬 " + s["summary"])
    t1, t2, t3, t4, t5 = st.tabs(["✍️ Your complaint", "🛡️ Contacts", "🪜 Escalation", "⚖️ Law cited",
                                  "📰 Precedents & forum"])
    with t1:
        left, right = st.columns([3, 2], gap="large")
        left.markdown(f"<div class='paper'>{E(f.get('letter', ''))}</div>", unsafe_allow_html=True)
        with right:
            st.download_button("⬇️ Download .txt", f.get("letter", ""), file_name=f"complaint_{s.get('company')}.txt",
                               width="stretch")
            st.download_button("⬇️ Download .md", f.get("markdown", ""), file_name="complaint.md", width="stretch")
            if rec:
                body = f.get("letter", "").split("Dear Sir/Madam,", 1)[-1].strip()
                st.link_button("📧 Open in email", mailto(rec["value"], f.get("subject", "Complaint"), body),
                               width="stretch")
            if f.get("removed_citations"):
                st.caption(f"🧹 Removed unsupported citations: {', '.join(f['removed_citations'])}")
            st.caption(f"Drafted by {s.get('draft_method')} in {s.get('draft_attempts')} attempt(s). Every legal "
                       "reference maps to a retrieved clause. Nothing has been sent.")
    with t2:
        contacts_section(s)
    with t3:
        st.markdown(ladder_html(s.get("escalation") or []), unsafe_allow_html=True)
    with t4:
        for c in f.get("cited") or s.get("clauses") or []:
            st.markdown(f"<div class='law'><span class='tag'>{E(c['tag'])}</span><span class='cit'>{E(c['act'])}, "
                        f"{E(c['section'])}</span><div class='ttl'>{E(c['title'])} · {E(c['source_file'])} · match "
                        f"{c['score']}</div></div>", unsafe_allow_html=True)
            with st.expander(f"Read {c['tag']} in full"):
                st.write(c["text"])
    with t5:
        lf = s.get("local_forum")
        if lf:
            st.markdown(f"<div class='law'><div class='cit'>📍 {E(lf['name'])}</div><div class='ttl'>"
                        f"{E(lf.get('address') or '')} {E(lf.get('phone') or '')}</div></div>", unsafe_allow_html=True)
            if lf.get("maps_link"):
                st.link_button("Open in Google Maps", lf["maps_link"])
        for p in s.get("precedents") or []:
            st.markdown(f"<div class='sim'><div class='kind'>{E(p['kind'])} · {E(str(p.get('source') or ''))}</div>"
                        f"<a href='{E(p['url'])}' target='_blank'>{E(p['title'])}</a>"
                        + (f"<div class='muted'>✔ {E(p['how_resolved'])}</div>" if p.get("how_resolved") else "")
                        + "</div>", unsafe_allow_html=True)
        if not lf and not s.get("precedents"):
            st.caption("No precedents or forum found (or searches were skipped).")


class Live:
    """Streams graph updates into the pipeline cards + trace log."""

    def __init__(self, sector=None):
        self.board, self.log = st.empty(), st.empty()
        self.counts, self.problems, self.lines, self.sector = {}, set(), [], sector

    def __call__(self, node: str, update: dict) -> None:
        if node == "__interrupt__":
            return
        self.counts[node] = self.counts.get(node, 0) + 1
        self.sector = update.get("sector") or self.sector
        if update.get("errors"):
            self.problems.add(node)
        self.lines += update.get("trace") or []
        self.draw()

    def draw(self, current=None):
        self.board.markdown(pipeline_html(self.counts, current, self.problems, self.sector), unsafe_allow_html=True)
        self.log.markdown("<div class='tlog'>" + "<br>".join(E(x) for x in self.lines[-14:]) + "</div>",
                          unsafe_allow_html=True)


def trace_from_state(s: dict) -> Live:
    lv = Live(s.get("sector"))
    lv.lines = s.get("trace") or []
    for line in lv.lines:
        for node, ico, _ in STAGES:
            if line.startswith(ico):
                lv.counts[node] = lv.counts.get(node, 0) + 1
    return lv


# --------------------------------------------------------------------------- pages

with st.sidebar:
    st.markdown("## 📣 Shikayat Saathi")
    page = st.radio("Go to", ["File a complaint", "Past cases", "Evaluation", "How it works"],
                    label_visibility="collapsed")
    st.divider()
    st.caption(f"🤖 {llm.label()}")
    st.caption("🔎 SerpApi: " + (f"live (cap {serp.max_credits}/complaint)" if SERPAPI_API_KEY else "cache only"))

if page == "File a complaint":
    from saathi import graph as G

    hero()
    pick = st.pills("Examples", list(EXAMPLES), label_visibility="collapsed", key="example")
    text0, co0, city0 = EXAMPLES.get(pick, ("", "", ""))
    with st.form("complaint_form"):
        complaint = st.text_area("What went wrong? (English, हिंदी or Hinglish)", text0, height=110,
                                 placeholder="e.g. Amazon delivered a fake product and is refusing to refund me…")
        c1, c2, c3 = st.columns(3)
        company = c1.text_input("Company / app", co0, placeholder="optional")
        city = c2.text_input("Your city", city0, placeholder="for the nearest consumer commission")
        name = c3.text_input("Your name", "", placeholder="for the letter")
        live = st.toggle("Search live with SerpApi", value=bool(SERPAPI_API_KEY), disabled=not SERPAPI_API_KEY)
        go = st.form_submit_button("🚀  Build my complaint", type="primary", width="stretch")

    if go and complaint.strip():
        serp.offline = not live
        thread = G.new_thread()
        lv = Live()
        lv.draw(current="intake_planner")
        with st.spinner("Agents at work…"):
            state = G.run(G.initial_state(complaint, company=company, city=city,
                                          complainant={"name": name} if name else None, live=live), thread,
                          on_update=lv)
        st.session_state.update(thread=thread, state=state)
        st.rerun()

    s = st.session_state.get("state")
    if s:
        lv = trace_from_state(s)
        lv.draw()
        header_chips(s)
        if "__interrupt__" in s and not s.get("final"):
            contacts_section(s)
            edits = review_panel(s, s["__interrupt__"])
            if edits is not None:
                lv2 = trace_from_state(s)
                with st.spinner("Finalising…"):
                    st.session_state["state"] = G.resume(st.session_state["thread"], edits, on_update=lv2)
                st.toast("Your complaint is ready ✍️")
                st.rerun()
        elif s.get("final"):
            final_view(s)
        with st.expander("🧭 Full agent trace"):
            st.code("\n".join(s.get("trace") or []), language=None)
            st.caption(f"thread_id {st.session_state.get('thread')} · {s.get('live_calls', 0)} live SerpApi call(s)")

elif page == "Past cases":
    st.title("🗂️ Past cases")
    files = sorted(CASES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        st.info("No finalised cases yet.")
    else:
        cases = [json.loads(p.read_text()) for p in files[:50]]
        i = st.selectbox("Case", range(len(cases)), format_func=lambda i: f"{cases[i].get('company') or '?'} · "
                         f"{cases[i]['complaint'][:80]}")
        c = cases[i]
        trace_from_state(c).draw()
        header_chips(c)
        final_view(c)

elif page == "Evaluation":
    st.title("📊 How well does it work?")
    ev = eval_stats()
    if ev:
        m1, m2, m3 = st.columns(3)
        m1.metric("Scam decoys blocked", ev["decoys"])
        m2.metric("Sector accuracy", f"{ev['sector']}%")
        m3.metric("Drafts passing the critic", f"{ev['critic']}%")
    rep = ROOT / "eval" / "REPORT.md"
    st.markdown(rep.read_text() if rep.exists() else "Run `python eval/run_eval.py` first.")

else:
    st.title("⚙️ How it works")
    st.markdown(pipeline_html({n: 1 for n, _, _ in STAGES}, None, set(), "loan_app"), unsafe_allow_html=True)
    st.markdown("""
```
START → intake_planner ─[Send]→ grievance · precedents · local_forum · (registry if loan app)   parallel
                         all workers → verifier                                                   fan-in
verifier ── no verified contact & attempts < 2 ──→ grievance (new phrasing)                       loop 1
verifier ──→ legal_rag → drafter → critic ── issues & drafts < 2 ──→ drafter                      loop 2
critic ──→ human_review (interrupt) → finalize → END                    checkpointed per thread (SQLite)
```

| Agent | Job | Guardrail |
|---|---|---|
| 🧭 Intake | language, sector, company, facts, missing facts, official domain, search plan | routing comes from a fixed playbook, not the model |
| 🔎 Grievance | `site:<official domain>` searches + one open-web query, page fetch, extraction | LLM values not literally on the page are discarded |
| 📰 Precedents · 📍 Forum · 🏛️ Registry | Forums/News, Maps, RBI loan-app directory | run in parallel via `Send` |
| 🛡️ Verifier | official domain · regulator registry · 2 independent sources | look-alike helplines & webmail never verify; retries search once |
| 📚 Law | clauses from 6 Indian laws (local Qdrant) | only retrieved clauses may be cited |
| ✍️ Draft · 🧐 Critic | letter, escalation, summary in your language; critic checks citations, contacts, invented facts | loops back once; finalize strips anything still unsupported |
| 🙋 Review | the graph pauses (`interrupt`) for your edits | nothing is ever sent automatically |
""")

st.markdown("<div class='muted' style='text-align:center;margin-top:28px'>Information, not legal advice.</div>",
            unsafe_allow_html=True)
