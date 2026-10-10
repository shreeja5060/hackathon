"""Runs a policy through the agent pipeline and hands the result to review.

phase2_agents/pipeline.py exposes start_review(chunks) and
submit_decisions(run_id, decisions). The dashboard starts one paused run per
section, so it can show progress section by section and one bad section
can't hold up the rest. All paused runs are collected into one review. When
the reviewer finalizes, each run is resumed with its own decisions.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from .contracts import (
    AnalysisRun,
    ContractError,
    SectionResult,
    format_pipeline_errors,
    normalize_pipeline_state,
)
from .review import ReviewError, ReviewSession, can_finalize, decisions_by_thread, mark_finalized, utc_now

if TYPE_CHECKING:
    from ..backends.base import ComplianceBackend, PolicyDocument

log = logging.getLogger("compliance_copilot")

ProgressCallback = Callable[[int, int, SectionResult], None]


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def describe_error(exc: BaseException) -> str:
    """A short, single-line description that is safe to show in the UI."""
    message = " ".join(str(exc).split())
    if len(message) > 300:
        message = message[:300] + "…"
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _analyze_section(backend, chunk, thread_id, label) -> tuple[SectionResult, list]:
    base = {"thread_id": thread_id, "chunk_id": chunk["chunk_id"], "label": label, "page": chunk.get("page")}
    try:
        state = backend.run_section(chunk, thread_id)
    except Exception as exc:  # noqa: BLE001 - one failed section must not sink the whole run
        log.exception("Analysis failed for %s", label)
        return SectionResult(**base, status="error", error=describe_error(exc)), []

    warnings = format_pipeline_errors(state)
    retrieval = state.get("retrieval_backend") if isinstance(state, dict) else None
    try:
        findings = normalize_pipeline_state(state, thread_id=thread_id, policy_chunk=chunk)
    except ContractError as exc:
        return SectionResult(**base, status="error", error=str(exc), warnings=warnings, retrieval=retrieval), []

    if warnings and not findings:
        status, error = "error", warnings[0]
    elif warnings:
        status, error = "partial", None
    else:
        status, error = "ok", None
    result = SectionResult(**base, status=status, finding_count=len(findings), error=error,
                           paused=True, warnings=warnings, retrieval=retrieval)
    return result, findings


def analyze_policy(
    backend: "ComplianceBackend",
    document: "PolicyDocument",
    chunk_ids,
    *,
    on_progress: ProgressCallback | None = None,
) -> ReviewSession:
    wanted = set(chunk_ids)
    chunks = [chunk for chunk in document.chunks if chunk["chunk_id"] in wanted]
    if not chunks:
        raise ValueError("Choose at least one section to analyze.")

    run = AnalysisRun(
        run_id=new_run_id(),
        backend_name=backend.name,
        simulated=backend.simulated,
        policy_source=document.source,
        policy_origin=document.origin,
        framework=dict(backend.framework_info()),
        started_at=utc_now(),
    )
    findings = []
    for index, chunk in enumerate(chunks):
        thread_id = f"{run.run_id}-s{index:03d}"
        result, section_findings = _analyze_section(backend, chunk, thread_id, document.chunk_label(chunk))
        findings.extend(section_findings)
        run.sections.append(result)
        if on_progress is not None:
            on_progress(index + 1, len(chunks), result)

    for number, finding in enumerate(findings, start=1):
        finding.finding_id = f"F-{number:03d}"
    run.finished_at = utc_now()
    return ReviewSession(run=run, findings=findings, passages={c["chunk_id"]: dict(c) for c in chunks})


def _confirm(result, decisions: dict) -> None:
    """Check the pipeline recorded exactly the decisions it was sent."""
    final = result.get("findings") if isinstance(result, dict) else None
    if not isinstance(final, list):
        raise ContractError("The pipeline didn't return its final findings.")
    recorded = {f.get("finding_id"): f.get("status") for f in final if isinstance(f, dict)}
    for finding_id, decision in decisions.items():
        if recorded.get(finding_id) != decision["decision"]:
            raise ContractError(
                f"The pipeline recorded {finding_id} as {recorded.get(finding_id)!r}, not {decision['decision']!r}."
            )


def finalize_review(backend: "ComplianceBackend", session: ReviewSession, reviewer) -> list[str]:
    """Send each paused run its decisions, then lock the review.

    Returns a list of errors. The review is only finalized when it's empty;
    runs that resumed successfully aren't resumed again on a retry. A review
    reopened from the ledger skips runs that are no longer paused (the server
    restarted since): the ledger already holds its decisions.
    """
    ok, reason = can_finalize(session, reviewer)
    if not ok:
        raise ReviewError(reason)

    errors = []
    for thread_id, decisions in decisions_by_thread(session).items():
        if thread_id in session.resumed_threads or thread_id in session.closed_threads:
            continue
        if session.restored and not backend.is_paused(thread_id):
            # Reopened from the ledger after the paused run ended (a restart, or another session
            # already resumed it). Its decisions are in the ledger; there is nothing left to resume.
            session.closed_threads.add(thread_id)
            continue
        try:
            _confirm(backend.submit_review(thread_id, decisions), decisions)
        except Exception as exc:  # noqa: BLE001 - report and let the reviewer retry
            log.exception("Resuming %s failed", thread_id)
            errors.append(f"{thread_id}: {describe_error(exc)}")
            continue
        session.resumed_threads.add(thread_id)

    if not errors:
        mark_finalized(session, reviewer)
    return errors
