"""Compliance Copilot: the Phase 3 dashboard.

Start it from this folder (so .streamlit/config.toml and the fonts load):

    streamlit run app.py

Data comes from a backend (see backends/): the simulator by default, or the
real Phase 1 + Phase 2 code with COPILOT_BACKEND=live. Reviews are kept in the
permanent ledger (core/memory.py over shared/ledger.py), so they survive the
browser session and a server restart.

The user flow:

    Library ........ every policy the organization has analyzed: versions, reviews,
                     who decided what, overlaps between policies, the audit trail
    Analyze ........ upload a policy (or pick one, or a starter draft) and run the agents
    Review ......... approve, edit or reject each finding; a second person confirms
    Sign-off ....... the signed report and its exports
    Assistant ...... questions about policies, controls and findings; suggested wording
    Policy writer .. a starter policy for an organization that has none yet
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
from phase3_dashboard.backends.base import PolicyDocument  # noqa: E402
from phase3_dashboard.core import analysis, identity, memory, overlaps, review, security  # noqa: E402
from phase3_dashboard.core.contracts import COVERAGE_VALUES, STATUS_VALUES, ContractError, normalize_chat_answer  # noqa: E402
from phase3_dashboard.core.report import build_report, to_csv, to_json, to_markdown  # noqa: E402
from phase3_dashboard.core.security import escape_md  # noqa: E402
from phase3_dashboard.ui import components as ui  # noqa: E402
from policy_writer import catalog as writer_catalog  # noqa: E402
from policy_writer import writer as policy_writer  # noqa: E402

st.set_page_config(page_title="Compliance Copilot", page_icon=":material/shield:", layout="wide")
log = logging.getLogger("compliance_copilot")

PAGES = ("Library", "Analyze", "Review", "Report", "Assistant", "Writer")
LIBRARY, ANALYZE, REVIEW, REPORT, ASSISTANT, WRITER = PAGES
MODE_LABELS = {"simulated": "Simulator (no AI calls)", "live": "Live pipeline"}
ORIGIN_LABELS = {"upload": "Uploaded file", "indexed": "From the Phase 1 index", "sample": "Sample policy",
                 "drafted": "Starter policy drafted here"}
SOURCE_OPTIONS = ("Upload a PDF", "Choose a loaded policy", "Starter policy draft")
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
_OFF = {"off", "0", "false", "no"}
KEEP_WIDGET_STATE = ("policy_origin", "loaded_policy", "writer_org", "writer_size", "writer_sector", "writer_it",
                     "writer_uses", "writer_topics")


def two_person_rule() -> bool:
    """COPILOT_TWO_PERSON=off allows one reviewer to decide and sign off alone (on by default)."""
    return os.getenv("COPILOT_TWO_PERSON", "on").strip().lower() not in _OFF


# ====================================================================== state

def init_state() -> None:
    ss = st.session_state
    ss.setdefault("mode", default_mode())
    ss.setdefault("backends", {})
    ss.setdefault("doc", None)
    ss.setdefault("doc_key", None)
    ss.setdefault("doc_info", None)  # what the ledger knows about the loaded document
    ss.setdefault("review_session", None)
    ss.setdefault("chat", [])
    ss.setdefault("focus_id", None)
    ss.setdefault("queue_ver", 0)
    ss.setdefault("inline", {})  # finding_id -> index of its latest assistant reply in chat
    ss.setdefault("writer_draft", None)
    ss.setdefault("writer_size", "1-10")
    ss.setdefault("writer_sector", "Other")
    ss.setdefault("writer_it", "owner")
    ss.setdefault("writer_uses", [])
    # Navigation requested by the previous run (a widget's state can only be
    # changed before that widget is drawn).
    if "_goto" in ss:
        ss.nav = ss.pop("_goto")
    if "_origin" in ss:
        ss.policy_origin = ss.pop("_origin")
    if "_writer_topics" in ss:
        ss.writer_topics = ss.pop("_writer_topics")
    # Streamlit forgets a widget's value when its page isn't shown. Reassigning keeps
    # the policy source and the writer's answers while the user moves between pages.
    for key in KEEP_WIDGET_STATE:
        if key in ss:
            ss[key] = ss[key]


def reset_work() -> None:
    ss = st.session_state
    ss.doc = None
    ss.doc_key = None
    ss.doc_info = None
    ss.review_session = None
    ss.chat = []
    ss.focus_id = None
    ss.inline = {}
    ss.writer_draft = None
    ss.pop("_stale", None)
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


def _sim_failing_section() -> int | None:
    """Developer option: COPILOT_SIM_FAIL_SECTION=1 makes the simulator's second section fail
    like a malformed model reply, to exercise the error handling. Not shown in the UI."""
    value = os.getenv("COPILOT_SIM_FAIL_SECTION", "").strip()
    return int(value) if value.isdigit() else None


def get_backend(mode: str):
    cache = st.session_state.backends
    if mode not in cache:  # failures aren't cached, so fixing the cause and rerunning works
        options = ({"delay": _sim_delay(), "fail_section_index": _sim_failing_section()}
                   if mode == "simulated" else {})
        cache[mode] = create_backend(mode, **options)
    return cache[mode]


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _set_focus(finding_id: str | None) -> None:
    """Open a finding from code; a fresh queue key clears the table's old row selection."""
    st.session_state.focus_id = finding_id
    st.session_state.queue_ver += 1


def _lower_first(text: str) -> str:
    """'Signed off by Shreeja' -> 'signed off by Shreeja' (names keep their capitals)."""
    return text[:1].lower() + text[1:]


def _reviewer() -> str:
    return " ".join(str(st.session_state.get("reviewer_name") or "").split())


# ====================================================================== permanent memory

@st.cache_resource(show_spinner=False)
def _open_memory(ledger_path: str, documents_dir: str) -> memory.Memory:
    return memory.Memory(ledger_path, documents_dir)


def get_memory(simulated: bool) -> memory.Memory | None:
    """The ledger for the simulator or the live pipeline (kept apart), or None when memory is off."""
    if not memory.enabled():
        return None
    ledger_path, documents_dir = memory.default_paths(simulated)
    try:
        return _open_memory(str(ledger_path), str(documents_dir))
    except memory.MemoryUnavailable as exc:
        st.session_state["_memory_error"] = str(exc)
        return None


def _session_memory(session) -> memory.Memory | None:
    if session is None or session.ledger_run_id is None:
        return None
    return get_memory(session.run.simulated)


def _guard(session) -> bool:
    """Before changing a stored review, make sure nobody changed it in another session meanwhile."""
    mem = _session_memory(session)
    if mem is None:
        return True
    try:
        mem.check_fresh(session)
    except memory.StaleReview as exc:
        st.session_state["_stale"] = str(exc)
        return False
    return True


def _persist(session) -> None:
    """Write the review's new actions to the ledger. Never loses a decision: on failure it retries next time."""
    mem = _session_memory(session)
    if mem is None:
        return
    try:
        mem.sync(session)
    except memory.StaleReview as exc:
        st.session_state["_stale"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - the decision stays in the session and is retried
        log.exception("Saving to the ledger failed")
        st.session_state["_memory_warning"] = (f"Couldn't save to the library just now ({type(exc).__name__}). "
                                               "Your decision is kept here and will be saved with the next one.")


def _remember_document(backend, document, content: bytes | None, extension: str = ".pdf") -> None:
    st.session_state.doc_info = None
    mem = get_memory(backend.simulated)
    if mem is None:
        return
    try:
        st.session_state.doc_info = mem.register(document, content, _reviewer() or None, extension)
    except Exception as exc:  # noqa: BLE001 - the policy can still be analyzed, just not kept
        log.exception("Registering %s failed", document.source)
        st.session_state["_memory_warning"] = f"This policy couldn't be added to the library ({type(exc).__name__})."


def _open_review(simulated: bool, run_id: int, message: str | None = None) -> None:
    mem = get_memory(simulated)
    if mem is None:
        st.error("The library isn't available, so the review can't be reopened.")
        return
    try:
        session = mem.restore(run_id)
    except Exception as exc:  # noqa: BLE001 - show it, don't crash the page
        log.exception("Reopening review %s failed", run_id)
        st.error(f"That review couldn't be reopened ({escape_md(type(exc).__name__)}).")
        return
    if session.run.simulated != (st.session_state.mode == "simulated"):
        st.session_state["_goto_mode"] = "simulated" if session.run.simulated else "live"
    st.session_state.review_session = session
    st.session_state.chat = []
    st.session_state.inline = {}
    st.session_state.pop("_stale", None)
    first = next((f.finding_id for f in session.findings if f.status == "pending"), None) or \
        next((f.finding_id for f in review.awaiting_confirmation(session)), None) or \
        next((f.finding_id for f in session.findings), None)
    _set_focus(first)
    go_to(REPORT if session.finalized else REVIEW,
          message or f"Reopened the review of {session.run.policy_source} from the library.")


# ====================================================================== sidebar

def render_sidebar():
    with st.sidebar:
        st.markdown("### :material/shield: Compliance Copilot", anchors=False)
        st.caption("AI drafts the findings. People sign them off.")
        signed_in = identity.from_headers(getattr(st.context, "headers", None))
        if signed_in:
            st.session_state.reviewer_name = signed_in
        reviewer = st.text_input(
            "Reviewer",
            key="reviewer_name",
            max_chars=review.MAX_REVIEWER_CHARS,
            placeholder="Your full name",
            disabled=identity.required(),
            help="Required to approve, reject or confirm findings. Every decision is logged under this name.",
        )
        if identity.required():
            st.caption(":material/badge: Signed in with your company account." if signed_in else
                       ":material/no_accounts: Sign-in required: open the dashboard through your company's "
                       "sign-in page to make decisions.")
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

        st.divider()
        mem = get_memory(backend.simulated)
        if mem is not None:
            where = "simulator records, kept apart from real ones" if backend.simulated else "the permanent ledger"
            st.caption(f":material/database: Reviews are saved to {where}.")
        elif memory.enabled():
            st.warning(escape_md(st.session_state.get("_memory_error") or "The library isn't available."),
                       icon=":material/database_off:")
        else:
            st.caption(":material/database_off: Memory is off: reviews last only as long as this session.")
        if two_person_rule():
            st.caption(":material/group: Two-person rule: a second reviewer confirms every decision.")
        if st.button("Start over", key="start_over", icon=":material/restart_alt:",
                     help="Clears the current policy, review, chat and draft. The library keeps everything saved."):
            reset_work()
            go_to(ANALYZE)
    return backend, reviewer


# Labels must not change between runs: the browser sends back the label it
# showed, so a count in a label would make the selection jump.
NAV_LABELS = {
    LIBRARY: ":material/folder_open: Library",
    ANALYZE: ":material/upload_file: Analyze",
    REVIEW: ":material/fact_check: Review",
    REPORT: ":material/verified: Sign-off",
    ASSISTANT: ":material/forum: Assistant",
    WRITER: ":material/edit_note: Policy writer",
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
            st.session_state.doc_info = None
            st.error(escape_md(str(exc)), icon=":material/error:")
        else:
            _remember_document(backend, st.session_state.doc, data)
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
        _remember_document(backend, st.session_state.doc, None)
    return st.session_state.doc


def _draft_document(draft) -> PolicyDocument:
    return PolicyDocument(source=draft.source_name, chunks=policy_writer.to_chunks(draft), origin="drafted",
                          notes=["Starter policy drafted on the Policy writer page. It is analyzed without its "
                                 "NIST control tags, so the check judges the wording alone."])


def _load_draft(backend, draft):
    markdown = policy_writer.to_markdown(draft).encode("utf-8")
    key = f"{st.session_state.mode}:drafted:{hashlib.sha256(markdown).hexdigest()}"
    if st.session_state.doc_key != key:
        st.session_state.doc = _draft_document(draft)
        st.session_state.doc_key = key
        _remember_document(backend, st.session_state.doc, markdown, extension=".md")
    return st.session_state.doc


def _document_summary(doc) -> None:
    pages = {chunk.get("page") for chunk in doc.chunks if chunk.get("page") is not None}
    st.markdown(f"**{escape_md(doc.source)}**")
    where = f" across {plural(len(pages), 'page')}" if pages else ""
    st.caption(f"{ORIGIN_LABELS.get(doc.origin, doc.origin)}. {plural(len(doc.chunks), 'section')}{where}.")
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


def _render_version_note(backend, info) -> None:
    """What the library already knows about this exact document."""
    if not info:
        return
    if info["already_seen"]:
        runs = info.get("runs") or []
        text = f":material/history: Already in the library as version {info['version']}"
        if runs:
            mem = get_memory(backend.simulated)
            latest = mem.run_summary(runs[-1]["id"]) if mem else None
            text += f", analyzed {plural(len(runs), 'time')}"
            if latest:
                text += f". Latest review: {escape_md(_lower_first(latest['label']))}"
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            st.caption(text + ".", width="content")
            if runs and st.button("Open the latest review", key="open_latest", type="tertiary",
                                  icon=":material/folder_open:",
                                  help="Reopen the stored review instead of analyzing the same file again."):
                _open_review(backend.simulated, runs[-1]["id"])
    elif info["is_new_version"]:
        previous = info.get("previous_summary")
        text = f":material/difference: Version {info['version']} of this policy."
        if previous:
            text += (f" Version {info['previous_version']} had {plural(previous['findings'], 'finding')} "
                     f"({previous['gaps']} gaps) and is {escape_md(_lower_first(previous['label']))}. "
                     "After the analysis, the review shows what changed and what was decided before.")
        st.caption(text)
    else:
        st.caption(":material/new_releases: New to the library. It will be kept as version 1, with every finding "
                   "and decision.")


def _section_picker(doc) -> list[str]:
    """A table of the policy's sections with a checkbox for each; returns the chosen chunk IDs."""
    has_pages = any(chunk.get("page") is not None for chunk in doc.chunks)
    rows = [{
        "Analyze": True,
        "Section": doc.chunk_label(chunk),
        **({"Page": chunk.get("page")} if has_pages else {}),
        "Words": len(chunk["text"].split()),
        "Flag": "Possible prompt injection" if security.scan_for_injection(chunk["text"]) else "",
    } for chunk in doc.chunks]
    edited = st.data_editor(
        pd.DataFrame(rows),
        key=f"sections:{hashlib.sha256(st.session_state.doc_key.encode()).hexdigest()[:12]}",
        hide_index=True,
        disabled=["Section", "Page", "Words", "Flag"] if has_pages else ["Section", "Words", "Flag"],
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
        steps = (
            "1. **Extractor** reads each section and lists the requirements someone could check.\n"
            "2. **Mapper** matches each requirement to a NIST SP 800-53 control from the official OSCAL catalog.\n"
            "3. **Auditor** rates coverage as Full, Partial, Missing or Not observable, explains it in plain "
            "words and drafts a fix.\n"
            "4. **You** approve, edit or reject every finding. The pipeline stays paused until you sign off, "
            "and only approved findings reach the report."
        )
        if two_person_rule():
            steps += "\n5. **A second reviewer** confirms each decision before the review can be signed off."
        st.markdown(steps)
        st.caption(":material/database: Every step is kept in the library: the document version, each finding, "
                   "and who decided what and when, in a tamper-evident trail.")
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


def _record_analysis(backend, session) -> None:
    mem = get_memory(backend.simulated)
    info = st.session_state.get("doc_info")
    if mem is None or not info:
        return
    try:
        mem.record_analysis(session, info, _reviewer() or None, backend.model_description)
    except Exception as exc:  # noqa: BLE001 - the review still works; it just isn't kept
        log.exception("Recording the analysis failed")
        st.session_state["_memory_warning"] = (f"The analysis couldn't be saved to the library "
                                               f"({type(exc).__name__}). You can still review it here.")


def page_analyze(backend) -> None:
    main, side = st.columns([3, 2], gap="large")
    with side:
        _render_how_it_works(backend)
    with main:
        st.markdown("#### Policy to review", anchors=False)
        captions = ("PDF with selectable text",
                    "Sample policies in the simulator" if backend.simulated else "Policies in the Phase 1 index",
                    "From the Policy writer")
        origin = st.radio("Policy source", SOURCE_OPTIONS, captions=captions, horizontal=True,
                          key="policy_origin", label_visibility="collapsed")

        doc = None
        if origin == SOURCE_OPTIONS[0]:
            upload = st.file_uploader(
                "Policy PDF",
                type=["pdf"],
                key="policy_upload",
                max_upload_size=security.MAX_UPLOAD_MB,
                help=f"Up to {security.MAX_UPLOAD_MB} MB and {security.MAX_PDF_PAGES} pages. Checked in memory; "
                     "kept in the library's document store when memory is on.",
            )
            if upload is not None:
                doc = _load_upload(backend, upload)
        elif origin == SOURCE_OPTIONS[1]:
            choices = backend.list_policies()
            if not choices:
                st.info("No indexed policies yet. Run the Phase 1 scripts so data/processed/policy_chunks.json "
                        "exists, or upload a PDF.")
            else:
                doc = _load_existing(backend, st.selectbox("Loaded policy", choices, key="loaded_policy"))
        else:
            draft = st.session_state.writer_draft
            if draft is None:
                st.info("No starter policy drafted yet. The Policy writer drafts one from a few questions about "
                        "the organization.", icon=":material/edit_note:")
                if st.button("Open the Policy writer", key="analyze_to_writer", icon=":material/edit_note:"):
                    go_to(WRITER)
            else:
                doc = _load_draft(backend, draft)
        if doc is None:
            return

        with st.container(border=True):
            _document_summary(doc)
            _render_version_note(backend, st.session_state.doc_info)
            selected = _section_picker(doc)

        current = st.session_state.review_session
        unsaved = (current is not None and not current.finalized and current.ledger_run_id is None
                   and any(f.status != "pending" for f in current.findings))
        confirmed = True
        if unsaved:
            confirmed = st.checkbox("Discard my current review decisions and start a new analysis",
                                    key="confirm_discard")

        if st.button(f"Run analysis on {plural(len(selected), 'section')}", type="primary", key="run_analysis",
                     icon=":material/play_arrow:", disabled=not selected or not confirmed):
            session = _run_analysis(backend, doc, selected)
            if session is not None and session.run.ok_threads:
                session.two_person = two_person_rule()
                _record_analysis(backend, session)
                st.session_state.review_session = session
                st.session_state.inline = {}
                st.session_state.pop("_stale", None)
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
        if answer.suggestions:
            _persist(session)
    st.session_state.chat.append({"role": "user", "content": question})
    st.session_state.chat.append(reply)
    return reply


# ====================================================================== review

def _needs_me(session, finding, reviewer: str) -> bool:
    """Pending findings, and (two-person rule) decisions someone else made that this reviewer can confirm."""
    if finding.status == "pending":
        return True
    return (session.two_person and not finding.confirmed and bool(reviewer)
            and not review.same_person(reviewer, finding.reviewer))


def _ensure_focus(session, shown, reviewer: str) -> str | None:
    ids = [f.finding_id for f in shown]
    focus = st.session_state.focus_id
    if focus not in ids:
        focus = next((f.finding_id for f in shown if _needs_me(session, f, reviewer)), ids[0] if ids else None)
        st.session_state.focus_id = focus
    return focus


def _on_queue_select(key: str, ids: list[str]) -> None:
    state = st.session_state.get(key)
    rows = state.selection.rows if state is not None else []
    if rows and rows[0] < len(ids):
        st.session_state.focus_id = ids[rows[0]]


def _advance(session, shown, current_id: str, reviewer: str = "") -> None:
    """After a decision, open the next finding that still needs this reviewer."""
    order = [f.finding_id for f in shown] or [f.finding_id for f in session.findings]
    start = order.index(current_id) + 1 if current_id in order else 0
    waiting = {f.finding_id for f in session.findings if _needs_me(session, f, reviewer)}
    for finding_id in order[start:] + order[:start]:
        if finding_id in waiting:
            _set_focus(finding_id)
            return
    _set_focus(current_id)


def _decide(action, session, shown, finding_id, *args, **kwargs) -> None:
    if not _guard(session):
        st.rerun()
    try:
        action(session, finding_id, *args, **kwargs)
    except review.ReviewError as exc:
        st.error(escape_md(str(exc)), icon=":material/error:")
        return
    _persist(session)
    _advance(session, shown, finding_id, _reviewer())
    st.rerun()


def _use_suggestion(rec_key: str, text: str) -> None:
    st.session_state[rec_key] = text


def _reopen_with_suggestion(session, finding_id: str, reviewer: str, rec_key: str, text: str) -> None:
    if not _guard(session):
        return
    try:
        review.reopen(session, finding_id, reviewer)
    except review.ReviewError as exc:
        st.session_state["_flash"] = str(exc)
        return
    _persist(session)
    st.session_state[rec_key] = text


def _render_stale_notice(session) -> None:
    """Tell the reviewer when someone else has changed this review since this page read it."""
    if session is None or session.ledger_run_id is None:
        return
    message = st.session_state.get("_stale")
    refused = bool(message)
    if not message:
        mem = _session_memory(session)
        try:
            if mem is not None:
                mem.check_fresh(session)
        except memory.StaleReview as exc:
            message = str(exc)
    if not message:
        return
    with st.container(border=True):
        if refused:
            st.error(escape_md(message), icon=":material/sync_problem:")
        else:
            st.warning(escape_md(message), icon=":material/sync:")
        if st.button("Reopen the latest version", key="reload_review", icon=":material/refresh:", type="primary"):
            _open_review(session.run.simulated, session.ledger_run_id, "Reopened the latest version of this review.")


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
        focus = _ensure_focus(session, shown, reviewer)
        if shown:
            ids = [f.finding_id for f in shown]
            key = f"queue:{run.run_id}:{st.session_state.queue_ver}"
            st.dataframe(
                ui.queue_frame(shown, focus, session.two_person),
                key=key,
                on_select=lambda: _on_queue_select(key, ids),
                selection_mode="single-row",
                hide_index=True,
                column_config=ui.QUEUE_CONFIG_TWO_PERSON if session.two_person else ui.QUEUE_CONFIG,
                height=min(38 + 35 * len(shown), 470),
            )
            st.caption("Select a row to open that finding.")
        else:
            st.caption("No findings match these filters.")

        if session.finalized:
            return shown
        bulk = review.bulk_approvable(session)
        if bulk:
            if st.button(f"Approve {plural(len(bulk), 'Full-coverage finding')}", key="bulk_full",
                         icon=":material/done_all:", disabled=not reviewer.strip(),
                         help="Approves findings rated Full that have no flags. Each approval is logged "
                              "under your name. Partial, Missing and flagged findings still need one-by-one review."):
                if not _guard(session):
                    st.rerun()
                try:
                    approved = review.approve_all_full(session, reviewer)
                except review.ReviewError as exc:
                    st.error(escape_md(str(exc)))
                else:
                    _persist(session)
                    _advance(session, shown, st.session_state.focus_id, reviewer)
                    st.session_state["_flash"] = f"Approved {plural(approved, 'Full-coverage finding')}."
                    st.rerun()
        if session.two_person and reviewer.strip():
            confirmable = [f for f in review.awaiting_confirmation(session)
                           if not review.same_person(reviewer, f.reviewer)]
            if confirmable and st.button(
                    f"Confirm {plural(len(confirmable), 'decision')} made by others", key="bulk_confirm",
                    icon=":material/verified_user:",
                    help="Second review: confirms every decision you didn't make yourself. Each confirmation is "
                         "logged under your name. Open a finding to send it back instead."):
                if not _guard(session):
                    st.rerun()
                try:
                    done, _skipped = review.confirm_all(session, reviewer)
                except review.ReviewError as exc:
                    st.error(escape_md(str(exc)))
                else:
                    _persist(session)
                    st.session_state["_flash"] = f"Confirmed {plural(done, 'decision')} as the second reviewer."
                    st.rerun()
    return shown


def _render_second_review(session, shown, finding, reviewer: str, key: str) -> None:
    if finding.confirmed:
        st.markdown(f":material/verified_user: {ui.confirmation_line(finding)}")
        return
    if not session.two_person:
        return
    if not reviewer.strip():
        st.caption(":material/group: Enter your name in the sidebar to confirm this decision as the second reviewer.")
        return
    if review.same_person(reviewer, finding.reviewer):
        st.caption(":material/group: Waiting for a second reviewer. A different person confirms this decision, "
                   "here or after opening the review from the Library.")
        return
    verb = "approved" if finding.status == "approved" else "rejected"
    with st.container(border=True):
        st.markdown(f":material/group: **Second review.** {escape_md(finding.reviewer)} {verb} this finding. "
                    "Confirm the decision, or send it back with a reason.")
        note = st.text_input("Second reviewer's note", key=f"cnote:{key}", max_chars=review.MAX_NOTE_CHARS,
                             placeholder="Required to send back. Optional to confirm.")
        with st.container(horizontal=True, gap="small"):
            confirm = st.button("Confirm decision", key=f"confirm:{key}", type="primary",
                                icon=":material/verified_user:")
            send_back = st.button("Send back", key=f"sendback:{key}", icon=":material/reply:")
    if confirm:
        _decide(review.confirm, session, shown, finding.finding_id, reviewer, note)
    if send_back:
        _decide(review.send_back, session, shown, finding.finding_id, reviewer, note)


def _render_decision(backend, session, shown, finding, reviewer: str, key: str) -> None:
    no_reviewer = not reviewer.strip()
    rec_key = f"rec:{key}"
    if session.finalized:
        st.caption(ui.decision_line(finding))
        if finding.confirmed:
            st.caption(ui.confirmation_line(finding))
        return

    if finding.status != "pending":
        st.markdown(f":material/history: {ui.decision_line(finding)}")
        _render_second_review(session, shown, finding, reviewer, key)
        if finding.assistant_suggestion and finding.assistant_suggestion != finding.final_recommendation:
            with st.container(border=True):
                st.markdown(":material/auto_awesome: **The assistant suggested new wording**")
                st.markdown(escape_md(finding.assistant_suggestion))
                st.button("Reopen and use this wording", key=f"reuse:{key}", icon=":material/edit_note:",
                          disabled=no_reviewer, on_click=_reopen_with_suggestion,
                          args=(session, finding.finding_id, reviewer, rec_key, finding.assistant_suggestion))
        if not session.two_person or review.same_person(reviewer, finding.reviewer):
            if st.button("Undo decision", key=f"undo:{key}", type="tertiary", icon=":material/undo:",
                         disabled=no_reviewer):
                _decide(review.reopen, session, shown, finding.finding_id, reviewer)
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


def _statements_for(session) -> list[dict]:
    """What every stored policy says per control, for the 'other policies' panel (cached per review state)."""
    mem = _session_memory(session)
    if mem is None:
        return []
    cache_key = (session.ledger_run_id, session.ledger_seq, mem.ledger.last_seq())
    cached = st.session_state.get("_statements")
    if cached and cached[0] == cache_key:
        return cached[1]
    try:
        statements = mem.statements()
    except Exception:  # noqa: BLE001 - extra context only
        log.exception("Reading the library for overlaps failed")
        statements = []
    st.session_state["_statements"] = (cache_key, statements)
    return statements


def _render_related(session, finding) -> None:
    if not finding.framework_control or session.ledger_run_id is None:
        return
    focus = {"policy": (session.document_record or {}).get("name") or session.run.policy_source,
             "run_id": session.ledger_run_id, "finding_id": finding.finding_id,
             "control": finding.framework_control, "coverage": finding.coverage,
             "requirement_text": finding.requirement_text, "locator": finding.citation.locator}
    items = overlaps.related(focus, _statements_for(session))
    ui.render_related(items, finding.framework_control)


def _render_prior_decision(finding, prior: dict) -> None:
    earlier = prior.get(finding.citation.chunk_id) if finding.citation.chunk_id else None
    if not earlier:
        return
    same = [item for item in earlier if item.get("requirement") == finding.requirement] or earlier
    item = same[0]
    state = item.get("state", "pending").replace("_", " ")
    who = item.get("final_approver") or item.get("first_approver") or item.get("rejected_by")
    text = (f":material/history: This section is unchanged since an earlier version. There, "
            f"\"{escape_md(item.get('requirement') or '')}\" was {escape_md(state)}")
    st.caption(text + (f" by {escape_md(who)}." if who else "."))


def _render_detail(backend, session, shown, reviewer: str, prior: dict) -> None:
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
            ui.render_second_review_badge(finding, session.two_person)
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
        _render_prior_decision(finding, prior)
        _render_related(session, finding)
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
            rule = (" Under the two-person rule, every decision must first be confirmed by a second reviewer."
                    if session.two_person else "")
            st.caption("Finalizing locks every decision, sends them back to the paused pipeline and produces the "
                       "signed report. Rejected findings stay out of the report; the audit trail keeps all of them."
                       + rule)
            if not ok:
                st.caption(f":material/info: {escape_md(reason)}")
        with action:
            clicked = st.button("Finalize review", type="primary", key="finalize", icon=":material/verified:",
                                disabled=not ok, width="stretch")
    if clicked:
        if not _guard(session):
            st.rerun()
        try:
            errors = analysis.finalize_review(backend, session, reviewer)
        except review.ReviewError as exc:
            st.error(escape_md(str(exc)))
            return
        _persist(session)
        if errors:
            listed = "\n".join(f"- {escape_md(error)}" for error in errors)
            st.error("Some sections couldn't be resumed, so the review isn't finalized yet. "
                     f"Select Finalize review again to retry.\n\n{listed}")
            return
        go_to(REPORT, "Review finalized. The signed report is ready.")


def _render_changes(session) -> None:
    """For a new version of a policy: which sections changed since the previous analyzed version."""
    mem = _session_memory(session)
    record = session.document_record or {}
    version = record.get("version")
    if mem is None or not version or version < 2:
        return
    changes = mem.compare(record["name"], version - 1, version)
    if not changes:
        return
    counts = {k: len(v) for k, v in changes.items()}
    label = (f"Compared with version {version - 1}: {counts['unchanged']} unchanged, {counts['changed']} changed, "
             f"{counts['added']} added, {counts['removed']} removed section(s)")
    with st.expander(label, icon=":material/difference:"):
        for kind in ("changed", "added", "removed"):
            if changes[kind]:
                st.markdown(f"**{kind.capitalize()}:** " + ", ".join(escape_md(s) for s in changes[kind]))
        st.caption("Sections are compared by their exact text. Findings in unchanged sections show what was "
                   "decided on them before.")


def page_review(backend, reviewer: str) -> None:
    session = st.session_state.review_session
    if session is None:
        st.info("Nothing to review yet. Analyze a policy, or reopen a stored review from the Library.",
                icon=":material/fact_check:")
        with st.container(horizontal=True, gap="small"):
            if st.button("Analyze a policy", key="empty_to_analyze", icon=":material/upload_file:"):
                go_to(ANALYZE)
            if st.button("Open the Library", key="empty_to_library", icon=":material/folder_open:"):
                go_to(LIBRARY)
        return

    run = session.run
    _render_stale_notice(session)
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        st.markdown(f"#### {escape_md(run.policy_source)}", anchors=False, width="content")
        version = (session.document_record or {}).get("version")
        if version:
            st.badge(f"Version {version}", color="blue", icon=":material/history:")
        if session.restored:
            st.badge("Reopened from the library", color="gray", icon=":material/folder_open:")
    st.caption(f"Analyzed {ui.format_ts(run.finished_at)} by the {escape_md(run.backend_name.lower())}. "
               f"{len(run.sections)} sections, {len(session.findings)} findings.")
    counts = review.counts(session)
    ui.render_posture(counts)

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
    _render_changes(session)
    if session.finalized:
        st.success(f"Signed off by {escape_md(session.finalized_by)} on {ui.format_ts(session.finalized_at)}. "
                   "Decisions are locked.", icon=":material/verified:")
    elif not reviewer.strip():
        st.warning("Enter your name as the reviewer in the sidebar to approve, reject or confirm findings.",
                   icon=":material/badge:")
    elif session.two_person and counts["awaiting_confirmation"]:
        mine = [f for f in review.awaiting_confirmation(session) if review.same_person(reviewer, f.reviewer)]
        others = counts["awaiting_confirmation"] - len(mine)
        if others:
            st.info(f"{plural(others, 'decision')} by another reviewer {'is' if others == 1 else 'are'} waiting for "
                    "your confirmation. Open each one, or confirm them together below the queue.",
                    icon=":material/group:")
        elif not counts["status"]["pending"]:
            st.info("Every finding has a decision. Under the two-person rule a different person now confirms them: "
                    "they open this review from the Library under their own name.", icon=":material/group:")

    if not session.findings:
        st.info("The agents found no checkable requirements in the analyzed sections.")
    else:
        mem = _session_memory(session)
        prior = mem.prior_decisions(session) if mem else {}
        queue, detail = st.columns([12, 13], gap="medium")
        with queue:
            shown = _render_queue(session, reviewer)
        with detail:
            _render_detail(backend, session, shown, reviewer, prior)

    if not session.finalized:
        _render_finalize(backend, session, reviewer)


# ====================================================================== report

ORIGIN_LABELS_REPORT = {None: "AI agents", "reviewer": "Edited by reviewer", "assistant": "Assistant's wording, accepted"}


def page_report() -> None:
    session = st.session_state.review_session
    if session is None:
        st.info("Analyze a policy and sign off the review to create a report. Signed reports of earlier reviews "
                "are in the Library.", icon=":material/lock:")
        return
    _render_stale_notice(session)
    if not session.finalized:
        counts = review.counts(session)
        pending, waiting = counts["status"]["pending"], counts["awaiting_confirmation"]
        if pending:
            detail = (f"{plural(pending, 'finding')} still {'needs' if pending == 1 else 'need'} a decision.")
        elif waiting:
            detail = f"{plural(waiting, 'decision')} still {'needs' if waiting == 1 else 'need'} a second reviewer."
        else:
            detail = "Every decision is in. Finalize the review to sign it off."
        st.info(f"The report unlocks once the review is finalized. {detail} Only approved findings go into the "
                "report.", icon=":material/lock:")
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
            two = (f" Every decision was confirmed by a second reviewer ({decisions['confirmed']} confirmations)."
                   if decisions.get("two_person") else "")
            st.caption(f"{escape_md(session.run.policy_source)}, signed off by {escape_md(decisions['finalized_by'])} "
                       f"on {ui.format_ts(decisions['finalized_at'])}. {decisions['approved']} approved and "
                       f"{decisions['rejected']} rejected findings.{two} Only approved findings are in the report; "
                       "the audit trail lists every decision.")
            with st.container(horizontal=True, gap="small"):
                if report["simulated"]:
                    st.badge("Simulated data, not a real assessment", color="gray", icon=":material/science:")
                if decisions.get("ledger_run_id"):
                    st.badge("Kept in the library", color="green", icon=":material/database:",
                             help=f"Ledger review {decisions['ledger_run_id']}")
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
        rows = []
        for f in approved:
            row = {
                "ID": f["id"],
                "Coverage": f["coverage"],
                "Control": f["framework_control"] or "Not mapped",
                "Requirement": f["requirement"],
                "Approved by": f["reviewer"],
            }
            if decisions.get("two_person"):
                row["Confirmed by"] = f.get("confirmed_by") or ""
            row["Recommendation"] = f["recommendation"] or "None needed"
            row["Wording"] = ORIGIN_LABELS_REPORT.get(f.get("recommendation_origin"), "AI agents")
            row["Source"] = ", ".join(str(x) for x in (f["citation"]["locator"], f"p. {f['citation']['page']}"
                                                          if f["citation"]["page"] else None) if x)
            rows.append(row)
        st.dataframe(
            pd.DataFrame(rows).style.map(ui.coverage_cell_css, subset=["Coverage"]),
            hide_index=True,
            column_config={
                "ID": st.column_config.TextColumn(width=56),
                "Coverage": st.column_config.TextColumn(width=104),
                "Control": st.column_config.TextColumn(width=80),
                "Requirement": st.column_config.TextColumn(width=180),
                "Approved by": st.column_config.TextColumn(width=110),
                "Confirmed by": st.column_config.TextColumn(width=110),
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


# ====================================================================== library

def _tile(column, label: str, value, caption: str | None = None, help_text: str | None = None) -> None:
    with column, st.container(border=True):
        st.metric(label, value, help=help_text)
        if caption:
            st.caption(caption)


def _library_policies(mem, rows, backend) -> None:
    frame = pd.DataFrame([{
        "Policy": ui.short_policy_name(row["name"]),
        "Version": f"v{row['version']}",
        "Findings": (row["latest"] or {}).get("findings"),
        "Gaps": (row["latest"] or {}).get("gaps"),
        "Status": (row["latest"] or {}).get("label") or "Not analyzed yet",
        "Last activity": ui.format_ts(row["last_activity"]),
    } for row in rows])
    event = st.dataframe(
        frame, key="library_table", on_select="rerun", selection_mode="single-row", hide_index=True,
        column_config={
            "Policy": st.column_config.TextColumn(width=240),
            "Version": st.column_config.TextColumn(width=70),
            "Findings": st.column_config.NumberColumn(width=80),
            "Gaps": st.column_config.NumberColumn(width=60),
            "Status": st.column_config.TextColumn(width=230),
            "Last activity": st.column_config.TextColumn(width=170),
        },
    )
    picked = event.selection.rows if event is not None and getattr(event, "selection", None) else []
    index = picked[0] if picked and picked[0] < len(rows) else 0
    st.caption("Select a policy to see its versions, reviews and memory.")
    _library_detail(mem, rows[index], backend)


def _library_detail(mem, row, backend) -> None:
    history = mem.document_history(row["name"])
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            st.markdown(f"**{escape_md(row['name'])}**", width="content")
            st.badge(f"Version {row['version']}", color="blue", icon=":material/history:")
            if row["versions"] > 1:
                st.badge(f"{row['versions']} versions", color="gray")
            st.space("stretch")
            stored = mem.stored_file(row["sha256"])
            if stored is not None:
                st.download_button("Original file", stored.read_bytes(), file_name=row["name"],
                                   key=f"lib_file:{row['sha256'][:12]}", icon=":material/download:",
                                   type="tertiary", on_click="ignore")
        st.caption(f"First added {ui.format_ts(row['first_seen'])}. Latest version added {ui.format_ts(row['added'])}. "
                   f"SHA-256 {row['sha256'][:16]}…")

        runs = list(reversed(history["runs"]))[:6]
        if runs:
            st.markdown("**Reviews**")
            for run in runs:
                with st.container(horizontal=True, vertical_alignment="center", gap="small"):
                    st.caption(f"v{run['version']} · {ui.format_ts(run['created_at'])} · "
                               f"{plural(run['findings'], 'finding')}, {run['gaps']} gaps · "
                               f"{escape_md(run['label'])}", width="content")
                    st.space("stretch")
                    if st.button("Open", key=f"lib_open:{run['id']}", icon=":material/open_in_new:", type="tertiary"):
                        _open_review(backend.simulated, run["id"])
        else:
            st.caption("Loaded but not analyzed yet.")

        analyzed_versions = sorted({run["version"] for run in history["runs"]})
        if len(analyzed_versions) >= 2:
            old, new = analyzed_versions[-2], analyzed_versions[-1]
            changes = mem.compare(row["name"], old, new)
            if changes:
                counts = {k: len(v) for k, v in changes.items()}
                st.caption(f":material/difference: Version {old} to {new}: {counts['unchanged']} sections unchanged, "
                           f"{counts['changed']} changed, {counts['added']} added, {counts['removed']} removed.")
        with st.expander("Memory note", icon=":material/sticky_note_2:"):
            st.caption("What the system knows about this policy, as a note you can paste into a wiki.")
            st.code(mem.note(row["name"]), language="markdown", wrap_lines=True)


def _library_overlaps(mem, rows) -> None:
    statements = mem.statements()
    cmap = overlaps.coverage_map(statements)
    policies = sorted({s["policy"] for s in statements})
    if len(policies) < 2:
        st.info("Analyze at least two policies to see where they overlap or disagree. For example, an internet "
                "and email policy and a computer security policy often both set password rules.",
                icon=":material/compare_arrows:")
    totals = overlaps.summary(cmap)
    baseline = writer_catalog.starter_controls()
    missing = overlaps.unaddressed(cmap, baseline)
    a, b, c, d = st.columns(4)
    _tile(a, "Controls addressed", totals["controls"], f"across {plural(len(policies), 'policy')}".replace("policys", "policies"))
    _tile(b, "In more than one policy", totals["overlaps"], "overlaps to keep consistent")
    _tile(c, "Values differ", totals["conflicts"], "conflicts to resolve")
    _tile(d, "Not addressed anywhere", len(missing), f"of {len(baseline)} starter controls",
          help_text="Controls from the starter set (NIST SP 800-53B low baseline plus a few moderate ones) that no "
                    "stored policy addresses at all.")

    conflicts = [row for row in cmap if row["conflicts"]]
    if conflicts:
        st.markdown("##### Values that differ between policies", anchors=False)
        for row in conflicts:
            for conflict in row["conflicts"][:3]:
                first, second, (dimension, values_a, values_b) = conflict["a"], conflict["b"], conflict["values"]
                with st.container(border=True):
                    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
                        st.badge(row["control"], color="blue", icon=":material/menu_book:")
                        st.badge("Values differ", color="red", icon=":material/difference:")
                        st.caption(f"{escape_md(' / '.join(values_a))} vs {escape_md(' / '.join(values_b))}",
                                   width="content")
                    left, right = st.columns(2)
                    for column, item in ((left, first), (right, second)):
                        with column:
                            st.markdown(f"**{escape_md(ui.short_policy_name(item['policy']))}**, "
                                        f"{escape_md(item.get('locator') or '')}")
                            st.markdown("> " + escape_md(item.get("requirement_text") or item.get("requirement") or ""))
                    st.caption(ui.RELATION_HELP["conflict"])

    if cmap:
        st.markdown("##### Coverage map", anchors=False)
        st.caption("Each row is a NIST control the policies address; each column shows the best coverage that "
                   "policy gives it. Rejected findings are left out.")
        names = {name: ui.short_policy_name(name) for name in policies}
        records = []
        for row in cmap:
            record = {"Control": row["control"]}
            for name in policies:
                record[names[name]] = row["policies"].get(name, "")
            record["Note"] = ("Values differ" if row["conflicts"] else
                              "Covered elsewhere" if row["overlap"] and row["best"] == "Full"
                              and any(v in overlaps.GAPS for v in row["policies"].values()) else
                              "In several policies" if row["overlap"] else "")
            records.append(record)
        frame = pd.DataFrame(records)
        styled = frame.style.map(lambda v: ui.coverage_cell_css(v) if v in ui.COVERAGE_STYLE else "",
                                 subset=[names[n] for n in policies])
        st.dataframe(styled, hide_index=True, height=min(38 + 35 * len(frame), 640),
                     column_config={"Control": st.column_config.TextColumn(width=90)})

    if missing:
        st.markdown("##### Not addressed by any policy yet", anchors=False)
        topics = writer_catalog.topics_for_controls(missing)
        listed = []
        for topic_id in topics:
            topic = writer_catalog.TOPIC_BY_ID[topic_id]
            ids = [cid for cid in topic.controls if cid in missing]
            listed.append(f"**{topic.title}**: " + ", ".join(f"`{cid}`" for cid in ids))
        st.markdown("  \n".join(listed))
        if st.button("Draft a starter policy for these topics", key="library_to_writer", icon=":material/edit_note:",
                     help="Opens the Policy writer with these topics selected."):
            st.session_state["_writer_topics"] = topics
            go_to(WRITER)


def _library_trail(mem, check) -> None:
    if check["ok"]:
        st.success(f"Trail intact: all {check['events']} entries check out. Each entry carries a SHA-256 hash of the "
                   "one before it, so changing or deleting a past entry outside the app would show up here.",
                   icon=":material/verified:")
    else:
        st.error(f"Trail broken at entry {check['first_bad_seq']}: an entry was changed, removed or inserted outside "
                 "the app. Restore the ledger from backup and investigate.", icon=":material/gpp_bad:")
    events = mem.trail(limit=400)
    if not events:
        st.caption("No activity yet.")
        return

    def details(event) -> str:
        detail, action = event.get("detail") or {}, event["action"]
        parts = []
        if action == "approved_first":
            parts.append({"approved_with_edits": "with edited wording",
                          "approved_with_ai_suggestion": "with the assistant's wording"}.get(detail.get("kind"), ""))
            if detail.get("bulk"):
                parts.append("bulk approval of Full coverage")
        elif action in ("approved_final", "rejection_confirmed"):
            parts.append("second reviewer")
        elif action == "reopened" and detail.get("sent_back"):
            parts.append("sent back by the second reviewer")
        elif action == "review_signed":
            parts.append(f"{detail.get('approved', 0)} approved, {detail.get('rejected', 0)} rejected")
            if detail.get("two_person"):
                parts.append("two-person rule")
        for key in ("note", "reason", "suggestion"):
            if detail.get(key):
                parts.append(str(detail[key]))
                break
        if any(parts):
            return "; ".join(p for p in parts if p)
        if action == "analysis_run":
            return f"{detail.get('findings', 0)} findings, {detail.get('backend') or ''}".strip(", ")
        if event["action"] == "document_added":
            return f"version {detail.get('version')}, {detail.get('sections')} sections"
        if event["action"] in ("policy_drafted", "policy_approved"):
            return f"{len(detail.get('topics') or [])} topics, {len(detail.get('controls') or [])} controls"
        return ""

    frame = pd.DataFrame([{
        "#": event["seq"],
        "Time": ui.format_ts(event["ts"]),
        "Who": event["actor"],
        "Action": memory.ACTION_LABELS.get(event["action"], event["action"].replace("_", " ").capitalize()),
        "Policy": ui.short_policy_name(event.get("document") or (event.get("detail") or {}).get("document") or "")
                  + (f" v{event['version']}" if event.get("version") else ""),
        "Finding": event.get("finding_id") or "",
        "Details": details(event),
    } for event in events])
    st.dataframe(frame, hide_index=True, height=min(38 + 35 * len(frame), 520), column_config={
        "#": st.column_config.NumberColumn(width=50),
        "Time": st.column_config.TextColumn(width=160),
        "Who": st.column_config.TextColumn(width=120),
        "Action": st.column_config.TextColumn(width=170),
        "Policy": st.column_config.TextColumn(width=200),
        "Finding": st.column_config.TextColumn(width=70),
        "Details": st.column_config.TextColumn(width=280),
    })


def page_library(backend) -> None:
    st.markdown("#### Policy library", anchors=False)
    st.caption("Everything analyzed here is kept: each version of each policy, every finding, and who decided what "
               "and when. Close the browser or restart the server, and it's all still here.")
    mem = get_memory(backend.simulated)
    if mem is None:
        st.warning("The library is off, so reviews last only as long as this browser session. "
                   + escape_md(st.session_state.get("_memory_error") or "Set COPILOT_MEMORY=on to keep them."),
                   icon=":material/database_off:")
        return
    rows = mem.library()
    check = mem.verify()
    in_review = sum(1 for row in rows if row["latest"] and not row["latest"]["signed"])
    awaiting = sum(row["latest"]["awaiting_confirmation"] for row in rows if row["latest"])
    a, b, c, d = st.columns(4)
    _tile(a, "Policies", len(rows), f"{plural(sum(row['versions'] for row in rows), 'version')} kept")
    _tile(b, "Open reviews", in_review, "not signed off yet")
    _tile(c, "Waiting for a 2nd reviewer", awaiting, "decisions to confirm")
    _tile(d, "Audit trail", "Intact" if check["ok"] else "Broken", f"{check['events']} hash-chained entries")
    if backend.simulated:
        st.caption(":material/science: Simulator records. They are kept in their own file, apart from live reviews.")
    if not rows:
        st.info("Nothing here yet. Analyze a policy, or draft a starter policy if the organization has none.",
                icon=":material/folder_open:")
        with st.container(horizontal=True, gap="small"):
            if st.button("Analyze a policy", key="library_to_analyze", icon=":material/upload_file:", type="primary"):
                go_to(ANALYZE)
            if st.button("Write a starter policy", key="library_empty_writer", icon=":material/edit_note:"):
                go_to(WRITER)
        return
    policies_tab, overlaps_tab, trail_tab = st.tabs([":material/description: Policies",
                                                     ":material/compare_arrows: Across policies",
                                                     ":material/receipt_long: Activity trail"])
    with policies_tab:
        _library_policies(mem, rows, backend)
    with overlaps_tab:
        _library_overlaps(mem, rows)
    with trail_tab:
        _library_trail(mem, check)


# ====================================================================== policy writer

def _writer_profile() -> writer_catalog.Profile:
    ss = st.session_state
    return writer_catalog.Profile(
        org=ss.get("writer_org") or "",
        size=ss.get("writer_size") or "1-10",
        sector=ss.get("writer_sector") or "Other",
        it_support=ss.get("writer_it") or "owner",
        uses=frozenset(ss.get("writer_uses") or ()),
    )


def _recommend_topics() -> None:
    st.session_state["writer_topics"] = writer_catalog.recommended_topics(_writer_profile())


def _render_writer_form(backend) -> None:
    ss = st.session_state
    with st.container(border=True):
        st.markdown("#### About the organization", anchors=False)
        st.text_input("Organization name", key="writer_org", max_chars=policy_writer.MAX_ORG_CHARS,
                      placeholder="For example: Maple Street Bakery")
        left, right = st.columns(2)
        with left:
            st.segmented_control("People", writer_catalog.SIZES, key="writer_size",
                                 format_func=lambda size: size.replace("-", "–"),
                                 help="How many people work there, including part-time staff.")
        with right:
            st.selectbox("Sector", writer_catalog.SECTORS, key="writer_sector")
        st.radio("Who looks after IT?", tuple(writer_catalog.IT_SUPPORT), key="writer_it",
                 format_func=writer_catalog.IT_SUPPORT.get, horizontal=True)
        st.pills("What the organization uses", tuple(writer_catalog.USES), key="writer_uses",
                 selection_mode="multi", format_func=writer_catalog.USES.get)

    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown("#### Topics", anchors=False, width="content")
            st.space("stretch")
            st.button("Use the recommended topics", key="writer_recommend", type="tertiary",
                      icon=":material/recommend:", on_click=_recommend_topics,
                      help="Picks the topics that fit the answers above.")
        if "writer_topics" not in ss:
            ss.writer_topics = writer_catalog.recommended_topics(_writer_profile())
        st.pills("Sections of the policy", [t.id for t in writer_catalog.TOPICS], key="writer_topics",
                 selection_mode="multi", label_visibility="collapsed",
                 format_func=lambda t: writer_catalog.TOPIC_BY_ID[t].title)
        picked = [t for t in writer_catalog.TOPICS if t.id in set(ss.writer_topics or ())]
        profile = _writer_profile()
        controls = list(dict.fromkeys(c.control for t in picked for c in policy_writer.baseline_clauses(t, profile)))
        st.caption(f"{plural(len(picked), 'section')}, implementing {plural(len(controls), 'NIST control')}: "
                   + ", ".join(controls) if picked else "Choose at least one topic.")
        ai = not backend.simulated
        label = "Draft the policy with AI" if ai else "Draft the policy"
        if st.button(label, key="writer_draft_btn", type="primary", icon=":material/edit_document:",
                     disabled=not picked or not (ss.get("writer_org") or "").strip()):
            _draft(backend, [t.id for t in picked])


def _draft(backend, topic_ids) -> None:
    profile = _writer_profile()
    drafter, method = None, None
    if not backend.simulated:
        try:
            drafter = backend.policy_drafter()
            method = f"NIST clauses adapted by {backend.model_description or 'the AI model'}"
        except Exception as exc:  # noqa: BLE001 - fall back to the baseline clauses, and say so
            log.exception("The AI policy drafter couldn't start")
            st.session_state["_flash"] = (f"The AI drafter couldn't start ({type(exc).__name__}), so the draft uses "
                                          "the baseline NIST clauses.")
    try:
        with st.spinner("Drafting each section from its NIST controls" + (" with AI" if drafter else "")):
            draft = policy_writer.draft_policy(profile, topic_ids, drafter=drafter, method=method)
    except policy_writer.DraftError as exc:
        st.error(escape_md(str(exc)))
        return
    st.session_state.writer_draft = draft
    st.session_state.writer_ver = st.session_state.get("writer_ver", 0) + 1
    mem = get_memory(backend.simulated)
    if mem is not None:
        try:
            mem.record(_reviewer() or "system", "policy_drafted",
                       {**policy_writer.to_record(draft), "document": draft.source_name})
        except Exception:  # noqa: BLE001 - the draft is still usable
            log.exception("Recording the draft failed")
    st.rerun()


def _render_writer_intro() -> None:
    with st.container(border=True):
        st.markdown("#### For organizations without a policy", anchors=False)
        st.markdown(
            "1. **Answer a few questions** in plain words: size, who does IT, what you use.\n"
            "2. **Pick the topics.** Each one maps to NIST SP 800-53 controls, mostly from the SP 800-53B low "
            "baseline that NIST sets for low-impact systems.\n"
            "3. **Get specific, checkable rules**, each traceable to its control. In live mode the AI adapts the "
            "wording to your answers from NIST's own text; any section it gets wrong falls back to the baseline.\n"
            "4. **Edit and approve it**, then **check it** with the same agents that review any other policy."
        )
        st.caption(escape_md(policy_writer.DISCLAIMER))


def _render_draft(backend, draft, reviewer: str) -> None:
    ver = st.session_state.get("writer_ver", 0)
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            st.markdown(f"#### {escape_md(draft.title)}", anchors=False, width="content")
            if draft.approved_by:
                st.badge(f"Approved by {escape_md(draft.approved_by)}", color="green", icon=":material/verified:")
            else:
                st.badge("Draft", color="orange", icon=":material/edit_note:")
            st.badge(f"{sum(len(s.clauses) for s in draft.sections)} rules", color="gray")
            st.badge(f"{len(draft.controls)} NIST controls", color="blue", icon=":material/menu_book:")
        st.caption(f"{escape_md(draft.method)}. Drafted {ui.format_ts(draft.created_at)}.")
        for note in draft.notes:
            st.warning(escape_md(note), icon=":material/warning:")

        for heading, text in policy_writer.front_matter(draft):
            st.markdown(f"**{escape_md(heading)}**  \n{escape_md(text)}")
        for section in draft.sections:
            title = section.heading + ("  ·  AI-adapted" if section.method == "ai" else "")
            with st.expander(title, expanded=section is draft.sections[0], icon=":material/rule:"):
                for n, clause in enumerate(section.clauses, start=1):
                    badge = "" if clause.baseline == "low" else " (moderate baseline)"
                    st.markdown(f"{section.number}.{n}  {escape_md(clause.text)}  \n"
                                f":blue-badge[{clause.control}] :gray[{escape_md(clause.title)}{badge}]")
                if section.edited_by:
                    st.caption(f":material/edit: Edited by {escape_md(section.edited_by)}.")
                with st.popover("Edit this section", icon=":material/edit:", type="tertiary"):
                    texts = [st.text_area(f"{section.number}.{n} ({clause.control})", clause.text,
                                          key=f"wedit:{ver}:{section.topic_id}:{n}", height=90,
                                          max_chars=policy_writer.MAX_CLAUSE_CHARS)
                             for n, clause in enumerate(section.clauses, start=1)]
                    if st.button("Save changes", key=f"wsave:{ver}:{section.topic_id}", type="primary",
                                 disabled=not reviewer.strip(),
                                 help="Needs your name in the sidebar: edits are recorded."):
                        try:
                            changed = policy_writer.edit_section(draft, section.topic_id, texts, reviewer)
                        except policy_writer.DraftError as exc:
                            st.error(escape_md(str(exc)))
                        else:
                            st.session_state["_flash"] = ("Saved. The draft needs approving again."
                                                          if changed else "No changes to save.")
                            st.rerun()
        for heading, text in policy_writer.back_matter(draft):
            st.markdown(f"**{escape_md(heading)}**  \n{escape_md(text)}")

    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        approve = st.button("Approve the draft", key="writer_approve", type="primary", icon=":material/verified:",
                            disabled=not reviewer.strip() or bool(draft.approved_by),
                            help="Records your approval and keeps this version in the library.")
        check = st.button("Check this draft against NIST", key="writer_check", icon=":material/fact_check:",
                          help="Runs the draft through the same gap analysis as any uploaded policy.")
        st.space("stretch")
        st.download_button("Markdown", policy_writer.to_markdown(draft), file_name=f"{draft.file_stem}.md",
                           mime="text/markdown", key="writer_md", icon=":material/download:", on_click="ignore")
        st.download_button("Print-ready page", policy_writer.to_html(draft), file_name=f"{draft.file_stem}.html",
                           mime="text/html", key="writer_html", icon=":material/print:", on_click="ignore",
                           help="Open it in a browser and print to PDF, or open it in Word.")
    if not reviewer.strip():
        st.caption("Enter your name in the sidebar to edit or approve the draft.")
    if approve:
        draft.approved_by, draft.approved_at = reviewer, policy_writer.now_iso()
        mem = get_memory(backend.simulated)
        if mem is not None:
            try:
                markdown = policy_writer.to_markdown(draft).encode("utf-8")
                info = mem.register(_draft_document(draft), markdown, reviewer, extension=".md")
                mem.record(reviewer, "policy_approved", {**policy_writer.to_record(draft), "document": info["name"],
                                                         "version": info["version"]})
            except Exception as exc:  # noqa: BLE001
                log.exception("Saving the approved draft failed")
                st.session_state["_memory_warning"] = f"The approval couldn't be saved ({type(exc).__name__})."
        st.session_state["_flash"] = "Draft approved and kept in the library."
        st.rerun()
    if check:
        st.session_state["_origin"] = SOURCE_OPTIONS[2]
        go_to(ANALYZE, "The draft is loaded. Choose the sections and run the analysis.")


def page_writer(backend, reviewer: str) -> None:
    draft = st.session_state.writer_draft
    form, side = st.columns([3, 2], gap="large")
    with side:
        _render_writer_intro()
    with form:
        _render_writer_form(backend)
    if draft is not None:
        _render_draft(backend, draft, reviewer)


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
            _persist(session)
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

def _default_page(backend) -> str:
    """Returning users land in the library; a first visit starts by analyzing a policy."""
    mem = get_memory(backend.simulated)
    try:
        return LIBRARY if mem is not None and mem.library() else ANALYZE
    except Exception:  # noqa: BLE001
        return ANALYZE


def main() -> None:
    init_state()
    if "_goto_mode" in st.session_state:  # reopening a review from the other data source's library
        st.session_state.mode = st.session_state.pop("_goto_mode")
    if "_flash" in st.session_state:
        st.toast(escape_md(st.session_state.pop("_flash")), icon=":material/info:")

    backend, reviewer = render_sidebar()
    if backend is None:
        st.markdown("## Compliance Copilot", anchors=False)
        st.error("The selected data source isn't available. Fix the problem shown in the sidebar, "
                 "or switch to the simulator.")
        return

    session = st.session_state.review_session
    if session is not None and not st.session_state.get("_stale"):
        _persist(session)  # anything a button callback logged since the last run
    if "_memory_warning" in st.session_state:
        st.toast(escape_md(st.session_state.pop("_memory_warning")), icon=":material/database_off:")

    ui.tighten_page()
    ui.render_header(backend, reviewer)
    if "nav" not in st.session_state:
        st.session_state.nav = _default_page(backend)
    st.segmented_control("Workflow", PAGES, key="nav", required=True, format_func=NAV_LABELS.get,
                         label_visibility="collapsed")

    page = st.session_state.nav
    if page == LIBRARY:
        page_library(backend)
    elif page == ANALYZE:
        page_analyze(backend)
    elif page == REVIEW:
        page_review(backend, reviewer)
    elif page == REPORT:
        page_report()
    elif page == WRITER:
        page_writer(backend, reviewer)
    else:
        page_assistant(backend, reviewer)


main()
