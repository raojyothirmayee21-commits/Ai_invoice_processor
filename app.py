"""Streamlit UI for the AP team.

Run:  streamlit run app.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from invoice_processor.config import ROOT, get_client, get_settings
from invoice_processor.db import Store
from invoice_processor.pipeline import process_file

st.set_page_config(page_title="AI Invoice Processor", page_icon="🧾", layout="wide")
settings = get_settings()

STATUS_LABEL = {
    "ready_for_payment": "✅ Ready for payment",
    "pending_manager_approval": "🟡 Manager approval",
    "needs_human_review": "🔴 Human review",
    "approved": "👍 Approved by human",
    "rejected": "⛔ Rejected",
    "processing": "⏳ Processing",
}


DEMO_DB = ROOT / "data" / "demo.db"


@st.cache_resource
def client_or_none():
    try:
        return get_client()
    except RuntimeError:
        return None


@st.cache_resource
def shared_store() -> Store:
    store = Store(settings.db_path)
    store.seed_vendors(settings.vendors_csv)
    return store


def demo_store() -> Store:
    # Each visitor gets a private copy of the demo database, so their approve/reject
    # clicks never change what the next visitor sees.
    if "demo_store" not in st.session_state:
        tmp = Path(tempfile.mkdtemp()) / "demo.db"
        shutil.copy(DEMO_DB, tmp)
        st.session_state.demo_store = Store(tmp)
    return st.session_state.demo_store


client = client_or_none()
DEMO_MODE = client is None and DEMO_DB.exists()
store = demo_store() if DEMO_MODE else shared_store()

st.title("🧾 AI Invoice Processor")
st.caption("Invoices in → PII masked → Claude extracts → code validates → agent routes → humans decide the edge cases.")
if DEMO_MODE:
    st.info(
        "**Demo mode.** These are real Claude results for the 35 sample invoices, processed ahead of time, "
        "so browsing this page makes no API calls. Try the **Review queue**: approve or reject an invoice "
        "and see it in the **Audit log**. Your changes reset when you leave. "
        "[Code on GitHub](https://github.com/raojyothirmayee21-commits/Ai_invoice_processor)"
    )
elif client is None:
    st.warning("No ANTHROPIC_API_KEY found. Add it to `.env` to process invoices; browsing works without it.")


def show_invoice(row: dict, expanded: bool = False) -> None:
    data = json.loads(row["data_json"] or "{}")
    issues = json.loads(row["issues_json"] or "[]")
    pii = json.loads(row["pii_masked_json"] or "{}")
    meta = json.loads(row["extraction_meta_json"] or "{}")
    title = f"#{row['id']} · {row['vendor_name'] or '?'} · {row['invoice_number'] or '?'} · " \
            f"${(row['total'] or 0):,.2f} · {STATUS_LABEL.get(row['status'], row['status'])}"
    with st.expander(title, expanded=expanded):
        st.markdown(f"**Decision reason:** {row['decision_reason'] or '-'}")
        c1, c2, c3 = st.columns(3)
        c1.metric("Confidence", f"{row['confidence']:.2f}")
        c2.metric("PII items masked", sum(pii.values()))
        c3.metric("Extraction latency", f"{meta.get('latency_s', 0)}s")
        if issues:
            st.error("Validation issues:\n\n" + "\n".join(f"- {i}" for i in issues))
        if meta.get("warnings"):
            st.warning("Warnings (non-blocking):\n\n" + "\n".join(f"- {w}" for w in meta["warnings"]))
        left, right = st.columns(2)
        with left:
            st.markdown("**Extracted fields**")
            fields = {k: v for k, v in data.items() if k not in ("line_items",)}
            st.json(fields, expanded=False)
            if data.get("line_items"):
                st.dataframe(pd.DataFrame(data["line_items"]), hide_index=True, width="stretch")
        with right:
            st.markdown("**Agent trace**")
            trace = json.loads(row["agent_trace_json"] or "[]")
            if not trace:
                st.caption("No agent run (deterministic routing).")
            for step in trace:
                if step["type"] == "tool":
                    st.markdown(f"🔧 `{step['name']}({json.dumps(step['input'])[:120]})`")
                    st.code(json.dumps(step["output"], default=str)[:400], language="json")
                else:
                    st.markdown(f"💭 {step['text']}")


tab_process, tab_queue, tab_all, tab_eval, tab_audit = st.tabs(
    ["Process", "Review queue", "All invoices", "Evaluation", "Audit log"]
)

with tab_process:
    st.subheader("Upload invoices")
    uploads = st.file_uploader("PDF or image", type=["pdf", "png", "jpg", "jpeg"], accept_multiple_files=True)
    sample_dir = ROOT / "data" / "sample_invoices"
    use_samples = st.checkbox(f"Or process the {len(list(sample_dir.glob('*.pdf')))} bundled sample invoices")
    if DEMO_MODE:
        st.caption("Processing new invoices is turned off in the demo, since it would call the Claude API. "
                   "Clone the repo and add your own key to try it.")
    if st.button("Process", type="primary", disabled=client is None):
        paths: list[Path] = []
        if uploads:
            up_dir = settings.inbox_dir / "uploads"
            up_dir.mkdir(parents=True, exist_ok=True)
            for f in uploads:
                p = up_dir / Path(f.name).name
                p.write_bytes(f.getvalue())
                paths.append(p)
        if use_samples:
            paths += sorted(sample_dir.glob("*.pdf"))
        bar = st.progress(0.0)
        for i, p in enumerate(paths, 1):
            try:
                res = process_file(p, client, store, settings)
                st.write(f"**{p.name}** → {STATUS_LABEL.get(res.status, res.status)} — {res.reason}")
            except Exception as exc:  # show, don't crash the page
                st.error(f"{p.name}: {exc}")
            bar.progress(i / len(paths))

with tab_queue:
    pending = store.list_invoices(["needs_human_review", "pending_manager_approval"])
    st.subheader(f"Waiting for a human ({len(pending)})")
    reviewer = st.text_input("Your name (recorded in the audit log)", value="ap-reviewer")
    for row in pending:
        show_invoice(row)
        c1, c2, c3 = st.columns([3, 1, 1])
        comment = c1.text_input("Comment", key=f"c{row['id']}", label_visibility="collapsed",
                                placeholder="Why? (required for the audit trail)")
        if c2.button("Approve", key=f"a{row['id']}", disabled=not comment):
            store.record_human_decision(row["id"], True, reviewer, comment)
            st.rerun()
        if c3.button("Reject", key=f"r{row['id']}", disabled=not comment):
            store.record_human_decision(row["id"], False, reviewer, comment)
            st.rerun()

with tab_all:
    rows = store.list_invoices()
    if rows:
        df = pd.DataFrame(rows)[["id", "file_name", "vendor_name", "invoice_number", "total", "status", "confidence"]]
        c1, c2, c3 = st.columns(3)
        c1.metric("Invoices", len(df))
        c2.metric("Auto-routed to payment", int((df.status == "ready_for_payment").sum()))
        c3.metric("Value processed", f"${df.total.fillna(0).sum():,.0f}")
        st.dataframe(df, hide_index=True, width="stretch")
        pick = st.selectbox("Inspect invoice", df.id.tolist())
        show_invoice(store.get_invoice(int(pick)), expanded=True)
    else:
        st.info("Nothing processed yet.")

with tab_eval:
    path = ROOT / "evals" / "results" / "latest.json"
    if not path.exists():
        st.info("No evaluation yet. Run `python scripts/run_eval.py`.")
    else:
        r = json.loads(path.read_text())
        st.caption(f"Run {r['run_at']} · {r['n_invoices']} invoices · {r['extraction_model']} / {r['agent_model']}")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Docs fully correct", f"{r['documents_fully_correct']:.0%}")
        c2.metric("Routing accuracy", f"{r['routing_accuracy']:.0%}")
        c3.metric("Unsafe auto-approvals", r["unsafe_auto_approvals"])
        c4.metric("Avg latency", f"{r['avg_extraction_latency_s']}s")
        st.markdown("**Field accuracy**")
        st.bar_chart(pd.Series(r["field_accuracy"], name="accuracy"))
        st.markdown("**Routing accuracy by case type**")
        st.bar_chart(pd.Series(r["routing_accuracy_by_tag"], name="accuracy"))
        if r["failures"]:
            st.markdown("**Failures**")
            st.dataframe(pd.DataFrame(r["failures"])[["file", "tags", "expected_status", "actual_status", "wrong_fields"]],
                         hide_index=True, width="stretch")

with tab_audit:
    st.dataframe(pd.DataFrame(store.audit_trail()), hide_index=True, width="stretch")
