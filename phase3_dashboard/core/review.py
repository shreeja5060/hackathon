"""Human-in-the-loop review: the only place a finding's status changes.

The rules live here rather than in the UI, so a widget bug can't bypass them:

* every decision needs a named reviewer,
* findings that failed validation can't be approved,
* rejecting needs a reason,
* nothing changes after the review is finalized,
* every action goes into an append-only audit log.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .contracts import COVERAGE_VALUES, GAP_COVERAGE, INVALID_COVERAGE, STATUS_VALUES, AnalysisRun, Finding

MAX_REVIEWER_CHARS = 80
MIN_REASON_CHARS = 5
MAX_NOTE_CHARS = 1000
MAX_RECOMMENDATION_CHARS = 2000


class ReviewError(ValueError):
    """A review action isn't allowed. The message is safe to show."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class AuditEvent:
    timestamp: str
    reviewer: str
    action: str
    finding_id: str | None
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "reviewer": self.reviewer,
            "action": self.action,
            "finding_id": self.finding_id,
            "detail": dict(self.detail),
        }


@dataclass
class ReviewSession:
    run: AnalysisRun
    findings: list[Finding]
    passages: dict[str, dict]  # chunk_id -> analyzed policy chunk, for showing evidence
    audit_log: list[AuditEvent] = field(default_factory=list)
    finalized: bool = False
    finalized_by: str | None = None
    finalized_at: str | None = None
    resumed_threads: set[str] = field(default_factory=set)

    def get(self, finding_id: str) -> Finding:
        for finding in self.findings:
            if finding.finding_id == finding_id:
                return finding
        raise ReviewError(f"There is no finding called {finding_id}.")


# ---------------------------------------------------------------- helpers

def clean_reviewer(name) -> str:
    if not isinstance(name, str) or not " ".join(name.split()):
        raise ReviewError("Enter your name in the sidebar before reviewing.")
    cleaned = " ".join(name.split())
    if len(cleaned) > MAX_REVIEWER_CHARS:
        raise ReviewError(f"Reviewer names can be at most {MAX_REVIEWER_CHARS} characters.")
    if not cleaned.isprintable():
        raise ReviewError("The reviewer name contains characters that can't be stored.")
    return cleaned


def _clean_note(note) -> str:
    text = " ".join(str(note or "").split())
    if len(text) > MAX_NOTE_CHARS:
        raise ReviewError(f"Notes can be at most {MAX_NOTE_CHARS} characters.")
    return text


def _ensure_open(session: ReviewSession) -> None:
    if session.finalized:
        raise ReviewError("This review is finalized. Run a new analysis to make changes.")


def _log(session, reviewer, action, finding_id, **detail) -> None:
    session.audit_log.append(AuditEvent(utc_now(), reviewer, action, finding_id, detail))


def _pending(session: ReviewSession, finding_id: str) -> Finding:
    finding = session.get(finding_id)
    if finding.status != "pending":
        raise ReviewError(f"{finding_id} is already {finding.status}. Undo that decision first.")
    return finding


# ---------------------------------------------------------------- actions

def approve(session, finding_id, reviewer, *, edited_recommendation=None, note="") -> None:
    """Approve a finding, optionally with the reviewer's edited recommendation."""
    _ensure_open(session)
    reviewer = clean_reviewer(reviewer)
    finding = _pending(session, finding_id)
    if finding.problems:
        raise ReviewError(
            f"{finding_id} failed validation, so it can't be approved. Reject it with a reason instead."
        )
    note = _clean_note(note)

    edited = None
    if edited_recommendation is not None:
        proposed = str(edited_recommendation).strip()
        if len(proposed) > MAX_RECOMMENDATION_CHARS:
            raise ReviewError(f"Recommendations can be at most {MAX_RECOMMENDATION_CHARS} characters.")
        if proposed != (finding.recommendation or ""):
            if not proposed and finding.coverage in GAP_COVERAGE:
                raise ReviewError("A gap needs a recommendation. Write one, or restore the AI's text.")
            edited = proposed

    finding.status = "approved"
    finding.reviewer = reviewer
    finding.reviewed_at = utc_now()
    finding.reviewer_note = note
    finding.recommendation_edited = edited

    detail: dict[str, Any] = {"coverage": finding.coverage, "control": finding.framework_control}
    if note:
        detail["note"] = note
    if edited is not None:
        detail["recommendation_before"] = finding.recommendation
        detail["recommendation_after"] = edited
    _log(session, reviewer, "approved_with_edits" if edited is not None else "approved", finding_id, **detail)


def reject(session, finding_id, reviewer, reason) -> None:
    """Reject a finding. A short reason is required for the audit trail."""
    _ensure_open(session)
    reviewer = clean_reviewer(reviewer)
    finding = _pending(session, finding_id)
    reason = _clean_note(reason)
    if len(reason) < MIN_REASON_CHARS:
        raise ReviewError("Add a short reason in the note field so the rejection is explained.")

    finding.status = "rejected"
    finding.reviewer = reviewer
    finding.reviewed_at = utc_now()
    finding.reviewer_note = reason
    finding.recommendation_edited = None
    _log(session, reviewer, "rejected", finding_id,
         coverage=finding.coverage, control=finding.framework_control, reason=reason)


def reopen(session, finding_id, reviewer) -> None:
    """Undo a decision before the review is finalized."""
    _ensure_open(session)
    reviewer = clean_reviewer(reviewer)
    finding = session.get(finding_id)
    if finding.status == "pending":
        raise ReviewError(f"{finding_id} doesn't have a decision yet.")
    previous = finding.status
    finding.status = "pending"
    finding.reviewer = None
    finding.reviewed_at = None
    finding.reviewer_note = ""
    finding.recommendation_edited = None
    _log(session, reviewer, "reopened", finding_id, previous_status=previous)


# ---------------------------------------------------------------- status

def counts(session: ReviewSession) -> dict[str, Any]:
    coverage = Counter(f.coverage for f in session.findings)
    status = Counter(f.status for f in session.findings)
    return {
        "total": len(session.findings),
        "coverage": {value: coverage.get(value, 0) for value in (*COVERAGE_VALUES, INVALID_COVERAGE)},
        "status": {value: status.get(value, 0) for value in STATUS_VALUES},
        "blocked": sum(1 for f in session.findings if f.problems),
    }


def can_finalize(session: ReviewSession, reviewer) -> tuple[bool, str]:
    if session.finalized:
        return False, "This review is already finalized."
    try:
        clean_reviewer(reviewer)
    except ReviewError as exc:
        return False, str(exc)
    if not session.run.ok_threads:
        return False, "No section was analyzed successfully, so there is nothing to finalize."
    pending = sum(1 for f in session.findings if f.status == "pending")
    if pending:
        return False, f"{pending} finding(s) still need a decision."
    return True, "Every finding has a decision."


def decisions_by_thread(session: ReviewSession) -> dict[str, dict[str, dict]]:
    """What each paused pipeline thread gets back, in submit_decisions()'s format:

        {finding_id: {"decision": "approved" | "rejected", "recommendation": edited text (optional)}}

    Rejections are sent too, so the pipeline's final record matches the review.
    Threads whose sections produced no findings get an empty dict, which still
    closes the paused run.
    """
    decisions: dict[str, dict[str, dict]] = {thread: {} for thread in session.run.ok_threads}
    for finding in session.findings:
        if finding.status == "pending" or not finding.source_finding_id:
            continue
        decision = {"decision": finding.status}
        if finding.status == "approved" and finding.recommendation_edited is not None:
            decision["recommendation"] = finding.recommendation_edited or "None"
        decisions.setdefault(finding.thread_id, {})[finding.source_finding_id] = decision
    return decisions


def mark_finalized(session: ReviewSession, reviewer) -> None:
    ok, reason = can_finalize(session, reviewer)
    if not ok:
        raise ReviewError(reason)
    reviewer = clean_reviewer(reviewer)
    session.finalized = True
    session.finalized_by = reviewer
    session.finalized_at = utc_now()
    c = counts(session)
    _log(session, reviewer, "finalized", None,
         approved=c["status"]["approved"], rejected=c["status"]["rejected"])
