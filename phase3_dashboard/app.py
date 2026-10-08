"""Compliance Copilot: the Phase 3 dashboard.

Start it from this folder (so .streamlit/config.toml and the fonts load):

    streamlit run app.py

Data comes from a backend (see backends/): the simulator by default, or the
real Phase 1 + Phase 2 code with COPILOT_BACKEND=live.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from phase3_dashboard.backends import MODES, BackendUnavailable, create_backend, default_mode  # noqa: E402
from phase3_dashboard.core import analysis, review, security  # noqa: E402
from phase3_dashboard.core.contracts import COVERAGE_VALUES, STATUS_VALUES, ContractError, normalize_chat_answer  # noqa: E402
from phase3_dashboard.core.report import build_report, to_csv, to_json, to_markdown  # noqa: E402
from phase3_dashboard.core.security import escape_md  # noqa: E402
from phase3_dashboard.ui import components as ui  # noqa: E402

st.set_page_config(page_title="Compliance Copilot", page_icon=":material/shield:", layout="wide")
log = logging.getLogger("compliance_copilot")

PAGES = ("Analyze", "Review", "Report", "Assistant")
ANALYZE, REVIEW, REPORT, ASSISTANT = PAGES
MODE_LABELS = {"simulated": "Simulator (no AI calls)", "live": "Live pipeline"}
ORIGIN_LABELS = {"upload": "Uploaded file", "indexed": "From the Phase 1 index", "sample": "Sample policy"}
SOURCE_OPTIONS = ("Upload a PDF", "Choose a loaded policy")
PROMPTS_GENERAL = (
    "What does our policy say about MFA?",
    "Can staff share passwords?",
    "Which NIST control covers account reviews?",
)
PROMPTS_REVIEW = (
    "Which findings have Missing coverage?",
    "Suggest stronger wording for the Partial findings",
    "Suggest better wording for the rejected findings",
)


# ====================================================================== state

def init_state() -> None:
    ss = st.session_state
    ss.setdefault("mode", default_mode())
    ss.setdefault("nav", ANALYZE)
    ss.setdefault("backends", {})
    ss.setdefault("doc", None)
    ss.setdefault("doc_key", None)
    ss.setdefault("review_session", None)
    ss.setdefault("chat", [])
    ss.setdefault("focus_id", None)
    ss.setdefault("queue_ver", 0)
    ss.setdefault("inline", {})  # finding_id -> index of its latest assistant reply in chat
    # Navigation requested by the previous run (a widget's state can only be
    # changed before that widget is drawn).
    if "_goto" in ss:
        ss.nav = ss.pop("_goto")


def reset_work() -> None:
    ss = st.session_state
    ss.doc = None
    ss.doc_key = None
    ss.review_session = None
    ss.chat = []
    ss.focus_id = None
    ss.inline = {}
    ss.queue_ver = ss.get("queue_ver", 0) + 1


def go_to(page: str, message: str | None = None) -> None:
    st.session_state["_goto"] = page
    if message:
        st.session_state["_flash"] = message
    st.rerun()


def _sim_delay() -> float:
    try:
        return max(0.0, float(os.getenv("COPILOT_SIM_DELAY", "0.3")))
    except ValueError:
        return 0.3


def get_backend(mode: str):
    cache = st.session_state.backends
    if mode not in cache:  # failures aren't cached, so fixing the cause and rerunning works
        options = {"delay": _sim_delay()} if mode == "simulated" else {}
        cache[mode] = create_backend(mode, **options)
    return cache[mode]


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _set_focus(finding_id: str | None) -> None:
    """Open a finding from code; a fresh queue key clears the table's old row selection."""
    st.session_state.focus_id = finding_id
    st.session_state.queue_ver += 1


# ====================================================================== sidebar

def render_sidebar():
    with st.sidebar:
        st.markdown("### :material/shield: Compliance Copilot", anchors=False)
        st.caption("AI drafts the findings. People sign them off.")
        reviewer = st.text_input(
            "Reviewer",
            key="reviewer_name",
            max_chars=review.MAX_REVIEWER_CHARS,
            placeholder="Your full name",
            help="Required to approve or reject findings. Every decision is logged under this name.",
        )
        st.divider()
        st.radio("Data source", MODES, format_func=MODE_LABELS.get, key="mode", on_change=reset_work)
        try:
            backend = get_backend(st.session_state.mode)
        except BackendUnavailable as exc:
            st.error(escape_md(str(exc)), icon=":material/error:")
            return None, reviewer

        framework = backend.framework_info()
        st.selectbox(
            "Framework",
            [framework["title"]],
            key="framework_choice",
            help="The agents map requirements to NIST SP 800-53. Add frameworks here once the agents support them.",
        )
        details = f"Version {framework.get('version') or 'unknown'}"
        if framework.get("publication_date"):
            details += f", published {framework['publication_date']}"
        st.caption(escape_md(details))
        if framework.get("note"):
            st.caption(escape_md(framework["note"]))

        if not backend.simulated:
            retrieval = backend.retrieval_backend or "unknown"
            if backend.placeholder_retrieval:
                st.warning(f"Retrieval: {escape_md(retrieval)}. Findings won't use the real index.",
                           icon=":material/warning:")
            else:
                st.caption(f"Retrieval: {escape_md(retrieval)}")
        else:
            with st.expander("Simulator options"):
                failing = st.toggle(
                    "Simulate a failing section",
                    key="sim_fail",
                    help="Makes the second section fail the way a malformed model reply would.",
                )
                backend.set_failing_section(1 if failing else None)

        st.divider()
        if st.button("Start over", key="start_over", icon=":material/restart_alt:",
                     help="Clears the policy, the review and the chat."):
            reset_work()
            go_to(ANALYZE)
        st.caption("Every decision is logged with the reviewer's name and time. "
                   "Only approved findings reach the report.")
    return backend, reviewer


# Labels must not change between runs: the browser sends back the label it
# showed, so a count in a label would make the selection jump.
NAV_LABELS = {
    ANALYZE: ":material/upload_file: Analyze policy",
    REVIEW: ":material/fact_check: Review findings",
    REPORT: ":material/verified: Signed report",
    ASSISTANT: ":material/forum: Assistant",
}


# ====================================================================== analyze

def _load_upload(backend, upload):
    data = upload.getvalue()
    key = f"{st.session_state.mode}:upload:{hashlib.sha256(data).hexdigest()}"
    if st.session_state.doc_key != key:
        try:
            st.session_state.doc = backend.parse_upload(upload.name, data)
            st.session_state.doc_key = key
        except (security.UploadRejected, BackendUnavailable) as exc:
            st.session_state.doc = None
            st.session_state.doc_key = None
            st.error(escape_md(str(exc)), icon=":material/error:")
    return st.session_state.doc


def _load_existing(backend, source: str):
    key = f"{st.session_state.mode}:loaded:{source}"
    if st.session_state.doc_key != key:
        try:
            st.session_state.doc = backend.load_policy(source)
            st.session_state.doc_key = key
        except BackendUnavailable as exc:
            st.error(escape_md(str(exc)), icon=":material/error:")
            return None
    return st.session_state.doc


def _document_summary(doc) -> None:
    pages = {chunk.get("page") for chunk in doc.chunks if chunk.get("page") is not None}
    st.markdown(f"**{escape_md(doc.source)}**")
    st.caption(f"{ORIGIN_LABELS.get(doc.origin, doc.origin)}. "
               f"{plural(len(doc.chunks), 'section')} across {plural(len(pages), 'page')}.")
    for note in doc.notes:
        st.caption(escape_md(note))
    flagged = security.scan_document(doc.chunks, doc.chunk_label)
    if flagged:
        listed = "; ".join(f"{escape_md(label)} ({', '.join(reasons)})" for label, reasons in flagged)
        st.warning(
            f"Possible prompt-injection text: {listed}. The agents treat document text as data, not "
            "instructions, but check findings from these sections with extra care.",
            icon=":material/gpp_maybe:",
        )


def _section_picker(doc) -> list[str]:
    """A table of the policy's sections with a checkbox for each; returns the chosen chunk IDs."""
    rows = [{
        "Analyze": True,
        "Section": doc.chunk_label(chunk),
        "Page": chunk.get("page"),
        "Words": len(chunk["text"].split()),
        "Flag": "Possible prompt injection" if security.scan_for_injection(chunk["text"]) else "",
    } for chunk in doc.chunks]
    edited = st.data_editor(
        pd.DataFrame(rows),
        key=f"sections:{hashlib.sha256(st.session_state.doc_key.encode()).hexdigest()[:12]}",
        hide_index=True,
        disabled=["Section", "Page", "Words", "Flag"],
        column_config={
            "Analyze": st.column_config.CheckboxColumn(width=76, help="Clear a box to skip that section."),
            "Section": st.column_config.TextColumn(width=230),
            "Page": st.column_config.NumberColumn(width=52),
            "Words": st.column_config.NumberColumn(width=60),
            "Flag": st.column_config.TextColumn(width=170),
        },
    )
    return [chunk["chunk_id"] for chunk, keep in zip(doc.chunks, edited["Analyze"]) if keep]


def _render_how_it_works(backend) -> None:
    with st.container(border=True):
        st.markdown("#### How a review works", anchors=False)
        st.markdown(
            "1. **Extractor** reads each section and lists the requirements someone could check.\n"
            "2. **Mapper** matches each requirement to a NIST SP 800-53 control from the official OSCAL catalog.\n"
            "3. **Auditor** rates coverage as Full, Partial, Missing or Not observable, explains it in plain "
            "words and drafts a fix.\n"
            "4. **You** approve, edit or reject every finding. The pipeline stays paused until you sign off, "
            "and only approved findings reach the report."
        )
        if backend.simulated:
            st.caption(":material/science: Simulator: local rules produce findings in the agents' exact format, "
                       "so you can try the workflow without API calls. Switch to Live pipeline in the sidebar "
                       "for real analysis.")
        else:
            st.caption(":material/bolt: Live pipeline: each section makes one Extractor call to Claude, plus a "
                       "Mapper and an Auditor call for every requirement it finds.")


def _run_analysis(backend, doc, chunk_ids):
    progress = st.progress(0.0, text="Starting the agents")
    with st.status("Running Extractor, Mapper and Auditor on each section", expanded=True) as status:
        def on_progress(done, total, result):
            progress.progress(done / total, text=f"{done} of {total} sections analyzed")
            label = escape_md(result.label)
            if result.status == "ok":
                status.write(f":material/check: {label}: {result.finding_count} finding(s)")
            elif result.status == "partial":
                status.write(f":material/warning: {label}: {result.finding_count} finding(s), "
                             f"{len(result.warnings)} item(s) failed")
            else:
                status.write(f":material/warning: {label}: {escape_md(result.error)}")

        try:
            session = analysis.analyze_policy(backend, doc, chunk_ids, on_progress=on_progress)
        except ValueError as exc:
            status.update(label="The analysis didn't start", state="error")
            st.error(escape_md(str(exc)))
            return None
        failed = len(session.run.section_errors)
        if failed == len(session.run.sections):
            status.update(label="Every section failed. See the errors above.", state="error")
        else:
            status.update(label=f"Paused for your review: {len(session.findings)} findings", state="complete")
    return session


def page_analyze(backend) -> None:
    main, side = st.columns([3, 2], gap="large")
    with side:
        _render_how_it_works(backend)
    with main:
        st.markdown("#### Policy to review", anchors=False)
        captions = ("PDF with selectable text",
                    "Sample policies in the simulator" if backend.simulated else "Policies in the Phase 1 index")
        origin = st.radio("Policy source", SOURCE_OPTIONS, captions=captions, horizontal=True,
                          key="policy_origin", label_visibility="collapsed")

        doc = None
        if origin == SOURCE_OPTIONS[0]:
            upload = st.file_uploader(
                "Policy PDF",
                type=["pdf"],
                key="policy_upload",
                max_upload_size=security.MAX_UPLOAD_MB,
                help=f"Up to {security.MAX_UPLOAD_MB} MB and {security.MAX_PDF_PAGES} pages. Read in memory, never saved.",
            )
            if upload is not None:
                doc = _load_upload(backend, upload)
        else:
            choices = backend.list_policies()
            if not choices:
                st.info("No indexed policies yet. Run the Phase 1 scripts so data/processed/policy_chunks.json "
                        "exists, or upload a PDF.")
            else:
                doc = _load_existing(backend, st.selectbox("Loaded policy", choices, key="loaded_policy"))
        if doc is None:
            return

        with st.container(border=True):
            _document_summary(doc)
            selected = _section_picker(doc)

        current = st.session_state.review_session
        unsaved = current is not None and not current.finalized and any(f.status != "pending" for f in current.findings)
        confirmed = True
        if unsaved:
            confirmed = st.checkbox("Discard my current review decisions and start a new analysis",
                                    key="confirm_discard")

        if st.button(f"Run analysis on {plural(len(selected), 'section')}", type="primary", key="run_analysis",
                     icon=":material/play_arrow:", disabled=not selected or not confirmed):
            session = _run_analysis(backend, doc, selected)
            if session is not None and session.run.ok_threads:
                st.session_state.review_session = session
                st.session_state.inline = {}
                first = next((f.finding_id for f in session.findings), None)
                _set_focus(first)
                failed = len(session.run.section_errors)
                message = f"{len(session.findings)} findings are waiting for your review."
                if failed:
                    message += f" {failed} section(s) failed."
                go_to(REVIEW, message)


# ====================================================================== assistant (shared)

def _review_context() -> list[dict]:
    return review.assistant_context(st.session_state.review_session)


def _chat_history() -> list[dict]:
    history = []
    for m in st.session_state.chat:
        if m.get("error"):
            continue
        item = {"role": m["role"], "content": m["content"]}
        if m["role"] == "assistant" and m.get("citations"):
            item["citations"] = m["citations"]  # so follow-ups know what each [n] referred to
        history.append(item)
    return history


def ask_assistant(backend, question: str, reviewer: str) -> dict:
    """Ask the Q&A agent, record any suggested wording on its finding, and add both turns to the chat."""
    session = st.session_state.review_session
    known = {f.finding_id for f in session.findings} if session else set()
    try:
        answer = normalize_chat_answer(
            backend.ask(question, findings=_review_context(), history=_chat_history()),
            known_finding_ids=known,
        )
    except (BackendUnavailable, ContractError, ValueError) as exc:
        reply = {"role": "assistant", "content": f"I couldn't answer that: {exc}", "error": True}
    except Exception as exc:  # noqa: BLE001 - the Q&A agent is outside code; never crash the page
        log.exception("Q&A failed")
        reply = {"role": "assistant", "error": True,
                 "content": f"The assistant failed ({type(exc).__name__}). The terminal has the details."}
    else:
        reply = {"role": "assistant", "content": answer.answer, "citations": list(answer.citations),
                 "dropped": answer.dropped_citations, "suggestions": list(answer.suggestions),
                 "dropped_suggestions": answer.dropped_suggestions}
        for suggestion in answer.suggestions:
            review.record_suggestion(session, suggestion["finding_id"], suggestion["recommendation"],
                                     reviewer, question)
    st.session_state.chat.append({"role": "user", "content": question})
    st.session_state.chat.append(reply)
    return reply


# ====================================================================== review

def _ensure_focus(shown) -> str | None:
    ids = [f.finding_id for f in shown]
    focus = st.session_state.focus_id
    if focus not in ids:
        focus = next((f.finding_id for f in shown if f.status == "pending"), ids[0] if ids else None)
        st.session_state.focus_id = focus
    return focus


def _on_queue_select(key: str, ids: list[str]) -> None:
    state = st.session_state.get(key)
    rows = state.selection.rows if state is not None else []
    if rows and rows[0] < len(ids):
        st.session_state.focus_id = ids[rows[0]]


def _advance(session, shown, current_id: str) -> None:
    """After a decision, open the next finding that still needs one."""
    order = [f.finding_id for f in shown] or [f.finding_id for f in session.findings]
    start = order.index(current_id) + 1 if current_id in order else 0
    pending = {f.finding_id for f in session.findings if f.status == "pending"}
    for finding_id in order[start:] + order[:start]:
        if finding_id in pending:
            _set_focus(finding_id)
            return
    _set_focus(current_id)


def _decide(action, session, shown, finding_id, *args, **kwargs) -> None:
    try:
        action(session, finding_id, *args, **kwargs)
    except review.ReviewError as exc:
        st.error(escape_md(str(exc)), icon=":material/error:")
        return
    _advance(session, shown, finding_id)
    st.rerun()


def _use_suggestion(rec_key: str, text: str) -> None:
    st.session_state[rec_key] = text


def _reopen_with_suggestion(session, finding_id: str, reviewer: str, rec_key: str, text: str) -> None:
    try:
        review.reopen(session, finding_id, reviewer)
    except review.ReviewError as exc:
        st.session_state["_flash"] = str(exc)
        return
    st.session_state[rec_key] = text


def _render_queue(session, reviewer: str) -> list:
    run = session.run
    counts = review.counts(session)
    with st.container(border=True):
        present = [value for value in (*COVERAGE_VALUES, "Invalid") if counts["coverage"].get(value)]
        coverage_key, status_key = f"filter_coverage:{run.run_id}", f"filter_status:{run.run_id}"
        coverage_filter = st.session_state.get(coverage_key, present)
        status_filter = st.session_state.get(status_key, list(STATUS_VALUES))
        shown = [f for f in session.findings if f.coverage in coverage_filter and f.status in status_filter]
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown("**Review queue**", width="content")
            filtered = len(shown) < len(session.findings)
            st.caption(f"{len(shown)} of {len(session.findings)}" + (" shown" if filtered else ""), width="content")
            st.space("stretch")
            with st.popover("Filter", icon=":material/filter_list:", type="tertiary" if not filtered else "secondary"):
                st.pills("Coverage", present, selection_mode="multi", default=present,
                         format_func=ui.coverage_option, key=coverage_key)
                st.pills("Status", STATUS_VALUES, selection_mode="multi", default=list(STATUS_VALUES),
                         format_func=ui.status_option, key=status_key)
        st.space("small")  # room for the table's hover toolbar
        focus = _ensure_focus(shown)
        if shown:
            ids = [f.finding_id for f in shown]
            key = f"queue:{run.run_id}:{st.session_state.queue_ver}"
            st.dataframe(
                ui.queue_frame(shown, focus),
                key=key,
                on_select=lambda: _on_queue_select(key, ids),
                selection_mode="single-row",
                hide_index=True,
                column_config=ui.QUEUE_CONFIG,
                height=min(38 + 35 * len(shown), 470),
            )
            st.caption("Select a row to open that finding.")
        else:
            st.caption("No findings match these filters.")

        bulk = review.bulk_approvable(session)
        if bulk and not session.finalized:
            if st.button(f"Approve {plural(len(bulk), 'Full-coverage finding')}", key="bulk_full",
                         icon=":material/done_all:", disabled=not reviewer.strip(),
                         help="Approves findings rated Full that have no flags. Each approval is logged "
                              "under your name. Partial, Missing and flagged findings still need one-by-one review."):
                try:
                    approved = review.approve_all_full(session, reviewer)
                except review.ReviewError as exc:
                    st.error(escape_md(str(exc)))
                else:
                    _advance(session, shown, st.session_state.focus_id)
                    st.session_state["_flash"] = f"Approved {plural(approved, 'Full-coverage finding')}."
                    st.rerun()
    return shown


def _render_decision(backend, session, shown, finding, reviewer: str, key: str) -> None:
    no_reviewer = not reviewer.strip()
    rec_key = f"rec:{key}"
    if session.finalized:
        st.caption(ui.decision_line(finding))
        return

    if finding.status != "pending":
        st.markdown(f":material/history: {ui.decision_line(finding)}")
        if finding.assistant_suggestion and finding.assistant_suggestion != finding.final_recommendation:
            with st.container(border=True):
                st.markdown(":material/auto_awesome: **The assistant suggested new wording**")
                st.markdown(escape_md(finding.assistant_suggestion))
                st.button("Reopen and use this wording", key=f"reuse:{key}", icon=":material/edit_note:",
                          disabled=no_reviewer, on_click=_reopen_with_suggestion,
                          args=(session, finding.finding_id, reviewer, rec_key, finding.assistant_suggestion))
        if st.button("Undo decision", key=f"undo:{key}", type="tertiary", icon=":material/undo:",
                     disabled=no_reviewer):
            try:
                review.reopen(session, finding.finding_id, reviewer)
            except review.ReviewError as exc:
                st.error(escape_md(str(exc)))
            else:
                st.rerun()
        return

    editable = finding.approvable and finding.coverage != "Full"
    if editable:
        if rec_key not in st.session_state:
            st.session_state[rec_key] = finding.recommendation or ""
        suggestion = finding.assistant_suggestion
        if suggestion and st.session_state[rec_key].strip() != suggestion:
            with st.container(border=True):
                st.markdown(":material/auto_awesome: **Suggested by the assistant**")
                st.markdown(escape_md(suggestion))
                st.button("Use this wording", key=f"use:{key}", icon=":material/edit_note:",
                          on_click=_use_suggestion, args=(rec_key, suggestion))
                st.caption("This fills the recommendation below. Nothing changes until you approve.")
        st.text_area("Recommendation to approve", key=rec_key, height=96,
                     max_chars=review.MAX_RECOMMENDATION_CHARS,
                     help="Edit the AI's wording before approving if it needs changes. Edits are recorded in "
                          "the audit trail.")
        if suggestion and st.session_state[rec_key].strip() == suggestion:
            st.caption(":material/auto_awesome: This is the assistant's wording. Approving records that you accepted it.")
    note = st.text_input("Note", key=f"note:{key}", max_chars=review.MAX_NOTE_CHARS,
                         placeholder="Required to reject. Optional when approving.")
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        approve = st.button("Approve", key=f"approve:{key}", type="primary", icon=":material/check:",
                            disabled=no_reviewer or not finding.approvable)
        reject = st.button("Reject", key=f"reject:{key}", icon=":material/close:", disabled=no_reviewer)
        st.space("stretch")
        suggest = st.button("Suggest stronger wording", key=f"suggest:{key}", type="tertiary",
                            icon=":material/auto_awesome:", disabled=not editable,
                            help="Asks the assistant to rewrite this recommendation from the policy text and the "
                                 "NIST control. You decide whether to use it.")
    if approve:
        edited = st.session_state.get(rec_key) if editable else None
        _decide(review.approve, session, shown, finding.finding_id, reviewer,
                edited_recommendation=edited, note=note)
    if reject:
        _decide(review.reject, session, shown, finding.finding_id, reviewer, note)
    if suggest:
        with st.spinner("Asking the assistant for stronger wording"):
            ask_assistant(backend, f"Suggest a stronger, more specific recommendation for finding "
                                   f"{finding.finding_id}.", reviewer)
        st.session_state.inline[finding.finding_id] = len(st.session_state.chat) - 1
        st.rerun()


def _render_inline_assistant(backend, finding, reviewer: str, key: str) -> None:
    index = st.session_state.inline.get(finding.finding_id)
    reply = st.session_state.chat[index] if index is not None and index < len(st.session_state.chat) else None
    with st.expander("Ask the assistant about this finding", icon=":material/forum:", expanded=reply is not None):
        with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
            question = st.text_input("Question", key=f"ask:{key}", label_visibility="collapsed",
                                     max_chars=security.MAX_QUESTION_CHARS,
                                     placeholder="For example: is this requirement covered elsewhere in the policy?")
            ask = st.button("Ask", key=f"askbtn:{key}", icon=":material/send:")
        if ask and question.strip():
            with st.spinner("Asking the assistant"):
                ask_assistant(backend, f"About finding {finding.finding_id}: {question.strip()}", reviewer)
            st.session_state.inline[finding.finding_id] = len(st.session_state.chat) - 1
            st.rerun()
        if reply is not None:
            ui.render_assistant_reply(reply)


def _render_detail(backend, session, shown, reviewer: str) -> None:
    focus_id = st.session_state.focus_id
    if focus_id is None:
        st.info("Select a finding in the queue to review it.")
        return
    finding = session.get(focus_id)
    ids = [f.finding_id for f in shown]
    position = ids.index(focus_id) if focus_id in ids else None
    key = f"{session.run.run_id}:{finding.finding_id}"
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            st.markdown(f"**{finding.finding_id}**", width="content")
            ui.render_finding_badges(finding)
            st.space("stretch")
            if position is not None:
                st.caption(f"{position + 1} of {len(ids)}", width="content")
                if st.button("", key="prev_finding", type="tertiary", icon=":material/chevron_left:",
                             help="Previous finding", disabled=position == 0):
                    _set_focus(ids[position - 1])
                    st.rerun()
                if st.button("", key="next_finding", type="tertiary", icon=":material/chevron_right:",
                             help="Next finding", disabled=position == len(ids) - 1):
                    _set_focus(ids[position + 1])
                    st.rerun()
        ui.render_finding_body(session, finding)
        st.divider()
        _render_decision(backend, session, shown, finding, reviewer, key)
        if not session.finalized:
            _render_inline_assistant(backend, finding, reviewer, key)


def _render_finalize(backend, session, reviewer: str) -> None:
    with st.container(border=True):
        text, action = st.columns([3, 1], vertical_alignment="center")
        ok, reason = review.can_finalize(session, reviewer)
        with text:
            st.markdown("#### Sign off", anchors=False)
            st.caption("Finalizing locks every decision, sends them back to the paused pipeline and produces the "
                       "signed report. Rejected findings stay out of the report; the audit trail keeps all of them.")
            if not ok:
                st.caption(f":material/info: {escape_md(reason)}")
        with action:
            clicked = st.button("Finalize review", type="primary", key="finalize", icon=":material/verified:",
                                disabled=not ok, width="stretch")
    if clicked:
        try:
            errors = analysis.finalize_review(backend, session, reviewer)
        except review.ReviewError as exc:
            st.error(escape_md(str(exc)))
            return
        if errors:
            listed = "\n".join(f"- {escape_md(error)}" for error in errors)
            st.error("Some sections couldn't be resumed, so the review isn't finalized yet. "
                     f"Select Finalize review again to retry.\n\n{listed}")
            return
        go_to(REPORT, "Review finalized. The signed report is ready.")


def page_review(backend, reviewer: str) -> None:
    session = st.session_state.review_session
    if session is None:
        st.info("Nothing to review yet. Analyze a policy first.", icon=":material/fact_check:")
        if st.button("Analyze a policy", key="empty_to_analyze", icon=":material/upload_file:"):
            go_to(ANALYZE)
        return

    run = session.run
    st.markdown(f"#### {escape_md(run.policy_source)}", anchors=False)
    st.caption(f"Analyzed {ui.format_ts(run.finished_at)} by the {escape_md(run.backend_name.lower())}. "
               f"{len(run.sections)} sections, {len(session.findings)} findings.")
    ui.render_posture(review.counts(session))

    if any("placeholder" in name.lower() for name in run.retrieval_backends):
        st.warning("Phase 2 used placeholder search (shared/fake_search.py) because Phase 1's retriever couldn't "
                   "load. NIST matches came from a few sample chunks, not the real index. Install Phase 1's "
                   "requirements and build its index, then analyze again.", icon=":material/warning:")
    if run.problem_sections:
        failed = len(run.section_errors)
        partial = len(run.problem_sections) - failed
        parts = ([f"{failed} section(s) couldn't be analyzed"] if failed else []) + \
                ([f"{partial} section(s) were only partly analyzed"] if partial else [])
        with st.expander(", ".join(parts), icon=":material/warning:"):
            for section in run.problem_sections:
                st.markdown(f"**{escape_md(section.label)}**")
                for line in section.warnings or [section.error]:
                    st.caption(escape_md(line))
            st.caption("Items that failed produced no findings. Re-run those sections after fixing the cause, "
                       "or check them by hand.")
    if session.finalized:
        st.success(f"Signed off by {escape_md(session.finalized_by)} on {ui.format_ts(session.finalized_at)}. "
                   "Decisions are locked.", icon=":material/verified:")
    elif not reviewer.strip():
        st.warning("Enter your name as the reviewer in the sidebar to approve or reject findings.",
                   icon=":material/badge:")

    if not session.findings:
        st.info("The agents found no checkable requirements in the analyzed sections.")
    else:
        queue, detail = st.columns([12, 13], gap="medium")
        with queue:
            shown = _render_queue(session, reviewer)
        with detail:
            _render_detail(backend, session, shown, reviewer)

    if not session.finalized:
        _render_finalize(backend, session, reviewer)


# ====================================================================== report

ORIGIN_LABELS_REPORT = {None: "AI agents", "reviewer": "Edited by reviewer", "assistant": "Assistant's wording, accepted"}


def page_report() -> None:
    session = st.session_state.review_session
    if session is None:
        st.info("Analyze a policy and sign off the review to create a report.", icon=":material/lock:")
        return
    if not session.finalized:
        pending = review.counts(session)["status"]["pending"]
        st.info(f"The report unlocks once the review is finalized. {plural(pending, 'finding')} still "
                f"{'needs' if pending == 1 else 'need'} a decision. Only approved findings go into the report.",
                icon=":material/lock:")
        if st.button("Continue the review", key="report_to_review", icon=":material/fact_check:"):
            go_to(REVIEW)
        return

    report = build_report(session)
    markdown = to_markdown(report)
    stem = ui.report_file_stem(session.run.policy_source, session.finalized_at)
    decisions = report["review"]
    with st.container(border=True):
        text, downloads = st.columns([1, 1], vertical_alignment="center")
        with text:
            st.markdown("#### Signed report", anchors=False)
            st.caption(f"{escape_md(session.run.policy_source)}, signed off by {escape_md(decisions['finalized_by'])} "
                       f"on {ui.format_ts(decisions['finalized_at'])}. {decisions['approved']} approved and "
                       f"{decisions['rejected']} rejected findings. Only approved findings are in the report; "
                       "the audit trail lists every decision.")
            if report["simulated"]:
                st.badge("Simulated data, not a real assessment", color="gray", icon=":material/science:")
        with downloads, st.container(horizontal=True, horizontal_alignment="right", gap="small"):
            st.download_button("Report (Markdown)", markdown, file_name=f"{stem}.md", mime="text/markdown",
                               key="download_md", type="primary", icon=":material/download:", on_click="ignore")
            st.download_button("Data (JSON)", to_json(report), file_name=f"{stem}.json",
                               mime="application/json", key="download_json", icon=":material/data_object:",
                               on_click="ignore")
            st.download_button("Findings (CSV)", to_csv(report), file_name=f"{stem}.csv", mime="text/csv",
                               key="download_csv", icon=":material/table:", on_click="ignore")

    approved = report["approved_findings"]
    st.markdown(f"#### Approved findings ({len(approved)})", anchors=False)
    if approved:
        frame = pd.DataFrame([{
            "ID": f["id"],
            "Coverage": f["coverage"],
            "Control": f["framework_control"] or "Not mapped",
            "Requirement": f["requirement"],
            "Recommendation": f["recommendation"] or "None needed",
            "Wording": ORIGIN_LABELS_REPORT.get(f.get("recommendation_origin"), "AI agents"),
            "Source": ", ".join(str(x) for x in (f["citation"]["locator"], f"p. {f['citation']['page']}"
                                                  if f["citation"]["page"] else None) if x),
        } for f in approved])
        st.dataframe(
            frame.style.map(ui.coverage_cell_css, subset=["Coverage"]),
            hide_index=True,
            column_config={
                "ID": st.column_config.TextColumn(width=56),
                "Coverage": st.column_config.TextColumn(width=104),
                "Control": st.column_config.TextColumn(width=80),
                "Requirement": st.column_config.TextColumn(width=180),
                "Recommendation": st.column_config.TextColumn(width=300),
                "Wording": st.column_config.TextColumn(width=170),
                "Source": st.column_config.TextColumn(width=200),
            },
        )
    else:
        st.caption("No findings were approved.")

    with st.expander(f"Audit trail ({len(report['audit_log'])} events)", icon=":material/history:"):
        st.dataframe(pd.DataFrame([{
            "Time": ui.format_ts(event["timestamp"]),
            "Reviewer": event["reviewer"],
            "Action": event["action"].replace("_", " ").capitalize(),
            "Finding": event["finding_id"] or "All",
            "Details": event["detail"].get("reason") or event["detail"].get("note")
                       or event["detail"].get("suggestion") or "",
        } for event in report["audit_log"]]), hide_index=True)
    with st.expander("Report as it will be downloaded (Markdown)", icon=":material/description:"):
        st.markdown(markdown, anchors=False)


# ====================================================================== assistant page

def _render_suggestion_card(index: int, suggestion: dict) -> None:
    session = st.session_state.review_session
    try:
        finding = session.get(suggestion["finding_id"]) if session else None
    except review.ReviewError:
        finding = None
    if finding is None:
        return
    with st.container(border=True):
        st.markdown(f":material/auto_awesome: **Suggested wording for {finding.finding_id}** "
                    f"({escape_md(finding.requirement)})")
        st.markdown(escape_md(suggestion["recommendation"]))
        if session.finalized:
            st.caption("The review is finalized, so this can't be applied.")
            return
        if st.button(f"Open {finding.finding_id} in review", key=f"open:{index}:{finding.finding_id}",
                     icon=":material/open_in_new:"):
            review.record_suggestion(session, finding.finding_id, suggestion["recommendation"],
                                     st.session_state.get("reviewer_name", ""))
            _set_focus(finding.finding_id)
            go_to(REVIEW)
        state = "It's waiting on the finding for your decision." if finding.status == "pending" else (
            f"{finding.finding_id} is already {finding.status}; you can reopen it from the review to use this.")
        st.caption(state)


def _render_chat_message(index: int, message: dict) -> None:
    avatar = ":material/person:" if message["role"] == "user" else ":material/smart_toy:"
    with st.chat_message(message["role"], avatar=avatar):
        if message["role"] == "user":
            st.markdown(escape_md(message["content"]))
            return
        ui.render_assistant_reply(message)
        for suggestion in message.get("suggestions") or []:
            _render_suggestion_card(index, suggestion)


def page_assistant(backend, reviewer: str) -> None:
    session = st.session_state.review_session
    conversation, side = st.columns([5, 2], gap="large")
    with side, st.container(border=True):
        st.markdown("#### How the assistant works", anchors=False)
        st.caption("It answers only from your policies, the NIST catalogs and any environment evidence, and "
                   "numbers its sources so you can check them. It can read the findings in your current review.")
        st.caption("When it suggests new wording for a finding, the suggestion waits on that finding until you "
                   "use it. It never changes a finding or a decision by itself.")
        if not backend.simulated:
            st.caption("Uploaded PDFs aren't in the search index, but the assistant still sees the passages "
                       "behind your current findings.")
    with conversation:
        prompts = PROMPTS_GENERAL + (PROMPTS_REVIEW if session is not None else ())
        picked = None
        st.caption("Try asking")
        with st.container(horizontal=True, wrap=True, gap="small"):
            for n, prompt in enumerate(prompts):
                if st.button(prompt, key=f"prompt:{n}", icon=":material/chat_bubble:"):
                    picked = prompt
        for index, message in enumerate(st.session_state.chat):
            _render_chat_message(index, message)
        if not st.session_state.chat:
            st.caption("Pick a question above or type your own below.")
        typed = st.chat_input("Ask about a policy, a NIST control or a finding", key="chat_input",
                              max_chars=security.MAX_QUESTION_CHARS)
    question = (picked or typed or "").strip()
    if question:
        with st.spinner("Asking the assistant"):
            ask_assistant(backend, question, reviewer)
        st.rerun()


# ====================================================================== main

def main() -> None:
    init_state()
    if "_flash" in st.session_state:
        st.toast(escape_md(st.session_state.pop("_flash")), icon=":material/info:")

    backend, reviewer = render_sidebar()
    if backend is None:
        st.markdown("## Compliance Copilot", anchors=False)
        st.error("The selected data source isn't available. Fix the problem shown in the sidebar, "
                 "or switch to the simulator.")
        return

    ui.tighten_page()
    ui.render_header(backend, reviewer)
    st.segmented_control("Workflow", PAGES, key="nav", required=True, format_func=NAV_LABELS.get,
                         label_visibility="collapsed")

    page = st.session_state.nav
    if page == ANALYZE:
        page_analyze(backend)
    elif page == REVIEW:
        page_review(backend, reviewer)
    elif page == REPORT:
        page_report()
    else:
        page_assistant(backend, reviewer)


main()
