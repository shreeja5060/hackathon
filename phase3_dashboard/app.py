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
from navigation import CHAT, REPORT, VIEWS

st.set_page_config(page_title="Compliance Copilot", layout="wide")

state = st.session_state
state.setdefault("findings", [])
state.setdefault("policy_name", None)
state.setdefault("chat_finding_id", None)
state.setdefault("selected_id", None)
state.setdefault("view", REPORT)

# Messages set just before a rerun (st.toast would be lost), shown once here.
if state.get("flash"):
    st.toast(state.pop("flash"))


# ---- Sidebar: choose and analyze a policy ----
with st.sidebar:
    st.header("Compliance Copilot")
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
            state.selected_id = None
            state.chat_history = []
            status.update(label=f"Done: {len(state.findings)} findings", state="complete")

    st.divider()
    st.caption("Phase 1 preview: showing sample findings, not live analysis.")


# ---- Main area ----
st.title("Compliance Gap Report")

if not state.findings:
    st.info("Choose a policy in the sidebar and click **Run gap analysis** to begin.")
    st.stop()

st.caption(f"Policy: **{state.policy_name}**")
render_metrics(state.findings)

view = st.radio("View", VIEWS, key="view", horizontal=True, label_visibility="collapsed")
st.divider()

if view == REPORT:
    clicked_id = render_gap_table(state.findings)
    # Remember the open finding, so it's still open after visiting the chat.
    if clicked_id:
        state.selected_id = clicked_id
    finding = next((f for f in state.findings if f["id"] == state.selected_id), None)
    if finding:
        st.divider()
        render_finding_detail(finding)

elif view == CHAT:
    render_chat(state.findings)

else:
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
