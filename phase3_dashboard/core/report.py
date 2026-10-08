"""Builds the exported gap report. Only human-approved findings go in it.

AI text is escaped in the Markdown export, and CSV cells that a spreadsheet
would treat as formulas are neutralized (CSV injection).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any

from .contracts import COVERAGE_VALUES
from .review import ReviewSession, counts, utc_now
from .security import escape_md

NOTICE = (
    "AI agents drafted these findings and recommendations. Every finding in this report "
    "was approved by the named reviewer. Recommendations the reviewer edited, or rewrote with "
    "the AI assistant's suggested wording, are marked. Rejected findings are not included."
)
ORIGIN_NOTES = {
    "reviewer": "edited by reviewer",
    "assistant": "AI assistant's wording, accepted by reviewer",
}
ACTION_LABELS = {
    "approved": "Approved",
    "approved_with_edits": "Approved with edits",
    "approved_with_ai_suggestion": "Approved with the assistant's wording",
    "assistant_suggested": "Assistant suggested wording",
    "rejected": "Rejected",
    "reopened": "Reopened",
    "finalized": "Finalized and signed off",
}


def _when(iso: str | None) -> str:
    """2026-10-08T16:03:22+00:00 -> 2026-10-08 16:03 UTC, for people reading the Markdown."""
    if not iso:
        return "unknown time"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso
SIMULATED_LABEL = "SIMULATED DATA: produced by the dashboard simulator, not a real assessment."


def build_report(session: ReviewSession) -> dict[str, Any]:
    if not session.finalized:
        raise ValueError("Finalize the review before exporting a report.")
    run = session.run
    approved = [f for f in session.findings if f.status == "approved"]
    c = counts(session)
    return {
        "title": "Compliance gap report",
        "generated_at": utc_now(),
        "data": SIMULATED_LABEL if run.simulated else "Live pipeline output",
        "simulated": run.simulated,
        "policy": {"source": run.policy_source, "origin": run.policy_origin},
        "framework": dict(run.framework),
        "run": {
            "run_id": run.run_id,
            "backend": run.backend_name,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "sections_analyzed": len(run.sections),
            "retrieval": run.retrieval_backends,
            "sections_with_problems": [
                {"section": s.label, "status": s.status, "problems": s.warnings or [s.error]}
                for s in run.problem_sections
            ],
        },
        "review": {
            "finalized_by": session.finalized_by,
            "finalized_at": session.finalized_at,
            "approved": c["status"]["approved"],
            "rejected": c["status"]["rejected"],
        },
        "summary": {
            "approved_by_coverage": {
                value: sum(1 for f in approved if f.coverage == value) for value in COVERAGE_VALUES
            },
        },
        "notice": NOTICE,
        "approved_findings": [_finding_record(f) for f in approved],
        "audit_log": [event.as_dict() for event in session.audit_log],
    }


def _finding_record(finding) -> dict[str, Any]:
    record = {
        "id": finding.finding_id,
        "requirement": finding.requirement,
        "requirement_text": finding.requirement_text,
        "coverage": finding.coverage,
        "framework_control": finding.framework_control,
        "finding": finding.finding,
        "plain_language": finding.plain_language,
        "clarifying_questions": list(finding.clarifying_questions),
        "recommendation": finding.final_recommendation,
        "recommendation_edited_by_reviewer": finding.recommendation_edited is not None,
        "recommendation_origin": finding.recommendation_origin,
        "citation": {
            "source": finding.citation.source,
            "locator": finding.citation.locator,
            "page": finding.citation.page,
            "chunk_id": finding.citation.chunk_id,
        },
        "reviewer": finding.reviewer,
        "reviewed_at": finding.reviewed_at,
        "reviewer_note": finding.reviewer_note or None,
    }
    if finding.recommendation_edited is not None:
        record["recommendation_ai_original"] = finding.recommendation
    return record


# ---------------------------------------------------------------- Markdown

def _md(value) -> str:
    """Escape one value for a Markdown line or table cell."""
    if value is None or value == "":
        return "None"
    return escape_md(value)


def _source(citation: dict) -> str:
    parts = [citation["source"]]
    if citation.get("locator"):
        parts.append(citation["locator"])
    if citation.get("page") is not None:
        parts.append(f"p. {citation['page']}")
    return ", ".join(parts)


def to_markdown(report: dict[str, Any]) -> str:
    framework = report["framework"]
    review = report["review"]
    findings = report["approved_findings"]
    lines = [f"# {report['title']}", ""]
    if report["simulated"]:
        lines += [f"> **{report['data']}**", ""]
    title = framework.get("title") or "NIST SP 800-53"
    version = framework.get("version")
    framework_line = title if version and version in title else f"{title}, version {version or 'unknown'}"
    lines += [
        f"- **Policy:** {_md(report['policy']['source'])}",
        f"- **Framework:** {_md(framework_line)}",
        f"- **Signed off by:** {_md(review['finalized_by'])}, {_when(review['finalized_at'])}",
        f"- **Decisions:** {review['approved']} approved, {review['rejected']} rejected",
        f"- **Generated:** {_when(report['generated_at'])}",
        "",
        f"_{escape_md(report['notice'])}_",
        "",
        "## Summary of approved findings",
        "",
        "| Coverage | Findings |",
        "| --- | ---: |",
        *[f"| {value} | {n} |" for value, n in report["summary"]["approved_by_coverage"].items()],
        "",
        "## Findings",
        "",
    ]

    if not findings:
        lines += ["No findings were approved.", ""]
    else:
        lines += [
            "| ID | Requirement | Coverage | Control | Finding | Recommendation | Source |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for f in findings:
            recommendation = _md(f["recommendation"])
            if f["recommendation_edited_by_reviewer"]:
                recommendation += f" _({ORIGIN_NOTES.get(f.get('recommendation_origin'), 'edited by reviewer')})_"
            lines.append(
                f"| {f['id']} | {_md(f['requirement'])} | {f['coverage']} | {_md(f['framework_control'])} "
                f"| {_md(f['finding'])} | {recommendation} | {_md(_source(f['citation']))} |"
            )
        lines += ["", "## Details", ""]
        for f in findings:
            lines += [f"### {f['id']}: {_md(f['requirement'])}", ""]
            if f["requirement_text"]:
                lines += [f"> {_md(f['requirement_text'])}", ""]
            lines += [
                f"- **Coverage:** {f['coverage']}, against {_md(f['framework_control'])}",
                f"- **In plain words:** {_md(f['plain_language'])}",
                f"- **Finding:** {_md(f['finding'])}",
                f"- **Recommendation:** {_md(f['recommendation'])}",
            ]
            if f["recommendation_edited_by_reviewer"]:
                lines.append(f"- **AI's original recommendation:** {_md(f.get('recommendation_ai_original'))}")
            lines += [
                f"- **Source:** {_md(_source(f['citation']))}",
                f"- **Approved by:** {_md(f['reviewer'])}, {_when(f['reviewed_at'])}",
            ]
            if f["clarifying_questions"]:
                lines.append(f"- **Questions raised:** {_md(' / '.join(f['clarifying_questions']))}")
            if f["reviewer_note"]:
                lines.append(f"- **Reviewer note:** {_md(f['reviewer_note'])}")
            lines.append("")

    problems = report["run"]["sections_with_problems"]
    if problems:
        lines += ["## Problems during analysis", ""]
        lines += [f"- {_md(s['section'])}: {_md('; '.join(p for p in s['problems'] if p))}" for s in problems]
        lines.append("")

    lines += ["## Audit trail", "", "| Time | Reviewer | Action | Finding |", "| --- | --- | --- | --- |"]
    for event in report["audit_log"]:
        action = ACTION_LABELS.get(event["action"], event["action"])
        if event["detail"].get("bulk"):
            action += " (bulk, Full coverage)"
        lines.append(
            f"| {_when(event['timestamp'])} | {_md(event['reviewer'])} | {action} "
            f"| {event['finding_id'] or 'All'} |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- JSON / CSV

def to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False) + "\n"


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value) -> Any:
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value  # stops Excel/Sheets from running it as a formula
    return value


CSV_COLUMNS = (
    "id", "requirement", "coverage", "framework_control", "finding", "plain_language", "recommendation",
    "recommendation_edited_by_reviewer", "recommendation_origin", "source", "locator", "page",
    "reviewer", "reviewed_at", "reviewer_note", "requirement_text", "clarifying_questions",
)


def to_csv(report: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for f in report["approved_findings"]:
        row = {key: f.get(key) for key in CSV_COLUMNS}
        row.update(source=f["citation"]["source"], locator=f["citation"]["locator"], page=f["citation"]["page"],
                   clarifying_questions=" / ".join(f["clarifying_questions"]))
        writer.writerow({key: _csv_cell(value) for key, value in row.items()})
    return buffer.getvalue()
