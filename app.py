"""Shikayat Saathi - Streamlit UI driving the LangGraph (streamed node updates, interrupt-based review).
Run: streamlit run app.py"""
import html
import json
import logging
import urllib.parse

import pandas as pd
import streamlit as st

from config import CASES_DIR, SERPAPI_API_KEY
from saathi.deadlines import step_dates
from saathi.llm import AIUnavailable
from saathi.tools.serp import serp

LOGGER = logging.getLogger(__name__)

st.set_page_config(page_title="Shikayat Saathi · शिकायत साथी", page_icon="📣", layout="wide",
                   initial_sidebar_state="collapsed")

st.markdown("""
<style>
:root { --ink:#172A3A; --muted:#596B73; --paper:#F3F6F4; --white:#FFFFFF; --line:#D9E2DE;
    --green:#176B55; --green-soft:#E9F4EF; --saffron:#E6A23C; --amber:#9A5B08; --amber-soft:#FFF5E2;
    --red:#A23A34; --red-soft:#FFF0EE; }
.stApp { background:var(--paper); background-image:radial-gradient(#D9E2DE 0.65px, transparent 0.65px);
     background-size:18px 18px; color:var(--ink); }
.block-container { max-width:1220px; padding:1.6rem 2rem 2rem; }
[data-testid="stSidebar"] { background:#172A3A; }
[data-testid="stSidebar"] * { color:#F5F7F6; }
[data-testid="stSidebar"] [data-testid="stRadio"] label { border-radius:7px; }
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { color:#C5D4CF; }
.hero { display:grid; grid-template-columns:minmax(0, 1fr) auto; gap:22px; align-items:center;
    background:#172A3A; color:#fff; border-radius:10px; border-left:6px solid var(--saffron);
    padding:24px 30px; margin:4px 0 22px; box-shadow:0 12px 28px rgba(23,42,58,.12);
    animation:enter .45s ease-out both; }
.hero h1 { font-family:Georgia, 'Palatino Linotype', serif; font-size:2.25rem; line-height:1.12;
      margin:0 0 5px; color:#fff; padding:0; }
.hero h1 .accent { color:#F0B24F; }
.hero .hindi { color:#D3E1DB; font-size:1rem; margin:0 0 12px; }
.hero p { max-width:640px; color:#F4F7F5; font-size:1rem; margin:0; line-height:1.55; }
.hero-side { min-width:210px; border-left:1px solid #52636C; padding-left:22px; }
.hero-side strong { display:block; color:#F0B24F; font-size:.78rem; text-transform:uppercase; }
.hero-side span { display:block; color:#EFF4F1; margin-top:4px; }
.eyebrow { color:var(--green); font-size:.75rem; font-weight:750; text-transform:uppercase; }
.section-title { font-family:Georgia, 'Palatino Linotype', serif; color:var(--ink); margin:2px 0 0; }
.navbrand { color:var(--ink); font-weight:750; padding:7px 0; }
[data-testid="stRadio"] div[role="radiogroup"] { justify-content:flex-end; gap:6px; }
[data-testid="stRadio"] label { border:1px solid transparent; border-radius:6px; padding:5px 9px; }
[data-testid="stRadio"] label:has(input:checked) { background:#E4F0E9; color:#14573F; }
.journey { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:8px; margin:0 0 20px; }
.journey-step { display:flex; align-items:center; gap:9px; min-height:42px; padding:8px 12px;
                background:#E7ECE9; border-radius:6px; color:#50615B; font-size:.84rem; font-weight:650; }
.journey-step b { display:grid; place-items:center; width:24px; height:24px; flex:0 0 24px;
                  border:1px solid #B8C5BF; border-radius:50%; font-size:.74rem; }
.journey-step.active { background:#E4F0E9; color:#14573F; box-shadow:inset 0 -2px #176B55; }
.journey-step.active b, .journey-step.done b { background:#176B55; color:#fff; border-color:#176B55; }
.journey-step.done { color:#176B55; }
.pipe { display:grid; grid-template-columns:repeat(auto-fit, minmax(118px, 1fr)); gap:8px; margin:8px 0 14px; }
.stage { min-height:88px; background:var(--white); border:1px solid var(--line); border-radius:8px;
     padding:11px 8px; text-align:center; animation:enter .3s ease-out both; }
.stage .ico { font-size:1.25rem; } .stage .nm { font-weight:700; font-size:.85rem; color:var(--ink); }
.stage .st { font-size:.72rem; color:var(--muted); margin-top:3px; }
.stage.done { border-color:#A7CFBC; background:var(--green-soft); }
.stage.done .st { color:var(--green); font-weight:700; }
.stage.warn { border-color:#E7C37F; background:var(--amber-soft); }
.stage.warn .st { color:var(--amber); font-weight:700; }
.stage.run { border-color:var(--saffron); box-shadow:0 0 0 2px #F8E7C7; }
.stage.run .st { color:#80500D; font-weight:700; }
.stage.todo { opacity:.64; }
.sendcard { border-radius:8px; padding:18px 20px; margin:6px 0 14px; color:#fff;
        background:var(--green); border-left:6px solid #8BC5A6; }
.sendcard.none { background:#83520D; border-left-color:#F0B24F; }
.sendcard .k { text-transform:uppercase; font-size:.72rem; font-weight:700; }
.sendcard .v { font-size:1.35rem; font-weight:750; margin:3px 0 4px; overflow-wrap:anywhere; }
.sendcard .why { font-size:.9rem; color:#F1F5F2; }
.route { display:inline-flex; align-items:center; gap:7px; background:var(--white); border:1px solid var(--line);
     border-radius:6px; padding:5px 10px; font-weight:650; font-size:.86rem; margin:0 6px 8px 0; }
.route .dot { width:8px; height:8px; border-radius:50%; background:var(--saffron); }
.urgent { background:var(--red-soft); border:1px solid #E8B8B3; color:#792B26; border-radius:8px;
      padding:11px 14px; font-weight:650; margin-bottom:12px; }
.ccard { background:var(--white); border:1px solid var(--line); border-left:4px solid var(--green);
     border-radius:7px; padding:12px 14px; margin-bottom:9px; }
.ccard.unv { border-left-color:var(--amber); background:#FFFCF5; }
.ccard .top { display:flex; justify-content:space-between; gap:10px; align-items:flex-start; }
.ccard .val { font-weight:700; font-size:.98rem; overflow-wrap:anywhere; }
.badge { border-radius:4px; padding:3px 8px; font-size:.69rem; font-weight:750; white-space:nowrap; }
.badge.ok { background:#DDF0E5; color:#14573F; } .badge.no { background:#FCECCB; color:#75460A; }
.ccard .meta { color:var(--muted); font-size:.83rem; margin-top:4px; }
.ccard .warn { color:var(--amber); font-size:.8rem; margin-top:4px; }
.timeline { position:relative; margin:4px 0 0 14px; padding-left:26px; border-left:2px solid var(--line); }
.tstep { position:relative; margin-bottom:18px; }
.tstep .num { position:absolute; left:-41px; top:-2px; width:27px; height:27px; border-radius:50%;
          background:var(--ink); color:#fff; display:flex; align-items:center; justify-content:center;
          font-weight:700; font-size:.82rem; box-shadow:0 0 0 3px var(--paper); }
.tstep:first-child .num { background:var(--green); }
.tstep .ttl { font-weight:700; } .tstep .wait { color:var(--muted); font-size:.82rem; }
.tstep .how { font-size:.9rem; margin-top:3px; }
.paper { background:var(--white); border:1px solid var(--line); border-top:3px solid var(--green); border-radius:8px;
     padding:24px 26px; font-family:Georgia, 'Times New Roman', serif; font-size:1rem; line-height:1.65;
     white-space:pre-wrap; max-height:600px; overflow-y:auto; color:var(--ink); }
.law { background:var(--white); border:1px solid var(--line); border-radius:7px; padding:12px 14px; margin-bottom:9px; }
.law .tag { display:inline-block; background:var(--ink); color:#fff; border-radius:4px; padding:2px 7px;
        font-size:.75rem; font-weight:700; margin-right:8px; }
.law .cit { font-weight:700; } .law .ttl { color:var(--muted); font-size:.84rem; }
.sim { background:var(--white); border:1px solid var(--line); border-radius:7px; padding:10px 13px; margin-bottom:8px; }
.sim .kind { font-size:.7rem; font-weight:750; text-transform:uppercase; color:var(--green); }
.muted { color:var(--muted); font-size:.84rem; }
.tlog { background:#172A3A; color:#E8EFEB; border:1px solid #43545D; border-radius:7px; padding:12px 14px;
    font-family:Consolas, 'Courier New', monospace; font-size:.78rem; line-height:1.55; margin-bottom:10px;
    max-height:280px; overflow:auto; overflow-wrap:anywhere; }
div[data-testid="stForm"] { background:var(--white); border:1px solid var(--line); border-radius:9px;
    padding:20px 22px; box-shadow:0 8px 22px rgba(23,42,58,.055); }
div[data-testid="stForm"] label p { color:var(--ink); font-weight:650; }
div[data-testid="stTextArea"] textarea, div[data-testid="stTextInput"] input {
    background:#FBFCFB; border-color:#C9D5D0; border-radius:6px; }
div[data-testid="stTextArea"] textarea:focus, div[data-testid="stTextInput"] input:focus {
    border-color:var(--green); box-shadow:0 0 0 1px var(--green); }
div[data-testid="stFormSubmitButton"] button { min-height:46px; border-radius:6px; font-weight:750; }
.stTabs [data-baseweb="tab"] { font-weight:700; }
div[data-testid="stExpander"] { background:rgba(255,255,255,.7); border:1px solid var(--line); border-radius:7px; }
@keyframes enter { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:translateY(0); } }
@media (max-width:700px) {
  .block-container { padding:1rem .85rem 1.5rem; }
  .hero { grid-template-columns:1fr; gap:14px; padding:20px 18px; }
  .hero h1 { font-size:1.85rem; }
  .hero-side { border-left:0; border-top:1px solid #52636C; padding:12px 0 0; min-width:0; }
    .journey { gap:5px; }
    .journey-step { flex-direction:column; justify-content:center; gap:3px; min-height:58px; padding:6px 3px;
                                    text-align:center; font-size:.72rem; }
  .pipe { grid-template-columns:repeat(2, minmax(0, 1fr)); }
  div[data-testid="stForm"] { padding:15px 13px; }
}
</style>
""", unsafe_allow_html=True)

E = html.escape
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


def hero() -> None:
    st.markdown(f"""
<div class="hero">
    <div><div class="eyebrow" style="color:#F0B24F">Consumer support · India</div>
        <h1>Shikayat <span class="accent">Saathi</span></h1>
        <div class="hindi">आपकी शिकायत, सही जगह तक</div>
        <p>Get a clear complaint draft and a practical next step, in English, Hindi or Hinglish.</p>
    </div>
    <div class="hero-side"><strong>Your complaint stays yours</strong>
        <span>Review everything before you decide what to send.</span></div>
</div>""", unsafe_allow_html=True)


def journey_step(active: int) -> None:
    labels = ["Tell us", "Review", "Ready"]
    items = []
    for index, label in enumerate(labels, 1):
        status = "done" if index < active else "active" if index == active else ""
        marker = "✓" if index < active else str(index)
        items.append(f"<div class='journey-step {status}'><b>{marker}</b><span>{label}</span></div>")
    st.markdown(f"<div class='journey'>{''.join(items)}</div>", unsafe_allow_html=True)


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
        dates = step_dates(s)
        if dates:
            wait += f"<div class='wait'><b>📅 {E(dates)}</b></div>"
        src = s.get("source") or ""
        if src:  # every step is traceable to where it came from
            wait += ("<div class='wait'>🔗 source: " + (f"<a href='{E(src)}' target='_blank'>{E(src.split('//')[-1][:60])}"
                     " ↗</a>" if src.startswith("http") else E(src[:90])) + "</div>")
        items.append(f"<div class='tstep'><div class='num'>{i}</div><div class='ttl'>{E(s['name'])}</div>{wait}"
                     f"<div class='how'><b>{E(s['channel'])}</b> — {E(s['when_to_use'])}{link}{phone}</div></div>")
    return f"<div class='timeline'>{''.join(items)}</div>"


def mailto(to: str, subject: str, body: str) -> str:
    body = body if len(body) < 1800 else body[:1800] + "\n…"  # mail clients cap mailto length
    return f"mailto:{to}?" + urllib.parse.urlencode({"subject": subject, "body": body}, quote_via=urllib.parse.quote)


def header_chips(s: dict) -> None:
    from saathi.playbooks import SECTORS

    f = s.get("facts") or {}
    sector = SECTORS.get(s.get("sector"))
    chips = [f"<span class='route'>🗣️ {E(s.get('language', ''))}</span>"]
    if sector:
        chips[:0] = [f"<span class='route'><span class='dot'></span>{E(sector['label'])}</span>",
                     f"<span class='route'>⚖️ {E(sector['regulator'])}</span>"]
    else:
        st.caption("Complaint category not available")
    if s.get("company"):
        chips.append(f"<span class='route'>🏢 {E(s['company'])}"
                     + (f" · {E(s['company_domain'])}" if s.get("company_domain") else "") + "</span>")
    if f.get("amount"):
        chips.append(f"<span class='route'>₹ {E(f['amount'])}</span>")
    st.markdown("".join(chips), unsafe_allow_html=True)
    if s.get("urgent_note"):
        st.markdown(f"<div class='urgent'>🚨 {E(s['urgent_note'])}</div>", unsafe_allow_html=True)
    if s.get("errors"):
        st.info("Some details could not be confirmed. Check the official source before sending your complaint.")


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


def resume_or_fail(graph_module, edits: dict) -> None:
    """Continue the paused graph; if the AI is unavailable, stop and say so (no fallback letter)."""
    try:
        st.session_state["state"] = graph_module.resume(st.session_state["thread"], edits)
    except AIUnavailable as e:
        st.session_state["workflow_failed"] = f"ai:{e}"
        st.session_state.pop("state", None)


def safety_banner(safety: dict) -> None:
    """Emergency numbers first, whenever the safety router saw immediate danger."""
    if (safety or {}).get("level") != "emergency":
        return
    msgs = " ".join(safety.get("messages") or [])
    nums = "".join(f"<li>{E(h)}</li>" for h in safety.get("helplines") or [])
    st.markdown(f"<div class='urgent'>🆘 <b>If anyone is in danger right now, call for help first.</b> {E(msgs)}"
                f"<ul style='margin:6px 0 0'>{nums}</ul></div>", unsafe_allow_html=True)


def brief_expander(brief: dict) -> None:
    if not brief:
        return
    with st.expander(f"What we found about {brief.get('subject') or 'your case'}", expanded=False):
        if brief.get("what_it_is"):
            st.write(brief["what_it_is"])
        if brief.get("administering_body"):
            st.caption(f"Run by {brief['administering_body']}")
        if brief.get("entitlement"):
            st.markdown("**Potential benefit**")
            st.write(brief["entitlement"])
        if brief.get("documents_needed"):
            st.markdown("**Documents that may help**")
            st.write(" · ".join(brief["documents_needed"]))
        for index, url in enumerate(brief.get("sources") or [], 1):
            if url.startswith("https://"):
                st.markdown(f"[Source {index}]({E(url)})")


def eligibility_box(payload: dict, *, show_checks: bool) -> None:
    elig = payload.get("eligibility") or {}
    if elig.get("message"):
        show = {"meets_listed": st.success, "may_not_qualify": st.warning}.get(elig.get("status"), st.info)
        show(("**You may not qualify yet.** " if elig.get("status") == "may_not_qualify" else "") + elig["message"])
    if show_checks:
        for check in payload.get("eligibility_checks") or []:
            if check.get("status") != "unknown":
                icon = {"met": "✅", "not_met": "⚠️"}.get(check["status"], "❔")
                st.markdown(f"{icon} {E(check['condition'])}" + (f" — _{E(check['evidence'])}_"
                                                                  if check.get("evidence") else ""))


@st.dialog("One quick question", width="large")
def clarify_dialog(s: dict, graph_module) -> None:
    """Very unclear complaint: one question, then we continue."""
    payload = s.get("__interrupt__") or {}
    safety_banner(payload.get("safety") or {})
    st.markdown('<div class="eyebrow">Step 2 of 3</div><h2 class="section-title">One quick question</h2>',
                unsafe_allow_html=True)
    with st.form("clarify_form"):
        answer = st.text_area(payload.get("question") or "What is the complaint about, and against whom?", height=90)
        go = st.form_submit_button("Continue", type="primary", width="stretch")
    if go:
        with st.spinner("Finding the right complaint route…"):
            resume_or_fail(graph_module, {"answer": answer})
        st.rerun()


@st.dialog("A few details first", width="large")
def details_dialog(s: dict, graph_module) -> None:
    """Step 2a: every missing fact and question in ONE form, before the letter is written."""
    payload = s.get("__interrupt__") or {}
    safety_banner(payload.get("safety") or {})
    st.markdown('<div class="eyebrow">Step 2 of 3</div><h2 class="section-title">A few details first</h2>',
                unsafe_allow_html=True)
    st.caption("Answer what you know in one go - we'll write the letter with your answers. Leave anything blank "
               "if you're unsure.")
    brief_expander(payload.get("case_brief") or {})
    eligibility_box(payload, show_checks=True)
    me0 = payload.get("complainant") or s.get("complainant") or {}
    with st.form("details_form"):
        fills, answers = {}, {}
        if payload.get("missing_facts"):
            st.markdown("**Details for the letter**")
            cols = st.columns(min(3, len(payload["missing_facts"])))
            for i, m in enumerate(payload["missing_facts"]):
                fills[m["placeholder"]] = cols[i % len(cols)].text_input(
                    m["fact"].replace("_", " ").title(), "", placeholder=m["placeholder"])
        conds = [q for q in payload.get("questions") or [] if q.get("condition")]
        extra = [q for q in payload.get("questions") or [] if not q.get("condition")]
        if conds:
            st.markdown("**Do you meet the published conditions?**")
            st.caption("Unknown does not mean ineligible - answer the ones you can.")
            for i, q in enumerate(conds):
                answers[q["key"]] = st.text_input(q["question"], key=f"details_cond_{i}",
                                                  placeholder="Your answer, or leave blank if unsure")
        if extra:
            st.markdown("**A few more details make the letter stronger**")
            for i, q in enumerate(extra):
                answers[q["key"]] = st.text_input(q["question"], key=f"details_extra_{i}", placeholder="Optional")
        langs = {"en": "English", "hi": "हिन्दी (Hindi)"}
        letter_lang = st.radio("Letter language - the whole letter is written in one language", list(langs),
                               index=list(langs).index(payload.get("letter_language") or "en"),
                               format_func=langs.get, horizontal=True)
        st.markdown("**Your details for the signature** (optional)")
        c1, c2, c3 = st.columns(3)
        me = {"name": c1.text_input("Your name", me0.get("name", "")),
              "phone": c2.text_input("Phone", me0.get("phone", "")),
              "email": c3.text_input("Email", me0.get("email", ""))}
        go = st.form_submit_button("Write my letter", type="primary", width="stretch")
    if go:
        edits = {"fills": {k: v for k, v in fills.items() if v.strip()},
                 "case_answers": {k: v for k, v in answers.items() if v.strip()},
                 "complainant": {k: v for k, v in me.items() if v.strip()}, "letter_language": letter_lang}
        with st.spinner("Writing your letter with your answers…"):
            resume_or_fail(graph_module, edits)
        st.rerun()
    if st.button("Back to complaint details", key="details_back"):
        st.session_state.pop("state", None)
        st.session_state.pop("thread", None)
        st.rerun()


@st.dialog("Review your complaint", width="large")
def review_dialog(s: dict, graph_module) -> None:
    """Step 2b: approve or edit the letter. All questions were asked before drafting."""
    payload = s.get("__interrupt__") or {}
    safety_banner(payload.get("safety") or {})
    st.markdown('<div class="eyebrow">Step 2 of 3</div><h2 class="section-title">Make it yours</h2>',
                unsafe_allow_html=True)
    st.caption("Check the letter and edit anything. Nothing is sent when you approve.")
    eligibility_box(payload, show_checks=False)
    dl = payload.get("deadline") or {}
    if dl.get("date"):
        st.info(f"📅 Reply due by **{dl['date']}** — {dl['basis']}"
                + (f" ({dl['source']})" if dl.get("source") else ""))
    rec = s.get("recommended")
    if rec:
        st.success(f"Suggested recipient: **{rec['value']}**"
                   + (f" · {rec['name']}" if rec.get("name") else "")
                   + f"\n\nVerified: {rec['reason']}", icon="✅")
    elif payload.get("tier", "A") == "A":
        st.warning("We couldn't verify a complaint email. Your next steps will point you to the official complaint "
                   "channel.", icon="🛡️")
    with st.form("review_form"):
        subject = st.text_input("Subject", payload.get("subject") or "")
        label = {"B": "Police complaint", "C": "Your explainer"}.get(payload.get("tier"), "Complaint letter")
        draft = st.text_area(label, payload.get("draft") or "", height=320)
        c1, c2, c3 = st.columns(3)
        me0 = s.get("complainant") or {}
        me = {"name": c1.text_input("Your name", me0.get("name", "")),
              "phone": c2.text_input("Phone (optional)", me0.get("phone", "")),
              "email": c3.text_input("Email (optional)", me0.get("email", ""))}
        approved = st.form_submit_button("Approve & finish", type="primary", width="stretch",
                                         help="Nothing is sent - you get the final letter to send yourself.")
    if approved:
        edits = {"draft": draft, "subject": subject, "complainant": {k: v for k, v in me.items() if v}}
        with st.spinner("Preparing your final complaint…"):
            resume_or_fail(graph_module, edits)
        st.rerun()
    if st.button("Back to complaint details", key="review_back"):
        st.session_state.pop("state", None)
        st.session_state.pop("thread", None)
        st.rerun()


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
            st.caption("Review the letter before sending. Nothing has been sent.")
    with t2:
        contacts_section(s)
    with t3:
        st.markdown(ladder_html(s.get("escalation") or []), unsafe_allow_html=True)
    with t4:
        for c in f.get("cited") or s.get("clauses") or []:
            st.markdown(f"<div class='law'><span class='tag'>{E(c['tag'])}</span><span class='cit'>{E(c['act'])}, "
                        f"{E(c['section'])}</span><div class='ttl'>{E(c['title'])}</div></div>",
                        unsafe_allow_html=True)
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


# --------------------------------------------------------------------------- pages

nav_brand, nav_links = st.columns([1, 1], vertical_alignment="center")
nav_brand.markdown('<div class="navbrand">📣 Shikayat Saathi</div>', unsafe_allow_html=True)
page = nav_links.radio("Your space", ["New complaint", "Past complaints"], horizontal=True,
                       label_visibility="collapsed", key="consumer_page")

if page == "New complaint":
    from saathi import graph as G

    hero()
    s = st.session_state.get("state")
    if s and s.get("final"):
        journey_step(3)
        safety_banner(s.get("safety") or {})
        st.markdown('<div class="eyebrow">Step 3 of 3</div><h2 class="section-title">Your complaint is ready</h2>',
                    unsafe_allow_html=True)
        header_chips(s)
        final_view(s)
        if st.button("Start another complaint", type="primary", width="stretch"):
            for key in ("state", "thread", "workflow_failed", "complaint_input", "company_input", "city_input",
                        "name_input", "example", "_loaded_example"):
                st.session_state.pop(key, None)
            st.rerun()
    elif s and "__interrupt__" in s:
        journey_step(2)
        safety_banner(s.get("safety") or {})
        form_stage = (s["__interrupt__"] or {}).get("stage") == "details"
        st.markdown('<div class="eyebrow">Step 2 of 3</div><h2 class="section-title">'
                    + ("A few details first" if form_stage else "Your draft is ready") + "</h2>",
                    unsafe_allow_html=True)
        if (s["__interrupt__"] or {}).get("stage") == "clarify":
            st.caption("We need one detail to find the right authority.")
            clarify_dialog(s, G)
        elif form_stage:
            st.caption("Answer a few questions so the letter can be written in one go.")
            details_dialog(s, G)
        else:
            st.caption("Review the proposed recipient and letter to continue.")
            review_dialog(s, G)
    elif st.session_state.get("workflow_failed"):
        journey_step(1)
        failed = st.session_state.get("workflow_failed")
        if isinstance(failed, str) and failed.startswith("ai:"):
            st.error(f"The AI service is unavailable right now ({failed[3:]}). Nothing was drafted or sent - "
                     "please try again in a minute.")
        else:
            st.error("We couldn't prepare your complaint this time. Your information has not been sent.")
        if st.button("Edit details and try again", type="primary"):
            st.session_state.pop("workflow_failed", None)
            st.rerun()
    elif s:
        journey_step(1)
        st.error("We couldn't prepare a reviewable draft. Your information has not been sent.")
        if st.button("Start again", type="primary"):
            st.session_state.pop("state", None)
            st.session_state.pop("thread", None)
            st.rerun()
    else:
        journey_step(1)
        st.markdown('<div class="eyebrow">New complaint</div><h2 class="section-title">Tell us what happened</h2>',
                    unsafe_allow_html=True)
        pick = st.pills("Start with an example", list(EXAMPLES), key="example")
        if pick and pick != st.session_state.get("_loaded_example"):
            example_text, example_company, example_city = EXAMPLES[pick]
            st.session_state["complaint_input"] = example_text
            st.session_state["company_input"] = example_company
            st.session_state["city_input"] = example_city
            st.session_state["_loaded_example"] = pick
        with st.form("complaint_form"):
            complaint = st.text_area("What happened?", key="complaint_input", height=130,
                                     placeholder="For example: My online order arrived damaged and the seller refused a refund.")
            with st.expander("Add details to improve your letter"):
                st.caption("These details are optional. Add only what you’re comfortable sharing.")
                c1, c2, c3 = st.columns(3)
                company = c1.text_input("Company or app", key="company_input", placeholder="Optional")
                city = c2.text_input("Your city", key="city_input", placeholder="For local complaint options")
                name = c3.text_input("Your name", key="name_input", placeholder="Optional")
            st.caption("We’ll prepare a draft for your review. Nothing is sent automatically.")
            go = st.form_submit_button("Find my complaint route", type="primary", width="stretch")

        if go:
            if not complaint.strip():
                st.error("Describe what happened so we can prepare your complaint.")
            else:
                live = bool(SERPAPI_API_KEY)
                serp.offline = not live
                thread = G.new_thread()
                try:
                    with st.spinner("Finding the right complaint route and preparing your draft…"):
                        state = G.run(G.initial_state(complaint, company=company, city=city,
                                                      complainant={"name": name} if name else None, live=live),
                                      thread)
                    if "__interrupt__" not in state and not state.get("final"):
                        raise RuntimeError("workflow ended before review")
                    st.session_state.update(thread=thread, state=state)
                    st.rerun()
                except AIUnavailable as e:  # no silent fallback letter: say so plainly
                    st.session_state["workflow_failed"] = f"ai:{e}"
                    st.rerun()
                except Exception:
                    LOGGER.exception("Complaint workflow failed before review")
                    st.session_state["workflow_failed"] = True
                    st.rerun()

elif page == "Past complaints":
    st.markdown('<div class="eyebrow">Your space</div><h2 class="section-title">Past complaints</h2>',
                unsafe_allow_html=True)
    files = sorted(CASES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        st.info("Your completed complaints will appear here.")
    else:
        cases = [json.loads(p.read_text(encoding="utf-8")) for p in files[:50]]
        i = st.selectbox("Case", range(len(cases)), format_func=lambda i: f"{cases[i].get('company') or '?'} · "
                         f"{cases[i]['complaint'][:80]}")
        c = cases[i]
        header_chips(c)
        final_view(c)

st.markdown("<div class='muted' style='text-align:center;margin-top:28px'>Information, not legal advice.</div>",
            unsafe_allow_html=True)
