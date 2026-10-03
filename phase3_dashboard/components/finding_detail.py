"""Phase 3: one finding's evidence plus the human approve/reject step."""

import streamlit as st

from components.gap_table import COVERAGE_ICON, STATUS_ICON


def render_finding_detail(finding: dict) -> None:
    st.subheader(f"{finding['id']} · {finding['requirement']}")
    a, b, c = st.columns(3)
    a.markdown(f"**Coverage**  \n{COVERAGE_ICON.get(finding['coverage'], finding['coverage'])}")
    b.markdown(f"**NIST control**  \n{finding['framework_control'] or '—'}")
    c.markdown(f"**Status**  \n{STATUS_ICON[finding['status']]}")

    st.markdown(f"**Finding:** {finding['finding'] or '—'}")
    st.markdown(f"**Recommendation:** {finding['recommendation'] or '—'}")

    cite = finding["citation"]
    with st.container(border=True):
        st.markdown("**Evidence**")
        st.markdown(f"📄 {cite['source'] or 'Unknown source'}  \n"
                    f"📍 {cite['locator'] or 'No section given'}  \n"
                    f"🔖 `{cite['chunk_id'] or 'no chunk ID'}`")
        if finding["cited_text"]:
            st.markdown(f"> {finding['cited_text']}")
        else:
            st.caption("The cited policy text will appear here once the dashboard is connected to search (Phase 2).")

    note = st.text_area("Reviewer note (optional)", value=finding["reviewer_note"],
                        key=f"note_{finding['id']}", height=80)
    approve, reject, reset, ask = st.columns(4)
    if approve.button("✅ Approve", key=f"approve_{finding['id']}", width="stretch"):
        _set_status(finding, "approved", note)
    if reject.button("❌ Reject", key=f"reject_{finding['id']}", width="stretch"):
        _set_status(finding, "rejected", note)
    if reset.button("↩️ Reset", key=f"reset_{finding['id']}", width="stretch",
                    disabled=finding["status"] == "pending"):
        _set_status(finding, "pending", note)
    if ask.button("💬 Ask about this", key=f"ask_{finding['id']}", width="stretch"):
        st.session_state.chat_finding_id = finding["id"]
        st.toast("Chat is now focused on this finding. Open the Ask tab.")


def _set_status(finding: dict, status: str, note: str) -> None:
    finding["status"] = status
    finding["reviewer_note"] = note.strip()
    st.rerun()
