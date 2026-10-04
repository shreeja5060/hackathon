"""Compliance Copilot: the Phase 3 dashboard.

Start it from this folder:

    streamlit run app.py

Data comes from a backend (see backends/): the simulator by default, or the
real Phase 1 + Phase 2 code once those branches are merged.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path

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

st.set_page_config(page_title="Compliance Copilot", page_icon=":material/policy:", layout="wide")
log = logging.getLogger("compliance_copilot")

PAGES = ("Analyze policy", "Review findings", "Export report", "Ask the docs")
ANALYZE, REVIEW, EXPORT, ASK = PAGES
MODE_LABELS = {"simulated": "Simulator (fake Phase 2 data)", "live": "Live pipeline (Phase 1 + 2 code)"}
ORIGIN_LABELS = {"upload": "Uploaded file", "indexed": "From Phase 1's index", "sample": "Sample policy"}
SOURCE_OPTIONS = ("Upload a PDF", "Choose a loaded policy")


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
    # Navigation requested by the previous run (a widget's state can only be
    # changed before that widget is drawn).
    if "_goto" in ss:
        ss.nav = ss.pop("_goto")


def reset_work() -> None:
    st.session_state.doc = None
    st.session_state.doc_key = None
    st.session_state.review_session = None
    st.session_state.chat = []


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


# ====================================================================== sidebar

def render_sidebar():
    with st.sidebar:
        st.header("Compliance Copilot")
        st.caption("Policy-to-NIST gap analysis with a human sign-off.")
        st.radio("Data source", MODES, format_func=MODE_LABELS.get, key="mode", on_change=reset_work)
        reviewer = st.text_input(
            "Your name (reviewer)",
            key="reviewer_name",
            max_chars=review.MAX_REVIEWER_CHARS,
            placeholder="e.g. Mahsa Sheikhi",
            help="Required to approve or reject findings. Every decision is logged under this name.",
        )
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

        if backend.simulated:
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
    return backend, reviewer


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


def _render_document(doc) -> None:
    with st.container(border=True):
        pages = {chunk.get("page") for chunk in doc.chunks if chunk.get("page") is not None}
        st.markdown(f"**{escape_md(doc.source)}**")
        st.caption(f"{ORIGIN_LABELS.get(doc.origin, doc.origin)}. "
                   f"{len(doc.chunks)} sections across {len(pages)} page(s).")
        for note in doc.notes:
            st.caption(escape_md(note))
        flagged = security.scan_document(doc.chunks, doc.chunk_label)
        if flagged:
            listed = "; ".join(f"{escape_md(label)} ({', '.join(reasons)})" for label, reasons in flagged)
            st.warning(
                f"Possible prompt-injection text: {listed}. The agents should treat this text as data, "
                "not instructions. Check findings from these sections with extra care.",
                icon=":material/gpp_maybe:",
            )
        with st.expander("Preview the sections"):
            for chunk in doc.chunks:
                page = f", page {chunk['page']}" if chunk.get("page") else ""
                st.markdown(f"**{escape_md(doc.chunk_label(chunk))}**{page}")
                st.markdown(escape_md_block_preview(chunk["text"]))


def escape_md_block_preview(text: str, limit: int = 1500) -> str:
    return security.escape_md_block(text if len(text) <= limit else text[:limit] + "…")


def _run_analysis(backend, doc, chunk_ids):
    progress = st.progress(0.0, text="Starting the agents")
    with st.status("Running Extractor, Mapper and Auditor on each section", expanded=True) as status:
        def on_progress(done, total, result):
            progress.progress(done / total, text=f"{done} of {total} sections analyzed")
            label = escape_md(result.label)
            if result.status == "ok":
                status.write(f":material/check: {label}: {result.finding_count} finding(s)")
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
    captions = ("", "Sample policies in the simulator" if backend.simulated else "Policies Phase 1 has indexed")
    origin = st.radio("Policy", SOURCE_OPTIONS, captions=captions, horizontal=True, key="policy_origin")

    doc = None
    if origin == SOURCE_OPTIONS[0]:
        upload = st.file_uploader(
            "Policy PDF",
            type=["pdf"],
            key="policy_upload",
            max_upload_size=security.MAX_UPLOAD_MB,
            help=f"A PDF with selectable text, up to {security.MAX_UPLOAD_MB} MB and {security.MAX_PDF_PAGES} pages.",
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

    _render_document(doc)

    labels = {chunk["chunk_id"]: doc.chunk_label(chunk) for chunk in doc.chunks}
    selected = st.multiselect(
        "Sections to analyze",
        list(labels),
        default=list(labels),
        format_func=labels.get,
        key=f"sections:{hashlib.sha256(st.session_state.doc_key.encode()).hexdigest()[:12]}",
    )
    if not backend.simulated:
        st.caption(f"{len(selected)} sections selected. Each section makes one Extractor call to Claude, "
                   "plus a Mapper and an Auditor call for every requirement it finds.")

    current = st.session_state.review_session
    unsaved = current is not None and not current.finalized and any(f.status != "pending" for f in current.findings)
    confirmed = True
    if unsaved:
        confirmed = st.checkbox("Discard my current review decisions and start a new analysis", key="confirm_discard")

    if st.button("Run analysis", type="primary", key="run_analysis", icon=":material/play_arrow:",
                 disabled=not selected or not confirmed):
        session = _run_analysis(backend, doc, selected)
        if session is not None and session.run.ok_threads:
            st.session_state.review_session = session
            failed = len(session.run.section_errors)
            message = f"{len(session.findings)} findings are waiting for your review."
            if failed:
                message += f" {failed} section(s) failed."
            go_to(REVIEW, message)


# ====================================================================== review

def _render_finalize(backend, session, reviewer: str) -> None:
    st.divider()
    st.subheader("Finalize the review")
    st.caption("Finalizing locks every decision, hands the approved findings back to the paused pipeline, "
               "and unlocks the report. Rejected findings stay out of the report.")
    ok, reason = review.can_finalize(session, reviewer)
    if st.button("Finalize review", type="primary", key="finalize", icon=":material/verified:", disabled=not ok):
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
        go_to(EXPORT, "Review finalized. Your report is ready.")
    if not ok:
        st.caption(escape_md(reason))


def page_review(backend, reviewer: str) -> None:
    session = st.session_state.review_session
    if session is None:
        st.info("Nothing to review yet. Analyze a policy first.")
        return

    run = session.run
    st.markdown(f"**{escape_md(run.policy_source)}**")
    st.caption(f"Analyzed {ui.format_ts(run.finished_at)} by the {escape_md(run.backend_name.lower())}. "
               f"{len(run.sections)} sections, {len(session.findings)} findings.")

    counts = review.counts(session)
    ui.render_coverage_summary(counts)
    ui.render_review_progress(counts)

    if run.section_errors:
        with st.expander(f"{len(run.section_errors)} section(s) couldn't be analyzed", icon=":material/warning:"):
            for section in run.section_errors:
                st.markdown(f"**{escape_md(section.label)}**: {escape_md(section.error)}")
            st.caption("These sections produced no findings. Re-run them after fixing the cause, or check them by hand.")

    if session.finalized:
        st.success(f"Finalized by {escape_md(session.finalized_by)} on {ui.format_ts(session.finalized_at)}. "
                   "The report is ready on the Export report page.", icon=":material/verified:")
    elif not reviewer.strip():
        st.warning("Enter your name in the sidebar to approve or reject findings.", icon=":material/badge:")

    if not session.findings:
        st.info("The agents found no checkable requirements in the analyzed sections.")
    else:
        present = [value for value in (*COVERAGE_VALUES, "Invalid") if counts["coverage"].get(value)]
        coverage_filter = st.pills("Coverage", present, selection_mode="multi", default=present,
                                   format_func=ui.coverage_option, key=f"filter_coverage:{run.run_id}")
        status_filter = st.pills("Status", STATUS_VALUES, selection_mode="multi", default=list(STATUS_VALUES),
                                 format_func=ui.status_option, key=f"filter_status:{run.run_id}")
        shown = [f for f in session.findings if f.coverage in coverage_filter and f.status in status_filter]

        st.dataframe(ui.findings_frame(shown), hide_index=True, column_config=ui.TABLE_CONFIG)
        st.subheader("Decide on each finding")
        if not shown:
            st.caption("No findings match these filters.")
        for finding in shown:
            ui.render_finding_card(session, finding, reviewer)

    if not session.finalized:
        _render_finalize(backend, session, reviewer)


# ====================================================================== export

def page_export() -> None:
    session = st.session_state.review_session
    if session is None:
        st.info("Analyze a policy and finalize the review to create a report.")
        return
    if not session.finalized:
        pending = review.counts(session)["status"]["pending"]
        st.info(f"The report unlocks once the review is finalized. {pending} finding(s) still need a decision. "
                "Only approved findings go into the report.")
        return

    report = build_report(session)
    markdown = to_markdown(report)
    stem = ui.report_file_stem(session.run.policy_source, session.finalized_at)
    with st.container(horizontal=True, gap="small"):
        st.download_button("Download report (Markdown)", markdown, file_name=f"{stem}.md", mime="text/markdown",
                           key="download_md", type="primary", icon=":material/download:", on_click="ignore")
        st.download_button("Download data (JSON)", to_json(report), file_name=f"{stem}.json",
                           mime="application/json", key="download_json", icon=":material/data_object:",
                           on_click="ignore")
        st.download_button("Download findings (CSV)", to_csv(report), file_name=f"{stem}.csv", mime="text/csv",
                           key="download_csv", icon=":material/table:", on_click="ignore")
    with st.container(border=True):
        st.markdown(markdown)


# ====================================================================== ask

def _answer(backend, question: str) -> dict:
    try:
        answer = normalize_chat_answer(backend.ask(question))
    except (BackendUnavailable, ContractError, ValueError) as exc:
        return {"role": "assistant", "content": f"I couldn't answer that: {exc}", "error": True}
    except Exception:  # noqa: BLE001 - the Q&A agent is outside code; never crash the page
        log.exception("Q&A failed")
        return {"role": "assistant", "content": "The Q&A agent failed. The terminal has the details.", "error": True}
    return {"role": "assistant", "content": answer.answer, "citations": list(answer.citations),
            "dropped": answer.dropped_citations}


def page_ask(backend) -> None:
    st.caption("Ask about the loaded policies and NIST controls. Answers list their sources, so check them "
               "before relying on an answer.")
    if not backend.simulated:
        st.caption("Live mode searches Phase 1's index. Uploaded PDFs aren't in the index yet.")
    for message in st.session_state.chat:
        ui.render_chat_message(message)
    question = st.chat_input("Ask a question, such as: Can staff share passwords?", key="chat_input",
                             max_chars=security.MAX_QUESTION_CHARS)
    if question and question.strip():
        st.session_state.chat.append({"role": "user", "content": question.strip()})
        st.session_state.chat.append(_answer(backend, question.strip()))
        st.rerun()


# ====================================================================== main

def main() -> None:
    init_state()
    if "_flash" in st.session_state:
        st.toast(st.session_state.pop("_flash"), icon=":material/info:")

    backend, reviewer = render_sidebar()
    st.title("Compliance Copilot")
    if backend is None:
        st.error("The selected data source isn't available. Fix the problem shown in the sidebar, "
                 "or switch to the simulator.")
        return

    st.caption("Compare a security policy with NIST SP 800-53, decide on each AI finding, "
               "and export a report that contains only the findings you approved.")
    if backend.simulated:
        st.info("Simulator mode: findings come from local keyword rules in the same format as the Phase 2 "
                "agents. They aren't real compliance results.", icon=":material/science:")
    st.segmented_control("Page", PAGES, key="nav", required=True, label_visibility="collapsed")

    page = st.session_state.nav
    if page == ANALYZE:
        page_analyze(backend)
    elif page == REVIEW:
        page_review(backend, reviewer)
    elif page == EXPORT:
        page_export()
    else:
        page_ask(backend)


main()
