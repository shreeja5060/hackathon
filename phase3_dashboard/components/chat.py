"""
Phase 3: the chat box.

Phase 1 (now): the UI and conversation history, with a placeholder answer.
Phase 2: answer() calls Claude with a retrieve() tool over Maryam's search,
cites chunk IDs, rejects citations that weren't retrieved, and logs each
exchange to SQLite. The UI below stays the same.

When the chat is focused on a finding, an answer can carry a
"proposed_recommendation". It is only a suggestion: it changes the finding
only when the reviewer clicks Apply.
"""

import streamlit as st

from data_source import update_recommendation
from navigation import REPORT, go_to

SUGGESTED_QUESTIONS = [
    "What's our MFA policy?",
    "Which NIST controls cover account management?",
    "What does the policy say about password requirements?",
]
FINDING_QUESTIONS = [
    "Why is this the coverage level?",
    "What does the NIST control require?",
    "Suggest a stronger recommendation",
]
REVISION_WORDS = ("recommend", "revise", "rewrite", "reword", "improve", "suggest",
                  "change", "update", "stronger", "better")


def answer(question: str, finding: dict | None) -> dict:
    """
    Return {"text", "citations", and optionally "proposed_recommendation"}.
    Placeholder until Phase 2.
    """
    if not finding:
        return {
            "text": "*(Placeholder answer: the chat agent connects in Phase 2. It will answer "
                    "only from the policy and NIST documents and cite its sources.)*",
            "citations": [],
        }

    cite = finding["citation"]
    citations = [cite] if cite["chunk_id"] else []

    if any(word in question.lower() for word in REVISION_WORDS):
        return {
            "text": (f"*(Placeholder: in Phase 2 the chat agent drafts this from the policy text "
                     f"and NIST {finding['framework_control']}.)*\n\n"
                     f"Here's a suggested recommendation for **{finding['id']} · "
                     f"{finding['requirement']}**. Review it, then click **Apply** to use it."),
            "citations": citations,
            "proposed_recommendation": (
                f"{finding['recommendation']} Document the scope, owner and review frequency, "
                f"and align the wording with NIST {finding['framework_control']}."
            ),
        }

    return {
        "text": (f"*(Placeholder answer: the chat agent connects in Phase 2.)*\n\n"
                 f"You asked about **{finding['id']} · {finding['requirement']}** "
                 f"({finding['coverage']} coverage, NIST {finding['framework_control']}).\n\n"
                 f"**Finding:** {finding['finding']}\n\n"
                 f"**Recommendation:** {finding['recommendation']}"),
        "citations": citations,
    }


def render_chat(findings: list[dict]) -> None:
    by_id = {f["id"]: f for f in findings}
    finding = by_id.get(st.session_state.get("chat_finding_id"))

    if finding:
        st.button("← Back to finding", on_click=go_to, args=(REPORT,))
    else:
        st.caption("Ask about the policies and NIST controls, or click "
                   "**Ask about this** on a finding to focus the chat on it.")

    history = st.session_state.setdefault("chat_history", [])

    starters = FINDING_QUESTIONS if finding else SUGGESTED_QUESTIONS
    cols = st.columns(len(starters))
    for col, q in zip(cols, starters):
        if col.button(q, width="stretch", key=f"starter_{q}"):
            st.session_state.pending_question = q
            st.rerun()

    for i, msg in enumerate(history):
        with st.chat_message(msg["role"]):
            st.markdown(msg["text"])
            if msg.get("proposed_recommendation"):
                _render_proposal(i, msg, by_id)
            _render_citations(msg.get("citations", []))

    question = st.chat_input("Ask a question about the policies…")
    question = question or st.session_state.pop("pending_question", None)
    if question:
        history.append({"role": "user", "text": question})
        reply = answer(question, finding)
        history.append({"role": "assistant", "finding_id": finding["id"] if finding else None, **reply})
        st.rerun()

    if history and st.button("Clear conversation"):
        st.session_state.chat_history = []
        st.rerun()


def _render_proposal(i: int, msg: dict, by_id: dict) -> None:
    finding = by_id.get(msg.get("finding_id"))
    with st.container(border=True):
        st.markdown(f"**Suggested recommendation**\n\n{msg['proposed_recommendation']}")
        if msg.get("applied"):
            st.success(f"Applied to {msg['finding_id']}.")
        elif finding:
            if st.button(f"Apply to {finding['id']}", key=f"apply_{i}", type="primary"):
                was_approved = finding["status"] == "approved"
                update_recommendation(finding, msg["proposed_recommendation"], "chat suggestion")
                msg["applied"] = True
                # Clear the finding's edit box so it shows the new text.
                st.session_state.pop(f"rec_{finding['id']}", None)
                st.session_state.flash = (
                    f"Recommendation for {finding['id']} updated. Approve it again to include it in the report."
                    if was_approved else f"Recommendation for {finding['id']} updated.")
                st.rerun()


def _render_citations(citations: list[dict]) -> None:
    if not citations:
        return
    with st.expander(f"Sources ({len(citations)})"):
        for c in citations:
            st.markdown(f"- {c['source']}, {c['locator']} (`{c['chunk_id']}`)")
