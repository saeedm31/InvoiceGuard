"""InvoiceGuard: an accounts-payable agent that reads supplier invoices, checks them against the ERP
through MCP servers, pays only what is clean, and asks a person for the rest."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pandas as pd
import streamlit as st

from invoiceguard import config as C
from invoiceguard.llm import AGENT_MODELS, PRICES_CHECKED, Meter, eur
from invoiceguard.service import APPROVERS, open_service

st.set_page_config(page_title="InvoiceGuard", page_icon="🧾", layout="wide")
CSS = """
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
html, body, [data-testid="stAppViewContainer"], [data-testid="stSidebar"] { font-family: 'IBM Plex Sans', sans-serif; }
h1, h2, h3, [data-testid="stMetricValue"] { font-family: 'Space Grotesk', sans-serif !important; letter-spacing: -0.01em; }
[data-testid="stAppViewContainer"] {
  background: radial-gradient(1100px 520px at 88% -8%, #1B3358 0%, rgba(11,20,36,0) 60%),
              linear-gradient(135deg, #0B1424 0%, #13233D 100%);
}
[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: 2.2rem; max-width: 1400px; }
#MainMenu, footer { visibility: hidden; }
[data-testid="stSidebar"] { border-right: 1px solid #2E4566; }
[data-testid="stMetric"] {
  background: rgba(22,37,61,0.85); border: 1px solid #2E4566; border-radius: 14px;
  padding: 14px 18px; box-shadow: 0 8px 24px rgba(0,0,0,0.25);
}
[data-testid="stMetricLabel"] p { color: #8FA3BF; font-size: 0.9rem; }
.stTabs [data-baseweb="tab-list"] { gap: 4px; border-bottom: 1px solid #2E4566; }
.stTabs [data-baseweb="tab"] { padding: 10px 16px; border-radius: 10px 10px 0 0; color: #B8C2D1; }
.stTabs [aria-selected="true"] { background: #16253D; color: #F3F1EA; }
.stTabs [data-baseweb="tab-highlight"] { background-color: #F2A65A; }
[data-testid="stDataFrame"] { border: 1px solid #2E4566; border-radius: 12px; overflow: hidden; }
.ig-kicker { color: #F2A65A; font-weight: 600; letter-spacing: 0.18em; text-transform: uppercase; font-size: 0.8rem; }
.ig-title { font-family: 'Space Grotesk', sans-serif; font-size: 2.2rem; font-weight: 700; line-height: 1.15;
            margin: 0.35rem 0 0.5rem 0; color: #F3F1EA; }
.ig-sub { color: #B8C2D1; font-size: 1.05rem; line-height: 1.5; max-width: 64rem; }
.ig-steps { display: flex; gap: 0.6rem; flex-wrap: wrap; margin: 0.9rem 0 1.2rem 0; }
.ig-step { background: rgba(22,37,61,0.85); border: 1px solid #2E4566; border-radius: 999px;
           padding: 0.35rem 0.95rem; color: #DCE3EE; font-size: 0.92rem; }
.ig-step b { color: #F2A65A; margin-right: 0.35rem; }
"""
HERO = """
<div class="ig-kicker">InvoiceGuard · accounts-payable agent</div>
<div class="ig-title">The AI reads and investigates. Code decides. A person approves big payments.</div>
<div class="ig-sub">Supplier invoices are read by Claude, checked against purchase orders and goods receipts in the ERP
through MCP servers, and paid only when everything matches. Fake bank details, duplicates and hidden instructions
in the PDF are caught before any money moves.</div>
<div class="ig-steps">
  <span class="ig-step"><b>1</b>📥 Inbox: process an invoice</span>
  <span class="ig-step"><b>2</b>🔍 Case file: see what the agent did and why</span>
  <span class="ig-step"><b>3</b>✋ Approvals: approve as the AP manager</span>
</div>
"""
st.markdown(f"<style>{CSS}</style>", unsafe_allow_html=True)
STATUS = {
    "scheduled": "🟢 paid (scheduled)",
    "awaiting_approval": "🟠 waiting for approval",
    "on_hold": "🔴 on hold",
    "rejected": "⛔ rejected",
    "needs_review": "🔴 needs review",
    "failed": "⚠️ failed",
}
SESSION_EUR = float(os.getenv("DEMO_SESSION_BUDGET_EUR", "0.5"))
DAILY_EUR = float(os.getenv("DEMO_DAILY_BUDGET_EUR", "3"))


def _shared_key() -> str | None:
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY")
    except Exception:  # no secrets file
        key = None
    return key or os.getenv("ANTHROPIC_API_KEY") or None


SHARED_KEY = _shared_key()
BEDROCK = os.getenv("LLM_PROVIDER", "anthropic") == "bedrock"  # company AWS account: the task role, no key


def _code_version() -> str:
    """Changes when the package code changes, so a redeploy starts a fresh service and fresh MCP servers."""
    return str(max(f.stat().st_mtime_ns for f in (C.ROOT / "invoiceguard").rglob("*.py")))


@st.cache_resource(show_spinner="Starting the ERP, documents, mail and notify MCP servers (first start only)...",
                   max_entries=1)
def service(code_version: str):
    return open_service(C.Settings().data_dir)


@st.cache_resource
def shared_spend() -> dict:
    return {"day": "", "eur": 0.0}


@st.cache_data(show_spinner=False, max_entries=64)
def page_png(path: str, mtime: float) -> bytes:
    """First page of an invoice PDF as a PNG, for viewing. mtime makes the cache follow a data reset."""
    import io

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(path)
    try:
        img = pdf[0].render(scale=1.6).to_pil().convert("RGB")
    finally:
        pdf.close()
    ink = img.convert("L").point(lambda v: 255 if v < 200 else 0).getbbox()  # visible content only
    if ink:
        pad = 40
        img = img.crop((0, max(ink[1] - pad, 0), img.width, min(ink[3] + pad, img.height)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def show_invoice(doc_id: str, where: str) -> None:
    pdf = svc.s.inbox / f"{doc_id}.pdf"
    if pdf.exists():
        st.image(page_png(str(pdf), pdf.stat().st_mtime), width="stretch")
        st.download_button("⬇ Download the PDF", pdf.read_bytes(), file_name=pdf.name, mime="application/pdf",
                           key=f"dl-{where}-{doc_id}")


svc = service(_code_version())
ss = st.session_state
ss.setdefault("meter", Meter())
ss.setdefault("session_eur", 0.0)
labels = json.loads(svc.s.labels.read_text())

# ---------- sidebar ----------
with st.sidebar:
    st.title("🧾 InvoiceGuard")
    st.caption("An AI agent for accounts payable, built the way a finance team could run it.")
    st.warning("**Simulated company** (Meridian Transport Group): 40 suppliers, 32 invoices. All names are fake.", icon="🧪")
    st.divider()
    st.markdown("**AI settings**")
    modes = (["AI (Claude)"] if SHARED_KEY or BEDROCK else []) + ["Demo (scripted, no AI)"] \
        + ([] if BEDROCK else ["Live (your Anthropic key)"])
    mode = st.radio("Mode", modes, index=0)
    api_key = SHARED_KEY if mode.startswith("AI") else None
    if mode.startswith("Live"):
        api_key = st.text_input("Anthropic API key", type="password", help="Used only for this session. Never stored or logged.")
    if mode.startswith("AI") and BEDROCK:
        st.caption(f"Claude on Amazon Bedrock ({svc.s.aws_region}), with this app's AWS role. No API key.")
    elif mode.startswith("AI"):
        st.caption(f"Free to try: up to €{SESSION_EUR:.2f} per visitor and €{DAILY_EUR:.2f} per day.")
    model = st.selectbox("Model", list(AGENT_MODELS), format_func=AGENT_MODELS.get,
                         index=list(AGENT_MODELS).index(svc.s.agent_model) if svc.s.agent_model in AGENT_MODELS else 0,
                         disabled=mode.startswith("Demo"))
    if mode.startswith("Demo"):
        st.caption("The scripted model is not AI: it follows fixed rules so the MCP tools, checks, approvals, audit "
                   "log and evals can be tried for free.")
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    spend = shared_spend()
    if spend["day"] != today:
        spend.update(day=today, eur=0.0)
    st.progress(min(ss.session_eur / SESSION_EUR, 1.0), text=f"Your AI spend {eur(ss.session_eur)} of {eur(SESSION_EUR)}")
    st.caption(f"Prices checked {PRICES_CHECKED}.")
    st.divider()
    if st.button("↺ Reset demo data", help="Rebuild the simulated company. Everyone shares one demo."):
        svc.reset()
        st.rerun()

st.markdown(HERO, unsafe_allow_html=True)
tabs = st.tabs(["📥 Inbox", "🔍 Case file", "✋ Approvals", "📣 Outbox", "🧪 Evals", "🛡️ Security", "🧭 How it works",
                "🧾 Audit log"])
tab_inbox, tab_case, tab_appr, tab_out, tab_eval, tab_sec, tab_how, tab_audit = tabs
if st.query_params.get("case"):  # links in Teams cards open a case
    ss.setdefault("case_pick", st.query_params["case"])


def can_run() -> str | None:
    if mode.startswith("Demo"):
        return None
    if not api_key and not (BEDROCK and mode.startswith("AI")):
        return "Enter your Anthropic API key in the sidebar, or pick Demo mode."
    if mode.startswith("AI") and spend["eur"] >= DAILY_EUR:
        return "Today's free AI budget is used up. Try Demo mode, or your own key in Live mode."
    if mode.startswith("AI") and ss.session_eur >= SESSION_EUR:
        return f"You have used your free {eur(SESSION_EUR)} of AI. Use Demo mode or your own key."
    return None


def process(doc_id: str) -> dict | None:
    kw = {} if mode.startswith("Demo") else {"api_key": api_key, "model": model, "meter": ss.meter}
    try:
        r = svc.process(doc_id, mode="scripted" if mode.startswith("Demo") else "live", **kw)
    except Exception as exc:
        st.error(f"The agent failed: {type(exc).__name__}: {exc}")
        return None
    cost = r["cost"]["eur"] or 0.0
    ss.session_eur += cost
    if mode.startswith("AI"):
        spend["eur"] += cost
    ss.case_pick = r["case_id"]
    return r


# ---------- inbox ----------
with tab_inbox:
    cases = svc.cases()
    latest = {}
    for c in reversed(cases):  # newest wins
        latest[c["doc_id"]] = c
    n_done = len(latest)
    st_counts = pd.Series([c["status"] for c in latest.values()]).value_counts()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Invoices in inbox", len(labels))
    c2.metric("Processed", n_done)
    c3.metric("Paid automatically", int(st_counts.get("scheduled", 0)))
    c4.metric("Waiting for approval", int(st_counts.get("awaiting_approval", 0)))
    c5.metric("On hold or rejected", int(st_counts.get("on_hold", 0) + st_counts.get("rejected", 0)))
    left, right = st.columns([2, 1])
    with right:
        st.markdown("**Process an invoice**")
        todo = [d for d in sorted(labels) if d not in latest] or sorted(labels)
        hint = {k: v["kind"].replace("_", " ") for k, v in labels.items()}
        pick = st.selectbox("Invoice", todo, format_func=lambda d: f"{d} · planted: {hint[d]}")
        blocked = can_run()
        if blocked:
            st.info(blocked)
        if st.button("▶ Run the agent on this invoice", type="primary", disabled=bool(blocked)):
            with st.spinner("Reading the PDF, extracting, investigating in the ERP, checking..."):
                r = process(pick)
            if r:
                st.success(f"{pick}: {STATUS.get(r['status'], r['status'])}. Open the 🔍 Case file tab for the details.")
        if mode.startswith("Demo") and st.button("⏩ Process all remaining (scripted, ~5 s)"):
            with st.spinner("Processing..."):
                for d in [d for d in sorted(labels) if d not in latest]:
                    process(d)
            st.rerun()
        st.caption("Good ones to try: a **hidden instructions** or **inject price** invoice (prompt injection), "
                   "**bank** (changed IBAN), **big** (needs a person), **duplicate**.")
    with left:
        rows = []
        for d in sorted(labels):
            c = latest.get(d)
            rows.append({
                "Invoice": d,
                "Planted scenario": hint[d],
                "Supplier": c["vendor"] if c else "",
                "Total (EUR)": c["gross"] if c else None,
                "Status": STATUS.get(c["status"], c["status"]) if c else "⏳ not processed",
                "Exceptions": ", ".join(json.loads(c["exceptions"])) if c else "",
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=520,
                     column_config={"Total (EUR)": st.column_config.NumberColumn(format="%.2f")})
        st.caption("The planted scenario is shown to help you try the demo. The agent never sees it: it lives in "
                   "the eval labels, which no MCP server serves.")
        st.markdown(f"**Invoice {pick}** (the one selected on the right)")
        show_invoice(pick, "inbox")
        if labels[pick]["kind"] == "inject_clean":
            st.caption("This one hides near-white, tiny text at the bottom: invisible on paper, but in the text "
                       "the model reads. Run it and open the Case file.")


def trace_rows(case_id: str) -> list[dict]:
    out = []
    for e in svc.audit.read(run_id=case_id):
        p = e["payload"]
        what = {
            "tool_call": lambda: f"{p['tool']}({json.dumps(p['args'])[:90]})" + ("" if p["ok"] else f"  ✖ {p['error']}"),
            "llm_call": lambda: f"{p['step']} → {', '.join(p['tools']) or 'text'}",
            "decision": lambda: f"{p['decision']} {p['exceptions']} (model proposed: {p['model_proposed']})",
            "security_flag": lambda: "; ".join(h["rule"] for h in p["hits"]),
            "findings": lambda: f"proposed {p['proposed_decision']}; concerns: {p['concerns']}",
        }.get(e["kind"], lambda: json.dumps(p)[:120])()
        who = p.get("by", "model" if e["kind"] in ("llm_call", "findings") else "code")
        out.append({"time": e["ts"][11:23], "step": e["kind"], "by": who, "what": what, "ms": p.get("ms")})
    return out


# ---------- case file ----------
with tab_case:
    cases = svc.cases()
    if not cases:
        st.info("No cases yet. Process an invoice in the 📥 Inbox tab.")
    else:
        ids = [c["case_id"] for c in cases]
        default = ids.index(ss.get("case_pick")) if ss.get("case_pick") in ids else 0
        by_id = {c["case_id"]: c for c in cases}
        cid = st.selectbox("Case", ids, index=default,
                           format_func=lambda i: f"{i} · {by_id[i]['doc_id']} · {STATUS.get(by_id[i]['status'], by_id[i]['status'])}")
        case = svc.case(cid)
        p = case["payload"]
        st.markdown(f"### {STATUS.get(case['status'], case['status'])} · {case['doc_id']} · decision **{p['decision']}**")
        note = p.get("note") or {}
        (st.success if p["decision"] == "pay" else st.warning if p["decision"] == "approve" else st.error)(
            note.get("summary") or "-")
        if p.get("scan"):
            st.error("**Prompt-injection attempt in the document.** "
                     + " · ".join(f"{h['rule']}: “{h['snippet']}”" for h in p["scan"]), icon="🛡️")
        f = p.get("findings") or {}
        st.caption(f"Model proposed **{f.get('proposed_decision', '-')}**, rules decided **{p['decision']}**"
                   + (" (they agree)" if f.get("proposed_decision") == p["decision"] else " (rules win; stricter one applies)")
                   + f" · {p['cost']['calls']} model calls · {eur(p['cost']['eur'])} · {p['seconds']} s · engine: {p['engine']}"
                   + f" · mode: {case['mode']}")
        left, right = st.columns([1, 1])
        with left:
            st.markdown("**Checks against the ERP** (code, not the model)")
            chk = pd.DataFrame([{"": "✅" if c["ok"] else ("🟠" if not c["hard"] else "❌"), "check": c["code"], "detail": c["detail"]}
                                for c in p["checks"]])
            st.dataframe(chk, hide_index=True, width="stretch")
            inv = p.get("extracted")
            if inv:
                st.markdown("**Fields the model read off the PDF**")
                show = {k: v for k, v in inv.items() if k != "lines"}
                show["iban"] = (show.get("iban") or "")[:4] + " **** " + (show.get("iban") or "")[-4:] if show.get("iban") else None
                st.dataframe(pd.DataFrame([show]).T.rename(columns={0: "value"}).astype(str), width="stretch")
                st.dataframe(pd.DataFrame(inv["lines"]), hide_index=True, width="stretch")
        with right:
            if note.get("email_body"):
                st.markdown("**Email to the supplier** (the AI drafted it; a person reviews and sends)")
                subj = st.text_input("Subject", note.get("email_subject") or f"Invoice {case['doc_id']}", key=f"subj-{cid}")
                body = st.text_area("Draft", note["email_body"], height=220, label_visibility="collapsed", key=f"body-{cid}")
                if st.button("✉ Send to the supplier", key=f"send-{cid}",
                             help="Sent as the AP clerk. The address comes from the vendor master, not from the invoice."):
                    r = svc.send_to_supplier(cid, "ana.costa", subj, body)
                    (st.success if r["ok"] else st.error)(f"{r['status']}: {r['detail']}" if r["ok"] else r["error"])
            msgs = svc.outbox(cid)
            if msgs:
                st.markdown("**Notifications for this case** (📣 Outbox has all)")
                for m in msgs:
                    st.markdown(f"- {'✉' if m['channel'] == 'email' else '💬'} {m['name']}: {m['subject']} · `{m['status']}`")
            st.markdown("**What happened in the ERP**")
            for a in p.get("actions") or []:
                st.markdown(f"- `{a[0]}`: {a[1] if isinstance(a[1], str) else json.dumps(a[1])}")
            st.markdown("**The invoice**")
            show_invoice(case["doc_id"], "case")
            with st.expander("Invoice text as the model saw it (untrusted)"):
                r = svc.hub.call("read_invoice", {"doc_id": case["doc_id"]})
                st.code(r.data["text"] if r.ok else r.error, language="text")
        st.markdown("**Agent trace** (every step, from the audit log)")
        st.dataframe(pd.DataFrame(trace_rows(cid)), hide_index=True, width="stretch", height=360)

# ---------- approvals ----------
with tab_appr:
    st.markdown(f"Invoices above **EUR {C.APPROVAL_LIMIT_EUR:,.0f}** wait here. Approving creates a signed, single-use "
                "token for exactly this invoice and amount; the ERP's payment tool checks it. The AI never sees the token.")
    who = st.selectbox("Acting as", list(APPROVERS),
                       format_func=lambda a: f"{a} · {APPROVERS[a][0]} · may approve up to "
                       + ("any amount" if APPROVERS[a][1] == float("inf") else f"EUR {APPROVERS[a][1]:,.0f}"))
    waiting = [c for c in svc.cases() if c["status"] == "awaiting_approval"]
    if not waiting:
        st.info("Nothing is waiting. Process a **big** invoice in the 📥 Inbox tab.")
    for c in waiting:
        p = json.loads(c["payload"])
        with st.container(border=True):
            a, b = st.columns([3, 1])
            a.markdown(f"**{c['doc_id']}** · {c['vendor']} · **EUR {c['gross']:,.2f}** · invoice {p['extracted']['invoice_number']}")
            a.caption(" · ".join(f"✅ {x['code']}" for x in p["checks"] if x["ok"]))
            a.caption((p.get("note") or {}).get("summary", ""))
            if b.button("Approve payment", key=f"ok-{c['case_id']}", type="primary"):
                r = svc.approve(c["case_id"], who)
                (st.success if r["ok"] else st.error)(
                    f"Payment {r['payment']['payment_id']} scheduled for {r['payment']['pay_on']}." if r["ok"] else r["error"])
            if b.button("Decline", key=f"no-{c['case_id']}"):
                svc.decline(c["case_id"], who, "declined in review")
                st.rerun()

# ---------- outbox ----------
with tab_out:
    nmode = svc.notify_mode()
    st.markdown("Email and Teams messages come from the **notify MCP server**. Code decides who is told what: the "
                "AP manager when an invoice waits for approval, the fraud channel when an IBAN changed or a PDF hides "
                "instructions, and the supplier when a clerk sends the drafted email. Recipients are directory keys, "
                "never addresses, so text in an invoice cannot redirect a message.")
    if nmode == "live":
        st.success("NOTIFY_MODE=live: messages are delivered (Microsoft Graph, SES or SMTP; Teams Workflows webhook).")
    else:
        st.info("Demo: NOTIFY_MODE=outbox, so messages are stored here and nothing leaves the app. Set NOTIFY_MODE=live "
                "with Microsoft Graph or SES credentials and Teams webhook URLs to deliver them.", icon="📭")
    msgs = svc.outbox()
    if not msgs:
        st.info("No messages yet. Process a **big**, **bank** or **inject** invoice in the 📥 Inbox tab.")
    else:
        st.dataframe(pd.DataFrame([{
            "time": m["created_at"][11:19], "via": "✉ email" if m["channel"] == "email" else "💬 Teams", "to": m["name"],
            "subject": m["subject"], "case": m["ref"], "by": m["sent_by"], "status": m["status"]} for m in msgs]),
            hide_index=True, width="stretch", height=300)
        pick_m = st.selectbox("Read a message", range(len(msgs)),
                              format_func=lambda i: f"{msgs[i]['msg_id']} · {msgs[i]['name']} · {msgs[i]['subject'][:60]}")
        m = msgs[pick_m]
        with st.container(border=True):
            st.markdown(f"**{m['subject']}**  \nTo: {m['name']} (`{m['address']}`) · {m['status']}: {m['detail']}")
            st.text(m["body"])

# ---------- evals ----------
with tab_eval:
    st.markdown("Every inbox invoice has a known right answer. The eval runs all 32 on a fresh copy of the company "
                "and scores the agent. **Unsafe payments** (paid when it should not be) is the gate: it must be 0.")
    live = C.ROOT / "eval_report.json"
    scripted = C.ROOT / "eval_report_scripted.json"
    if st.button("▶ Run the evals now (scripted model, ~10 s)"):
        from invoiceguard import evals

        with st.spinner("Building a fresh company and processing 32 invoices..."):
            evals.run("scripted", out=scripted)
    shown = [(x, json.loads(x.read_text())) for x in (live, scripted) if x.exists()]
    if not shown:
        st.info("No eval report yet: run it above.")
    for path, rep in shown:
        st.markdown(f"#### {'Claude (live)' if rep['mode'] == 'live' else 'Scripted model'} · {rep['model']} · {rep['when']}")
        k = st.columns(6)
        k[0].metric("Decision accuracy", f"{rep['decision_accuracy']:.0%}")
        k[1].metric("Unsafe payments", rep["unsafe_payments"])
        k[2].metric("Red-team passed", rep["redteam_passed"])
        k[3].metric("Exceptions found", f"{rep['exception_recall']:.0%}")
        k[4].metric("Model agrees with rules", f"{rep['model_agreement']:.0%}")
        k[5].metric("Cost per invoice", eur(rep["cost_eur_per_invoice"]))
        df = pd.DataFrame(rep["rows"])[["doc_id", "scenario", "expected", "got", "model_proposed", "found_exceptions", "unsafe", "seconds"]]
        st.dataframe(df, hide_index=True, width="stretch", height=300)
    st.caption("Live run: `python -m invoiceguard eval --live` with ANTHROPIC_API_KEY set. CI runs the scripted eval on every push.")

# ---------- security ----------
with tab_sec:
    st.markdown("How InvoiceGuard handles the [OWASP Top 10 for Agentic Applications]"
                "(https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/).")
    st.dataframe(pd.DataFrame([
        ("ASI01 Agent goal hijack", "PDF text goes to the model as marked untrusted data; a scanner flags instructions; "
         "the investigating model never sees the raw document; decisions are code."),
        ("ASI02 Tool misuse", "The model gets 6 read-only ERP tools (allow-list AND the server's read-only flag). "
         "Booking, holding and paying are called by code only."),
        ("ASI03 Identity and privilege abuse", "Payment needs an HMAC-signed, single-use token for one invoice and "
         "amount, minted by code after policy or a person approves. Approver limits per role."),
        ("ASI04 Supply chain", "Own MCP servers only, pinned major versions, tools listed at start-up."),
        ("ASI05 Unexpected code execution", "No shell, code or free SQL tools. Document ids are validated (no path tricks)."),
        ("ASI06 Memory and context poisoning", "No long-term memory: every case starts clean from ERP facts."),
        ("ASI08 Cascading failures", "Step limit, tool timeouts, and any failure leaves the invoice unpaid."),
        ("ASI09 Human trust exploitation", "Approvers see the check table from the ERP, not just the model's summary."),
        ("Outbound messages", "Notify tools are code-only. Recipients are directory keys, not addresses; supplier and "
         "invoice names in messages come from the vendor master; links only point back to the app; rate limit per "
         "recipient; every message is in the outbox and audit log."),
        ("Data protection", "IBANs are masked for the model and in logs; the ERP answers 'match: yes/no'. "
         "Claude via Amazon Bedrock in an EU region is one setting away."),
    ], columns=["Risk", "Control"]), hide_index=True, width="stretch")
    st.markdown("**Defence in depth on payments:** even if every model step were fooled, `schedule_payment` in the "
                "ERP MCP server refuses unless the token is valid, unused, for this invoice and amount, the supplier "
                "is active, and the IBAN matches the vendor master.")

# ---------- how it works ----------
with tab_how:
    st.graphviz_chart("""digraph {
      rankdir=LR; bgcolor="transparent"; nodesep=0.45;
      node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11, color="#2E4566", fillcolor="#16253D", fontcolor="#F3F1EA"];
      edge [fontname="Helvetica", fontsize=9, color="#8FA3BF", fontcolor="#B8C2D1"];
      read [label="read\\ndocuments MCP\\n+ injection scan"];
      extract [label="extract\\nLLM, fixed schema\\n(no tools)"];
      agent [label="agent\\nLLM investigates\\n(sees fields only)"];
      tools [label="tools\\n6 read-only\\nERP MCP tools"];
      check [label="check\\ncode: 3-way match\\n+ payment policy", fillcolor="#F2A65A", fontcolor="#0B1424", color="#F2A65A"];
      explain [label="explain\\nLLM note + email\\n(numbers verified)"];
      act [label="act\\ncode: book, pay,\\nhold, reject"];
      human [label="person approves\\n(signed token)", shape=oval];
      notify [label="notify MCP\\nemail + Teams\\n(code only)"];
      read -> extract -> agent; agent -> tools [label="needs data", style=dashed]; tools -> agent;
      agent -> check [label="findings", style=dashed]; check -> explain -> act;
      act -> human [label="over limit", style=dashed];
      act -> notify [label="approval, fraud alert", style=dashed];
    }""")
    st.markdown("**MCP servers and tools** (live list from the running servers)")
    st.dataframe(pd.DataFrame([
        {"server": m["server"], "tool": n, "read-only": "✅" if m["read_only"] else "✏️ writes",
         "model may call": "✅" if svc.hub.agent_may_call(n) else "—", "description": m["description"].split("\n")[0]}
        for n, m in sorted(svc.hub.tools.items(), key=lambda kv: (kv[1]["server"], kv[0]))
    ]), hide_index=True, width="stretch")
    st.caption("The app is the MCP client; each server runs as its own process over stdio here, and over Streamable "
               "HTTP in docker-compose. Source: `invoiceguard/mcp_servers/`.")

# ---------- audit ----------
with tab_audit:
    v = svc.audit.verify()
    st.markdown(("✅ **Audit chain intact**" if v["ok"] else f"❌ **Chain broken at row {v.get('broken_at')}**")
                + f" · {v['rows']} events. Each row stores the hash of the previous one, so edits or deletions show up.")
    ev = svc.audit.read(limit=300)
    if ev:
        st.dataframe(pd.DataFrame([{"time": e["ts"][:19], "case": e["run_id"], "actor": e["actor"], "event": e["kind"],
                                    "payload": json.dumps(e["payload"])[:160]} for e in ev]),
                     hide_index=True, width="stretch", height=480)
