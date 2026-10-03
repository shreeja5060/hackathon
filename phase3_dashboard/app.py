"""
Phase 3: Compliance Copilot dashboard.

Run from the repo root:
    streamlit run phase3_dashboard/app.py

Flow: choose a policy -> run the analysis -> review the gap table -> open a
finding's evidence -> approve or reject it -> ask the chat box -> export the
approved report.
"""

import os
import sys
import time

import streamlit as st

# Make this folder importable however Streamlit is launched.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from data_source import list_policies, run_analysis
from export import approved, to_csv, to_json, to_markdown
from components.gap_table import render_gap_table, render_metrics
from components.finding_detail import render_finding_detail
from components.chat import render_chat

st.set_page_config(page_title="Compliance Copilot", page_icon="🛡️", layout="wide")

state = st.session_state
state.setdefault("findings", [])
state.setdefault("policy_name", None)
state.setdefault("chat_finding_id", None)


# ---- Sidebar: choose and analyze a policy ----
with st.sidebar:
    st.header("🛡️ Compliance Copilot")
    st.caption("Policy vs. NIST SP 800-53 gap analysis, with human review.")

    st.subheader("1. Choose a policy")
    uploaded = st.file_uploader("Upload a policy PDF", type="pdf")
    library = list_policies()
    picked = st.selectbox("…or pick one from the library", library,
                          index=None, placeholder="Select a policy",
                          disabled=uploaded is not None)
    policy_name = uploaded.name if uploaded else picked

    st.subheader("2. Analyze")
    if st.button("Run gap analysis", type="primary", width="stretch",
                 disabled=policy_name is None):
        with st.status("Analyzing policy…", expanded=True) as status:
            for step in ["Extractor: pulling requirements from the policy",
                         "Mapper: matching requirements to NIST controls",
                         "Auditor: classifying coverage and drafting recommendations"]:
                st.write(step)
                time.sleep(0.4)
            state.findings = run_analysis(policy_name)
            state.policy_name = policy_name
            state.chat_finding_id = None
            status.update(label=f"Done: {len(state.findings)} findings", state="complete")

    st.divider()
    st.caption("⚠️ Phase 1 preview: showing sample findings, not live analysis.")


# ---- Main area ----
st.title("Compliance Gap Report")

if not state.findings:
    st.info("Choose a policy in the sidebar and click **Run gap analysis** to begin.")
    st.stop()

st.caption(f"Policy: **{state.policy_name}**")
render_metrics(state.findings)

report_tab, chat_tab, export_tab = st.tabs(["📋 Gap report", "💬 Ask the documents", "📤 Export"])

with report_tab:
    selected_id = render_gap_table(state.findings)
    if selected_id:
        st.divider()
        finding = next(f for f in state.findings if f["id"] == selected_id)
        render_finding_detail(finding)

with chat_tab:
    render_chat(state.findings)

with export_tab:
    ready = approved(state.findings)
    pending = sum(f["status"] == "pending" for f in state.findings)
    st.markdown(f"**{len(ready)}** approved finding(s) will be exported. "
                f"Rejected findings are left out.")
    if pending:
        st.warning(f"{pending} finding(s) still need review.")
    if ready:
        stem = os.path.splitext(state.policy_name)[0] + "_gap_report"
        a, b, c = st.columns(3)
        a.download_button("Download Markdown", to_markdown(ready, state.policy_name),
                          file_name=f"{stem}.md", width="stretch")
        b.download_button("Download CSV", to_csv(ready),
                          file_name=f"{stem}.csv", width="stretch")
        c.download_button("Download JSON", to_json(ready),
                          file_name=f"{stem}.json", width="stretch")
        with st.expander("Preview report"):
            st.markdown(to_markdown(ready, state.policy_name))
    else:
        st.info("Approve at least one finding in the Gap report tab to export it.")
