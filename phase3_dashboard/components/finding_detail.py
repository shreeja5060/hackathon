"""Phase 3: one finding's evidence plus the human review step."""

import streamlit as st

from components.gap_table import STATUS_LABEL
from data_source import update_recommendation
from navigation import focus_chat_on


def render_finding_detail(finding: dict) -> None:
    fid = finding["id"]
    title, close = st.columns([5, 1])
    title.subheader(f"{fid} · {finding['requirement']}")
    close.button("Close", key=f"close_{fid}", width="stretch", on_click=_close)

    a, b, c = st.columns(3)
    a.markdown(f"**Coverage**  \n{finding['coverage']}")
    b.markdown(f"**NIST control**  \n{finding['framework_control'] or '—'}")
    c.markdown(f"**Status**  \n{STATUS_LABEL[finding['status']]}")

    st.markdown(f"**Finding:** {finding['finding'] or '—'}")
    st.markdown(f"**Recommendation:** {finding['recommendation'] or '—'}")
    if finding["recommendation_source"]:
        st.caption(f"Changed by {finding['recommendation_source']}. "
                   f"Original: \"{finding['original_recommendation'] or '—'}\"")

    cite = finding["citation"]
    with st.container(border=True):
        st.markdown("**Evidence**")
        st.markdown(f"**Source:** {cite['source'] or 'Unknown source'}  \n"
                    f"**Section:** {cite['locator'] or 'No section given'}  \n"
                    f"**Chunk ID:** `{cite['chunk_id'] or 'none'}`")
        if finding["cited_text"]:
            st.markdown(f"> {finding['cited_text']}")
        else:
            st.caption("The cited policy text will appear here once the dashboard is connected to search (Phase 2).")

    note = st.text_area("Reviewer note (optional)", value=finding["reviewer_note"],
                        key=f"note_{fid}", height=80)

    editing_key = f"editing_{fid}"
    editing = st.session_state.get(editing_key, False)

    approve, reject, reset, edit, ask = st.columns(5)
    if approve.button("Approve", key=f"approve_{fid}", width="stretch", type="primary"):
        _set_status(finding, "approved", note)
    if reject.button("Reject", key=f"reject_{fid}", width="stretch"):
        _set_status(finding, "rejected", note)
    if reset.button("Reset", key=f"reset_{fid}", width="stretch",
                    disabled=finding["status"] == "pending"):
        _set_status(finding, "pending", note)
    edit.button("Close editor" if editing else "Edit recommendation", key=f"edit_{fid}",
                width="stretch", on_click=_toggle, args=(editing_key,))
    ask.button("Ask about this", key=f"ask_{fid}", width="stretch",
               on_click=focus_chat_on, args=(fid,),
               help="Open the chat focused on this finding. You can also ask it to suggest a better recommendation.")

    if editing:
        _render_editor(finding)


def _render_editor(finding: dict) -> None:
    fid = finding["id"]
    with st.container(border=True):
        draft = st.text_area("Edit recommendation", value=finding["recommendation"],
                             key=f"rec_{fid}", height=100)
        save, restore, _ = st.columns([1, 1, 3])
        if save.button("Save", key=f"save_rec_{fid}", width="stretch", type="primary"):
            _apply(finding, draft, "reviewer edit")
        if restore.button("Restore original", key=f"restore_rec_{fid}", width="stretch",
                          disabled=not finding["recommendation_source"]):
            _apply(finding, finding["original_recommendation"], "")


def _apply(finding: dict, text: str, source: str) -> None:
    was_approved = finding["status"] == "approved"
    if update_recommendation(finding, text, source):
        if not source:  # restored: no longer a change
            finding["recommendation_source"] = ""
        st.session_state.flash = (
            "Recommendation updated. Approve the finding again to include it in the report."
            if was_approved else "Recommendation updated.")
        # Drop the edit box's own state so it shows the new text after the rerun.
        st.session_state.pop(f"rec_{finding['id']}", None)
        st.session_state[f"editing_{finding['id']}"] = False
        st.rerun()
    else:
        st.toast("No changes to save.")


def _toggle(key: str) -> None:
    st.session_state[key] = not st.session_state.get(key, False)


def _close() -> None:
    # Closing the finding also ends the chat's focus on it.
    if st.session_state.get("chat_finding_id") == st.session_state.get("selected_id"):
        st.session_state.chat_finding_id = None
    st.session_state.selected_id = None


def _set_status(finding: dict, status: str, note: str) -> None:
    finding["status"] = status
    finding["reviewer_note"] = note.strip()
    st.rerun()
