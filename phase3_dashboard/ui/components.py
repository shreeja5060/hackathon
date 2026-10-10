"""Streamlit pieces for the dashboard.

Rules for this file:
* Document text and model output are always passed through escape_md() or
  escape_md_block() before st.markdown, and never through unsafe_allow_html.
* The only raw HTML is the coverage bar (render_posture), built from counts
  and fixed labels; it never contains document or model text.
* Tables (st.dataframe) and st.json render plain values.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

import pandas as pd
import streamlit as st

from ..core import review
from ..core.contracts import COVERAGE_VALUES, GAP_COVERAGE, INVALID_COVERAGE, Finding, is_control_id
from ..core.security import escape_md, escape_md_block

# Hex values match .streamlit/config.toml (greenColor, orangeColor, ...).
COVERAGE_STYLE = {
    "Full": {"badge": "green", "icon": ":material/check_circle:", "bar": "#1A7F5A", "ink": "#135E43", "tint": "#E3F2EC"},
    "Partial": {"badge": "orange", "icon": ":material/timelapse:", "bar": "#D08A1E", "ink": "#8A5306", "tint": "#FBEFD9"},
    "Missing": {"badge": "red", "icon": ":material/cancel:", "bar": "#B42318", "ink": "#8F1C13", "tint": "#FBE7E5"},
    "Not observable": {"badge": "gray", "icon": ":material/visibility_off:", "bar": "#8A97A8", "ink": "#4A5668",
                       "tint": "#EDF0F4"},
    INVALID_COVERAGE: {"badge": "violet", "icon": ":material/error:", "bar": "#6E56CF", "ink": "#4F3BA6",
                       "tint": "#EEEAFB"},
}
STATUS_STYLE = {
    "pending": {"badge": "blue", "icon": ":material/pending:", "label": "Needs decision", "ink": "#184C8C"},
    "approved": {"badge": "green", "icon": ":material/task_alt:", "label": "Approved", "ink": "#135E43"},
    "rejected": {"badge": "gray", "icon": ":material/block:", "label": "Rejected", "ink": "#4A5668"},
}
INK_MUTED = "#526079"
FOCUS_TINT = "#E4EDF8"


def coverage_option(value: str) -> str:
    style = COVERAGE_STYLE.get(value, COVERAGE_STYLE[INVALID_COVERAGE])
    return f"{style['icon']} {value}"


def status_option(value: str) -> str:
    style = STATUS_STYLE[value]
    return f"{style['icon']} {style['label']}"


def control_md(control: str | None) -> str:
    """A control ID as inline code when it looks like one; otherwise escaped text."""
    if not control:
        return "Not mapped"
    return f"`{control}`" if is_control_id(control) else escape_md(control)


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


# ---------------------------------------------------------------- header

def tighten_page() -> None:
    """Less empty space above the header. Fixed CSS only; never user or model text."""
    st.html("<style>[data-testid='stMainBlockContainer']{padding-top:2.25rem}</style>")


def render_header(backend, reviewer: str) -> None:
    """Product name, what it checks against, and the state of this workspace."""
    framework = backend.framework_info()
    title, status = st.columns([3, 2], vertical_alignment="bottom")
    with title:
        st.markdown("## Compliance Copilot", anchors=False)
        st.caption("Gap analysis of security policies against NIST SP 800-53 Rev. 5. "
                   "AI agents draft every finding; a named reviewer signs off on each one.")
    with status, st.container(horizontal=True, horizontal_alignment="right", gap="small"):
        if backend.simulated:
            st.badge("Simulated data", color="gray", icon=":material/science:",
                     help="Findings come from local rules in the agents' exact format, not from Claude.")
        else:
            st.badge("Live pipeline", color="green", icon=":material/bolt:",
                     help="Phase 1 index, Phase 2 agents and the Q&A assistant.")
        version = framework.get("version") or "version unknown"
        st.badge(f"SP 800-53 {escape_md(version)}", color="blue", icon=":material/menu_book:",
                 help=escape_md(framework.get("title") or "NIST SP 800-53"))
        if reviewer.strip():
            st.badge(escape_md(" ".join(reviewer.split())), color="primary", icon=":material/person:")
        else:
            st.badge("No reviewer set", color="orange", icon=":material/person_off:",
                     help="Enter your name in the sidebar to make decisions.")


# ---------------------------------------------------------------- coverage summary

def render_posture(counts: dict) -> None:
    """The coverage bar and decision progress. Only counts and fixed labels go into the HTML."""
    total = counts["total"]
    coverage = counts["coverage"]
    status = counts["status"]
    gaps = sum(coverage[value] for value in GAP_COVERAGE)
    decided = total - status["pending"]
    shown = [v for v in (*COVERAGE_VALUES, INVALID_COVERAGE) if v in COVERAGE_VALUES or coverage[v]]

    segments = "".join(
        f'<div title="{html.escape(v)}: {coverage[v]}" style="flex:{coverage[v]} 1 0;min-width:6px;'
        f'background:{COVERAGE_STYLE[v]["bar"]}"></div>'
        for v in shown if coverage[v]
    ) or '<div style="flex:1;background:#E3E8EF"></div>'
    legend = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:18px;white-space:nowrap">'
        f'<span style="width:10px;height:10px;border-radius:2px;background:{COVERAGE_STYLE[v]["bar"]}"></span>'
        f'{html.escape(v)} <strong>{coverage[v]}</strong></span>'
        for v in shown
    )
    label = ", ".join(f"{v} {coverage[v]}" for v in shown)
    progress = 0 if not total else round(100 * decided / total)
    st.html(f"""
<div style="display:flex;flex-wrap:wrap;gap:16px 40px;align-items:flex-end;margin:4px 0 8px">
  <div style="flex:3 1 380px;min-width:0">
    <div style="display:flex;justify-content:space-between;gap:12px;margin-bottom:8px">
      <span style="font-weight:600">Coverage across {total} findings</span>
      <span style="color:{INK_MUTED}">{gaps} gap{'' if gaps == 1 else 's'} (Partial or Missing)</span>
    </div>
    <div role="img" aria-label="{html.escape(label)}"
         style="display:flex;gap:2px;height:14px;border-radius:4px;overflow:hidden">{segments}</div>
    <div style="margin-top:10px;font-size:0.875rem">{legend}</div>
  </div>
  <div style="flex:2 1 240px;min-width:0">
    <div style="display:flex;justify-content:space-between;gap:12px;margin-bottom:8px">
      <span style="font-weight:600">Decisions</span>
      <span style="color:{INK_MUTED}">{decided} of {total}</span>
    </div>
    <div role="progressbar" aria-valuenow="{progress}" aria-valuemin="0" aria-valuemax="100"
         style="height:14px;border-radius:4px;background:#E3E8EF;overflow:hidden">
      <div style="width:{progress}%;height:100%;background:#1F5FAE"></div>
    </div>
    <div style="margin-top:10px;font-size:0.875rem;color:{INK_MUTED}">
      {status['approved']} approved, {status['rejected']} rejected, {status['pending']} waiting
    </div>
  </div>
</div>""")
    invalid = coverage[INVALID_COVERAGE]
    if invalid:
        st.error(f"{invalid} finding(s) failed validation. They can be rejected but not approved.",
                 icon=":material/error:")


# ---------------------------------------------------------------- queue

# The requirement name goes last: on a narrow screen it's the column that gets
# cut off, and the open finding shows it in full anyway.
QUEUE_COLUMNS = ("ID", "Coverage", "Status", "Requirement")
QUEUE_CONFIG = {
    "ID": st.column_config.TextColumn(width=56),
    "Coverage": st.column_config.TextColumn(width=104),
    "Status": st.column_config.TextColumn(width=82),
    "Requirement": st.column_config.TextColumn(width=210),
}
QUEUE_STATUS = {"pending": "Open", "approved": "Approved", "rejected": "Rejected"}


def coverage_cell_css(value) -> str:
    style = COVERAGE_STYLE.get(value, COVERAGE_STYLE[INVALID_COVERAGE])
    return f"color:{style['ink']};background-color:{style['tint']};font-weight:600"


def queue_frame(findings: list[Finding], focus_id: str | None):
    """The review queue: one row per finding, colored by coverage and status, the open one tinted."""
    frame = pd.DataFrame(
        [{
            "ID": f.finding_id,
            "Coverage": f.coverage,
            "Status": QUEUE_STATUS[f.status],
            "Requirement": f.requirement,
        } for f in findings],
        columns=QUEUE_COLUMNS,
    )
    labels = {QUEUE_STATUS[status]: style for status, style in STATUS_STYLE.items()}

    coverage_css = coverage_cell_css

    def status_css(value):
        style = labels.get(value)
        return f"color:{style['ink']};font-weight:500" if style else ""

    def focus_row(row):
        tint = f"background-color:{FOCUS_TINT}" if row["ID"] == focus_id else ""
        return [tint if column != "Coverage" else "" for column in row.index]

    return (frame.style
            .apply(focus_row, axis=1)
            .map(coverage_css, subset=["Coverage"])
            .map(status_css, subset=["Status"]))


# ---------------------------------------------------------------- finding detail

def render_finding_badges(finding: Finding) -> None:
    style = COVERAGE_STYLE.get(finding.coverage, COVERAGE_STYLE[INVALID_COVERAGE])
    st.badge(finding.coverage, color=style["badge"], icon=style["icon"])
    if finding.framework_control:
        st.badge(escape_md(finding.framework_control), color="blue", icon=":material/menu_book:")
    else:
        st.badge("No control mapped", color="gray")
    status = STATUS_STYLE[finding.status]
    st.badge(status["label"], color=status["badge"], icon=status["icon"])
    if finding.assistant_suggestion and finding.status == "pending":
        st.badge("Suggestion waiting", color="violet", icon=":material/auto_awesome:")


def render_finding_body(session: review.ReviewSession, finding: Finding) -> None:
    """What the agents found, next to the evidence it rests on."""
    st.markdown(f"### {escape_md(finding.requirement)}", anchors=False)
    if finding.requirement_text:
        st.markdown("> " + escape_md(finding.requirement_text))
    st.caption(f":material/description: {escape_md(finding.citation.label())}")
    if finding.plain_language:
        st.markdown(f"**In plain words:** {escape_md(finding.plain_language)}")
    st.markdown(f"**Finding:** {escape_md(finding.finding)}")
    recommendation = escape_md(finding.recommendation) if finding.recommendation else "None needed."
    st.markdown(f"**AI recommendation:** {recommendation}")
    if finding.recommendation_edited is not None:
        origin = "the assistant's wording, accepted" if finding.recommendation_origin == "assistant" else "edited"
        edited = escape_md(finding.recommendation_edited) or "None"
        st.markdown(f"**Approved recommendation ({origin} by the reviewer):** {edited}")
    if finding.clarifying_questions:
        st.markdown("**Questions for the reviewer**  \n" + "  \n".join(
            f"{n}\\. {escape_md(question)}" for n, question in enumerate(finding.clarifying_questions, start=1)))
    for problem in finding.problems:
        st.error(escape_md(problem), icon=":material/error:")
    for flag in finding.flags:
        st.caption(f":material/flag: {escape_md(flag)}")
    with st.expander("Evidence", icon=":material/plagiarism:"):
        st.markdown(f"**Mapped control:** {control_md(finding.framework_control)}")
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


def decision_line(finding: Finding) -> str:
    verb = "Approved" if finding.status == "approved" else "Rejected"
    line = f"{verb} by {escape_md(finding.reviewer)} on {format_ts(finding.reviewed_at)}."
    if finding.reviewer_note:
        line += f" Note: {escape_md(finding.reviewer_note)}"
    return line


# ---------------------------------------------------------------- assistant

def _preview(text: str, limit: int = 320) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def render_sources(citations: list[dict], *, expanded: bool = False) -> None:
    if not citations:
        st.caption("No sources came back with this answer, so treat it with caution.")
        return
    with st.expander(f"Sources ({len(citations)})", icon=":material/library_books:", expanded=expanded):
        for citation in citations:
            where = citation["locator"] or (f"page {citation['page']}" if citation["page"] else "")
            heading = f"**\\[{citation.get('ref', '?')}\\]** {escape_md(citation['source'])}"
            if where:
                heading += f", {escape_md(where)}"
            st.markdown(heading)
            st.caption(escape_md(_preview(citation["text"])))


def render_assistant_reply(message: dict) -> None:
    """An assistant answer: text, numbered sources and a note about dropped items."""
    st.markdown(escape_md(message["content"]))
    if message.get("error"):
        return
    render_sources(message.get("citations") or [])
    dropped = message.get("dropped", 0) + message.get("dropped_suggestions", 0)
    if dropped:
        st.caption(f"{dropped} item(s) were left out because they weren't in the expected format "
                   "or named a finding that isn't in this review.")
