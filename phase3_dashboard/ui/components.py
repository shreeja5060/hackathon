"""Streamlit pieces for the dashboard.

Rule for this file: document text and model output are always passed through
escape_md() or escape_md_block() before st.markdown, and never through
unsafe_allow_html. Tables (st.dataframe) and st.json render plain values.
"""

from __future__ import annotations

import re
from datetime import datetime

import pandas as pd
import streamlit as st

from ..core import review
from ..core.contracts import COVERAGE_VALUES, INVALID_COVERAGE, Finding
from ..core.security import escape_md, escape_md_block

COVERAGE_BADGE = {
    "Full": ("green", ":material/check_circle:"),
    "Partial": ("orange", ":material/timelapse:"),
    "Missing": ("red", ":material/cancel:"),
    "Not observable": ("gray", ":material/visibility_off:"),
    INVALID_COVERAGE: ("violet", ":material/error:"),
}
STATUS_BADGE = {
    "pending": ("blue", ":material/hourglass_empty:", "Needs decision"),
    "approved": ("green", ":material/task_alt:", "Approved"),
    "rejected": ("gray", ":material/block:", "Rejected"),
}
# Plain-text markers for places that can't show badges (tables, pills).
COVERAGE_MARK = {"Full": "🟢", "Partial": "🟠", "Missing": "🔴", "Not observable": "⚪", INVALID_COVERAGE: "⛔"}
STATUS_MARK = {"pending": "⏳", "approved": "✅", "rejected": "✖️"}

TABLE_COLUMNS = ("ID", "Coverage", "Requirement", "Control", "Finding", "Recommendation", "Source", "Status")
TABLE_CONFIG = {
    "ID": st.column_config.TextColumn(width="small"),
    "Coverage": st.column_config.TextColumn(width="small"),
    "Control": st.column_config.TextColumn(width="small"),
    "Finding": st.column_config.TextColumn(width="large"),
    "Recommendation": st.column_config.TextColumn(width="large"),
    "Status": st.column_config.TextColumn(width="small"),
}


def coverage_option(value: str) -> str:
    return f"{COVERAGE_MARK.get(value, '')} {value}".strip()


def status_option(value: str) -> str:
    return f"{STATUS_MARK.get(value, '')} {STATUS_BADGE[value][2]}".strip()


def format_ts(iso: str | None) -> str:
    if not iso:
        return "unknown time"
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return iso


def report_file_stem(policy_source: str, finalized_at: str | None) -> str:
    stem = re.sub(r"\.pdf$", "", policy_source, flags=re.I)
    stem = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()[:60] or "policy"
    day = (finalized_at or "")[:10] or "draft"
    return f"gap-report-{stem}-{day}"


# ---------------------------------------------------------------- summary

def render_coverage_summary(counts: dict) -> None:
    columns = st.columns(len(COVERAGE_VALUES))
    for column, value in zip(columns, COVERAGE_VALUES):
        column.metric(value, counts["coverage"][value], icon=COVERAGE_BADGE[value][1], border=True)
    invalid = counts["coverage"][INVALID_COVERAGE]
    if invalid:
        st.error(f"{invalid} finding(s) failed validation. They can be rejected but not approved.",
                 icon=":material/error:")


def render_review_progress(counts: dict) -> None:
    total = counts["total"]
    status = counts["status"]
    decided = total - status["pending"]
    st.progress(
        decided / total if total else 1.0,
        text=f"{decided} of {total} findings decided: {status['approved']} approved, {status['rejected']} rejected",
    )


def findings_frame(findings: list[Finding]) -> pd.DataFrame:
    rows = [
        {
            "ID": f.finding_id,
            "Coverage": coverage_option(f.coverage),
            "Requirement": f.requirement,
            "Control": f.framework_control or "Not mapped",
            "Finding": f.finding,
            "Recommendation": f.final_recommendation or "None needed",
            "Source": f.citation.label(),
            "Status": status_option(f.status),
        }
        for f in findings
    ]
    return pd.DataFrame(rows, columns=TABLE_COLUMNS)


# ---------------------------------------------------------------- finding card

def _badges(finding: Finding) -> None:
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        st.markdown(f"**{finding.finding_id}**", width="content")
        color, icon = COVERAGE_BADGE.get(finding.coverage, COVERAGE_BADGE[INVALID_COVERAGE])
        st.badge(finding.coverage, color=color, icon=icon)
        if finding.framework_control:
            st.badge(escape_md(finding.framework_control), color="primary", icon=":material/shield:")
        else:
            st.badge("No control mapped", color="gray")
        color, icon, label = STATUS_BADGE[finding.status]
        st.badge(label, color=color, icon=icon)


def _evidence(session: review.ReviewSession, finding: Finding) -> None:
    with st.expander("Evidence"):
        if finding.mapping_reasoning:
            st.markdown(f"**Why this control:** {escape_md(finding.mapping_reasoning)}")
        passage = session.passages.get(finding.citation.chunk_id) if finding.citation.chunk_id else None
        if passage:
            st.markdown("**Cited passage**")
            st.markdown(escape_md_block(passage["text"]))
        else:
            st.caption("The cited passage isn't available in this run.")
        if finding.extras:
            st.markdown("**Other fields from the agents**")
            st.json(finding.extras, expanded=False)


def _apply(action, *args, **kwargs) -> None:
    try:
        action(*args, **kwargs)
    except review.ReviewError as exc:
        st.error(escape_md(str(exc)))
        return
    st.rerun()


def _decision_line(finding: Finding) -> None:
    if finding.status == "pending":
        return
    verb = "Approved" if finding.status == "approved" else "Rejected"
    line = f"{verb} by {escape_md(finding.reviewer)} on {format_ts(finding.reviewed_at)}."
    if finding.reviewer_note:
        line += f" Note: {escape_md(finding.reviewer_note)}"
    st.caption(line)


def _decision_controls(session, finding: Finding, reviewer: str, key: str) -> None:
    if session.finalized:
        _decision_line(finding)
        return
    if finding.status != "pending":
        _decision_line(finding)
        if st.button("Undo decision", key=f"undo:{key}", type="tertiary", icon=":material/undo:",
                     disabled=not reviewer.strip()):
            _apply(review.reopen, session, finding.finding_id, reviewer)
        return

    edited = None
    if finding.approvable and finding.coverage != "Full":
        edited = st.text_area(
            "Recommendation to approve",
            value=finding.recommendation or "",
            key=f"rec:{key}",
            height=80,
            max_chars=review.MAX_RECOMMENDATION_CHARS,
            help="Edit the AI's text before approving if it needs changes. Edits are recorded in the audit trail.",
        )
    note = st.text_input(
        "Note",
        key=f"note:{key}",
        max_chars=review.MAX_NOTE_CHARS,
        placeholder="Required to reject. Optional when approving.",
    )
    no_reviewer = not reviewer.strip()
    with st.container(horizontal=True, gap="small"):
        approve = st.button("Approve", key=f"approve:{key}", type="primary", icon=":material/check:",
                            disabled=no_reviewer or not finding.approvable)
        reject = st.button("Reject", key=f"reject:{key}", icon=":material/close:", disabled=no_reviewer)
    if approve:
        _apply(review.approve, session, finding.finding_id, reviewer, edited_recommendation=edited, note=note)
    if reject:
        _apply(review.reject, session, finding.finding_id, reviewer, note)


def render_finding_card(session: review.ReviewSession, finding: Finding, reviewer: str) -> None:
    key = f"{session.run.run_id}:{finding.finding_id}"
    with st.container(border=True):
        _badges(finding)
        st.markdown(f"**{escape_md(finding.requirement)}**")
        if finding.requirement_text:
            st.markdown("> " + escape_md(finding.requirement_text))
        st.markdown(f"**Finding:** {escape_md(finding.finding)}")
        recommendation = escape_md(finding.recommendation) if finding.recommendation else "None needed."
        st.markdown(f"**AI recommendation:** {recommendation}")
        if finding.recommendation_edited is not None:
            edited = escape_md(finding.recommendation_edited) or "None"
            st.markdown(f"**Approved recommendation, edited by the reviewer:** {edited}")
        st.caption(f"Source: {escape_md(finding.citation.label())}")
        for problem in finding.problems:
            st.error(escape_md(problem), icon=":material/error:")
        for flag in finding.flags:
            st.caption(f":material/flag: {escape_md(flag)}")
        _evidence(session, finding)
        _decision_controls(session, finding, reviewer, key)


# ---------------------------------------------------------------- chat

def _preview(text: str, limit: int = 320) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def render_chat_message(message: dict) -> None:
    with st.chat_message(message["role"]):
        st.markdown(escape_md(message["content"]))
        if message["role"] != "assistant" or message.get("error"):
            return
        citations = message.get("citations") or []
        if not citations:
            st.caption("No sources came back with this answer, so treat it with caution.")
        else:
            with st.expander(f"Sources ({len(citations)})"):
                for citation in citations:
                    where = citation["locator"] or (f"page {citation['page']}" if citation["page"] else "")
                    heading = f"**{escape_md(citation['source'])}**"
                    if where:
                        heading += f", {escape_md(where)}"
                    st.markdown(f"{heading} (similarity {citation['score']:.2f})")
                    st.caption(escape_md(_preview(citation["text"])))
        if message.get("dropped"):
            st.caption(f"{message['dropped']} source(s) were left out because they weren't in the expected format.")
