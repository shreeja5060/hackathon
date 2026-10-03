"""
Phase 3: the chat box.

Phase 1 (now): the UI and conversation history, with a placeholder answer.
Phase 2: answer() calls Claude with a retrieve() tool over Maryam's search,
cites chunk IDs, rejects citations that weren't retrieved, and logs each
exchange to SQLite. The UI below stays the same.
"""

import streamlit as st

SUGGESTED_QUESTIONS = [
    "What's our MFA policy?",
    "Which NIST controls cover account management?",
    "What does the policy say about password requirements?",
]


def answer(question: str, finding: dict | None) -> dict:
    """Return {"text": ..., "citations": [...]}. Placeholder until Phase 2."""
    if finding:
        cite = finding["citation"]
        text = (
            f"*(Placeholder answer: the chat agent connects in Phase 2.)*\n\n"
            f"You asked about **{finding['id']} · {finding['requirement']}** "
            f"({finding['coverage']} coverage, NIST {finding['framework_control']}).\n\n"
            f"**Finding:** {finding['finding']}\n\n"
            f"**Recommendation:** {finding['recommendation']}"
        )
        return {"text": text, "citations": [cite] if cite["chunk_id"] else []}
    return {
        "text": "*(Placeholder answer: the chat agent connects in Phase 2. It will answer "
                "only from the policy and NIST documents and cite its sources.)*",
        "citations": [],
    }


def render_chat(findings: list[dict]) -> None:
    by_id = {f["id"]: f for f in findings}
    focus_id = st.session_state.get("chat_finding_id")
    finding = by_id.get(focus_id)

    if finding:
        left, right = st.columns([4, 1])
        left.info(f"Focused on **{finding['id']} · {finding['requirement']}**. "
                  "Answers will use this finding and its evidence.")
        if right.button("Clear focus", width="stretch"):
            st.session_state.chat_finding_id = None
            st.rerun()
    else:
        st.caption("Ask about the policies and NIST controls, or click "
                   "**💬 Ask about this** on a finding to focus the chat on it.")

    history = st.session_state.setdefault("chat_history", [])

    if not history:
        cols = st.columns(len(SUGGESTED_QUESTIONS))
        for col, q in zip(cols, SUGGESTED_QUESTIONS):
            if col.button(q, width="stretch"):
                st.session_state.pending_question = q
                st.rerun()

    for msg in history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["text"])
            _render_citations(msg.get("citations", []))

    question = st.chat_input("Ask a question about the policies…")
    question = question or st.session_state.pop("pending_question", None)
    if question:
        history.append({"role": "user", "text": question})
        reply = answer(question, finding)
        history.append({"role": "assistant", **reply})
        st.rerun()

    if history and st.button("Clear conversation"):
        st.session_state.chat_history = []
        st.rerun()


def _render_citations(citations: list[dict]) -> None:
    if not citations:
        return
    with st.expander(f"Sources ({len(citations)})"):
        for c in citations:
            st.markdown(f"- {c['source']}, {c['locator']} (`{c['chunk_id']}`)")
