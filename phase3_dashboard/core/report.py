"""Builds the exported gap report. Only human-approved findings go in it.

AI text is escaped in the Markdown export, and CSV cells that a spreadsheet
would treat as formulas are neutralized (CSV injection).
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from .contracts import COVERAGE_VALUES
from .review import ReviewSession, counts, utc_now
from .security import escape_md

NOTICE = (
    "AI agents drafted these findings and recommendations. Every finding in this report "
    "was approved by the named reviewer, and edited recommendations are marked. "
    "Rejected findings are not included."
)
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
            "sections_failed": [{"section": s.label, "error": s.error} for s in run.section_errors],
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
        "recommendation": finding.final_recommendation,
        "recommendation_edited_by_reviewer": finding.recommendation_edited is not None,
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
    version = framework.get("version") or "unknown"
    lines += [
        f"- **Policy:** {_md(report['policy']['source'])}",
        f"- **Framework:** {_md(framework.get('title'))}, version {_md(version)}",
        f"- **Reviewed by:** {_md(review['finalized_by'])}, finalized {review['finalized_at']}",
        f"- **Decisions:** {review['approved']} approved, {review['rejected']} rejected",
        f"- **Generated:** {report['generated_at']}",
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
                recommendation += " _(edited by reviewer)_"
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
                f"- **Finding:** {_md(f['finding'])}",
                f"- **Recommendation:** {_md(f['recommendation'])}",
            ]
            if f["recommendation_edited_by_reviewer"]:
                lines.append(f"- **AI's original recommendation:** {_md(f.get('recommendation_ai_original'))}")
            lines += [
                f"- **Source:** {_md(_source(f['citation']))}",
                f"- **Approved by:** {_md(f['reviewer'])} at {f['reviewed_at']}",
            ]
            if f["reviewer_note"]:
                lines.append(f"- **Reviewer note:** {_md(f['reviewer_note'])}")
            lines.append("")

    failed = report["run"]["sections_failed"]
    if failed:
        lines += ["## Sections that could not be analyzed", ""]
        lines += [f"- {_md(s['section'])}: {_md(s['error'])}" for s in failed]
        lines.append("")

    lines += ["## Audit trail", "", "| Time (UTC) | Reviewer | Action | Finding |", "| --- | --- | --- | --- |"]
    for event in report["audit_log"]:
        lines.append(
            f"| {event['timestamp']} | {_md(event['reviewer'])} | {event['action']} "
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
    "id", "requirement", "coverage", "framework_control", "finding", "recommendation",
    "recommendation_edited_by_reviewer", "source", "locator", "page",
    "reviewer", "reviewed_at", "reviewer_note", "requirement_text",
)


def to_csv(report: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for f in report["approved_findings"]:
        row = {key: f.get(key) for key in CSV_COLUMNS}
        row.update(source=f["citation"]["source"], locator=f["citation"]["locator"], page=f["citation"]["page"])
        writer.writerow({key: _csv_cell(value) for key, value in row.items()})
    return buffer.getvalue()
