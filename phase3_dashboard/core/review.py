"""Human-in-the-loop review: the only place a finding's status changes.

The rules live here rather than in the UI, so a widget bug can't bypass them:

* every decision needs a named reviewer,
* findings that failed validation can't be approved,
* rejecting needs a reason,
* with the two-person rule on, a different reviewer confirms every decision
  (approvals and rejections) before the review can be signed off,
* nothing changes after the review is finalized,
* every action goes into an append-only audit log (and from there into the
  permanent ledger, see memory.py).
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
    two_person: bool = False  # every decision needs confirming by a different reviewer before sign-off
    # Where this review lives in the permanent record (memory.py). None when memory is off.
    ledger_run_id: int | None = None
    document_record: dict[str, Any] | None = None  # {"id", "name", "version", "sha256"} from the ledger
    synced_events: int = 0  # how many audit_log entries are already in the ledger
    ledger_seq: int = 0  # the last ledger entry for this run that this session knows about
    restored: bool = False  # rebuilt from the ledger rather than analyzed in this session
    closed_threads: set[str] = field(default_factory=set)  # paused runs that ended before sign-off (restart)

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


def same_person(a, b) -> bool:
    """Names match ignoring case and spacing, so "rashmi " can't confirm Rashmi's decision."""
    def key(name):
        return " ".join(str(name or "").split()).casefold()
    return bool(key(a)) and key(a) == key(b)


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

def approve(session, finding_id, reviewer, *, edited_recommendation=None, note="", bulk=False) -> None:
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

    # Was the approved text the assistant's wording, accepted as is?
    suggestion = (finding.assistant_suggestion or "").strip()
    from_assistant = edited is not None and bool(suggestion) and edited == suggestion

    finding.status = "approved"
    finding.reviewer = reviewer
    finding.reviewed_at = utc_now()
    finding.reviewer_note = note
    finding.recommendation_edited = edited
    finding.recommendation_origin = None if edited is None else ("assistant" if from_assistant else "reviewer")

    detail: dict[str, Any] = {"coverage": finding.coverage, "control": finding.framework_control}
    if note:
        detail["note"] = note
    if bulk:
        detail["bulk"] = True
    if edited is not None:
        detail["recommendation_before"] = finding.recommendation
        detail["recommendation_after"] = edited
    if from_assistant:
        action = "approved_with_ai_suggestion"
    elif edited is not None:
        action = "approved_with_edits"
        if finding.assistant_suggestion:
            detail["assistant_suggestion"] = finding.assistant_suggestion  # what the reviewer started from
    else:
        action = "approved"
    _log(session, reviewer, action, finding_id, **detail)


def bulk_approvable(session: ReviewSession) -> list[Finding]:
    """Pending Full-coverage findings with no validation problems or flags.

    These need no change, so the reviewer can approve them together. Anything
    flagged, partial or missing still gets a one-by-one decision.
    """
    return [f for f in session.findings
            if f.status == "pending" and f.coverage == "Full" and not f.problems and not f.flags]


def approve_all_full(session, reviewer, note="") -> int:
    """Approve every finding bulk_approvable() returns; each one is logged separately."""
    _ensure_open(session)
    reviewer = clean_reviewer(reviewer)
    note = _clean_note(note) or "Bulk approval: Full coverage, no flags"
    findings = bulk_approvable(session)
    for finding in findings:
        approve(session, finding.finding_id, reviewer, note=note, bulk=True)
    return len(findings)


def record_suggestion(session, finding_id, text, requested_by, question="") -> None:
    """Hold the assistant's suggested wording on a finding for the reviewer to accept or ignore.

    This never changes the finding's recommendation or status: the reviewer
    applies it with the Use this wording button and still has to approve.
    """
    finding = session.get(finding_id)
    if session.finalized:
        return
    text = " ".join(str(text or "").split())[:MAX_RECOMMENDATION_CHARS]
    if not text or text == finding.assistant_suggestion:
        return
    finding.assistant_suggestion = text
    who = " ".join(str(requested_by or "").split())[:MAX_REVIEWER_CHARS] or "Unnamed reviewer"
    detail: dict[str, Any] = {"suggestion": text}
    if question:
        detail["question"] = " ".join(str(question).split())[:MAX_NOTE_CHARS]
    _log(session, who, "assistant_suggested", finding_id, **detail)


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
    finding.recommendation_origin = None
    _log(session, reviewer, "rejected", finding_id,
         coverage=finding.coverage, control=finding.framework_control, reason=reason)


def reopen(session, finding_id, reviewer) -> None:
    """Undo a decision before the review is finalized."""
    _ensure_open(session)
    reviewer = clean_reviewer(reviewer)
    finding = session.get(finding_id)
    if finding.status == "pending":
        raise ReviewError(f"{finding_id} doesn't have a decision yet.")
    if session.two_person and finding.reviewer and not same_person(reviewer, finding.reviewer):
        raise ReviewError(f"Only {finding.reviewer} can undo this decision. As the second reviewer, send it back "
                          "with a reason instead.")
    previous = finding.status
    _clear_decision(finding)
    _log(session, reviewer, "reopened", finding_id, previous_status=previous)


def _clear_decision(finding: Finding) -> None:
    finding.status = "pending"
    finding.reviewer = None
    finding.reviewed_at = None
    finding.reviewer_note = ""
    finding.recommendation_edited = None
    finding.recommendation_origin = None
    _clear_confirmation(finding)


def _clear_confirmation(finding: Finding) -> None:
    finding.confirmed_by = None
    finding.confirmed_at = None
    finding.confirmation_note = ""


# ---------------------------------------------------------------- two-person rule

def awaiting_confirmation(session: ReviewSession) -> list[Finding]:
    """Decided findings that no second reviewer has confirmed yet."""
    return [f for f in session.findings if f.decided and not f.confirmed]


def confirm(session, finding_id, reviewer, note="") -> None:
    """A second reviewer confirms someone else's decision (approval or rejection)."""
    _ensure_open(session)
    reviewer = clean_reviewer(reviewer)
    finding = session.get(finding_id)
    if not finding.decided:
        raise ReviewError(f"{finding_id} has no decision to confirm yet.")
    if finding.confirmed:
        raise ReviewError(f"{finding_id} was already confirmed by {finding.confirmed_by}.")
    if same_person(reviewer, finding.reviewer):
        raise ReviewError(f"{reviewer} made the decision on {finding_id}, so a different person must confirm it.")
    note = _clean_note(note)
    finding.confirmed_by = reviewer
    finding.confirmed_at = utc_now()
    finding.confirmation_note = note
    detail: dict[str, Any] = {"decision": finding.status, "decided_by": finding.reviewer}
    if note:
        detail["note"] = note
    _log(session, reviewer, "confirmed", finding_id, **detail)


def confirm_all(session, reviewer, note="") -> tuple[int, int]:
    """Confirm every decision the reviewer didn't make. Returns (confirmed, skipped as their own)."""
    _ensure_open(session)
    reviewer = clean_reviewer(reviewer)
    note = _clean_note(note)
    confirmed = skipped = 0
    for finding in awaiting_confirmation(session):
        if same_person(reviewer, finding.reviewer):
            skipped += 1
            continue
        confirm(session, finding.finding_id, reviewer, note)
        confirmed += 1
    return confirmed, skipped


def send_back(session, finding_id, reviewer, note) -> None:
    """The second reviewer disagrees: the finding goes back to pending, with a reason."""
    _ensure_open(session)
    reviewer = clean_reviewer(reviewer)
    finding = session.get(finding_id)
    if not finding.decided:
        raise ReviewError(f"{finding_id} has no decision to send back.")
    note = _clean_note(note)
    if len(note) < MIN_REASON_CHARS:
        raise ReviewError("Say why you're sending it back, so the first reviewer knows what to change.")
    previous, decided_by = finding.status, finding.reviewer
    _clear_decision(finding)
    _log(session, reviewer, "sent_back", finding_id, previous_status=previous, decided_by=decided_by, note=note)


# ---------------------------------------------------------------- status

def counts(session: ReviewSession) -> dict[str, Any]:
    coverage = Counter(f.coverage for f in session.findings)
    status = Counter(f.status for f in session.findings)
    return {
        "total": len(session.findings),
        "coverage": {value: coverage.get(value, 0) for value in (*COVERAGE_VALUES, INVALID_COVERAGE)},
        "status": {value: status.get(value, 0) for value in STATUS_VALUES},
        "blocked": sum(1 for f in session.findings if f.problems),
        "confirmed": sum(1 for f in session.findings if f.confirmed),
        "awaiting_confirmation": len(awaiting_confirmation(session)) if session.two_person else 0,
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
    if session.two_person:
        waiting = len(awaiting_confirmation(session))
        if waiting:
            return False, (f"{waiting} decision(s) still need confirming by a second reviewer "
                           "(two-person rule).")
        return True, "Every decision is made and confirmed by a second reviewer."
    return True, "Every finding has a decision."


def decisions_by_thread(session: ReviewSession) -> dict[str, dict[str, dict]]:
    """What each paused pipeline thread gets back, in submit_decisions()'s format:

        {finding_id: {"decision": "approved" | "rejected", "recommendation": edited text (optional),
                      "reviewer": who decided, "confirmed_by": the second reviewer (two-person rule)}}

    pipeline.py reads "decision" and "recommendation"; the names travel with them so
    Phase 2 can record who decided. Rejections are sent too, so the pipeline's final
    record matches the review.
    Threads whose sections produced no findings get an empty dict, which still
    closes the paused run.
    """
    decisions: dict[str, dict[str, dict]] = {thread: {} for thread in session.run.ok_threads}
    for finding in session.findings:
        if finding.status == "pending" or not finding.source_finding_id:
            continue
        decision = {"decision": finding.status, "reviewer": finding.reviewer}
        if finding.confirmed_by:
            decision["confirmed_by"] = finding.confirmed_by
        if finding.status == "approved" and finding.recommendation_edited is not None:
            decision["recommendation"] = finding.recommendation_edited or "None"
        decisions.setdefault(finding.thread_id, {})[finding.source_finding_id] = decision
    return decisions


def assistant_context(session: ReviewSession | None) -> list[dict]:
    """The review's findings for the Q&A agent, under the IDs the reviewer sees.

    Each carries its status, the recommendation as it stands, the reviewer's
    note and the cited passage's text (so uploads that aren't indexed still
    have their evidence).
    """
    if session is None:
        return []
    context = []
    for f in session.findings:
        passage = session.passages.get(f.citation.chunk_id) if f.citation.chunk_id else None
        context.append({
            **f.raw,
            "finding_id": f.finding_id,
            "status": f.status,
            "recommendation": f.final_recommendation or "None",
            "requirement_text": f.requirement_text,
            "plain_language": f.plain_language,
            "clarifying_questions": list(f.clarifying_questions),
            "reviewer_note": f.reviewer_note or None,
            "decided_by": f.reviewer,
            "confirmed_by": f.confirmed_by,
            "cited_text": passage["text"] if passage else None,
        })
    return context


def undo_unsaved_sign_off(session: ReviewSession) -> None:
    """The permanent record refused the sign-off (the review changed in another session), so it didn't happen."""
    if session.audit_log and session.audit_log[-1].action == "finalized":
        session.audit_log.pop()
    session.finalized = False
    session.finalized_by = None
    session.finalized_at = None


def mark_finalized(session: ReviewSession, reviewer) -> None:
    ok, reason = can_finalize(session, reviewer)
    if not ok:
        raise ReviewError(reason)
    reviewer = clean_reviewer(reviewer)
    session.finalized = True
    session.finalized_by = reviewer
    session.finalized_at = utc_now()
    c = counts(session)
    detail: dict[str, Any] = {"approved": c["status"]["approved"], "rejected": c["status"]["rejected"]}
    if session.two_person:
        detail["two_person"] = True
        detail["confirmed"] = c["confirmed"]
    if session.closed_threads:
        detail["pipeline_runs_already_closed"] = len(session.closed_threads)
    _log(session, reviewer, "finalized", None, **detail)
