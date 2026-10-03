"""
Phase 3: which view is showing.

st.tabs can't be switched from code, so the views are a radio control keyed
in session state. Buttons switch views with go_to() as an on_click callback,
which runs before the control is drawn on the next rerun.
"""

import streamlit as st

REPORT = "Gap report"
CHAT = "Ask the documents"
EXPORT = "Export"
VIEWS = [REPORT, CHAT, EXPORT]


def go_to(view: str) -> None:
    st.session_state.view = view


def focus_chat_on(finding_id: str) -> None:
    """On-click callback for "Ask about this": focus the chat and open it."""
    st.session_state.chat_finding_id = finding_id
    go_to(CHAT)
