"""Runs a policy through the agent pipeline and hands the result to review.

pipeline.py analyzes one policy chunk per run and pauses at human_review.
So the dashboard starts one LangGraph thread per section, collects every
paused thread's findings into a single review, and, when the reviewer
finalizes, resumes each thread with its own approved findings.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from .contracts import AnalysisRun, ContractError, SectionResult, normalize_pipeline_state
from .review import ReviewError, ReviewSession, can_finalize, mark_finalized, resume_payloads, utc_now

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
        label = document.chunk_label(chunk)
        try:
            state = backend.run_section(chunk, thread_id)
            section_findings = normalize_pipeline_state(state, thread_id=thread_id, policy_chunk=chunk)
        except Exception as exc:  # noqa: BLE001 - one failed section must not sink the whole run
            log.exception("Analysis failed for %s", label)
            result = SectionResult(thread_id, chunk["chunk_id"], label, chunk.get("page"), "error",
                                   error=describe_error(exc))
        else:
            findings.extend(section_findings)
            result = SectionResult(thread_id, chunk["chunk_id"], label, chunk.get("page"), "ok",
                                   finding_count=len(section_findings))
        run.sections.append(result)
        if on_progress is not None:
            on_progress(index + 1, len(chunks), result)

    for number, finding in enumerate(findings, start=1):
        finding.finding_id = f"F-{number:03d}"
    run.finished_at = utc_now()
    return ReviewSession(run=run, findings=findings, passages={c["chunk_id"]: dict(c) for c in chunks})


def finalize_review(backend: "ComplianceBackend", session: ReviewSession, reviewer) -> list[str]:
    """Send each paused thread its approved findings, then lock the review.

    Returns a list of errors. The review is only finalized when it's empty;
    threads that resumed successfully aren't resumed again on a retry.
    """
    ok, reason = can_finalize(session, reviewer)
    if not ok:
        raise ReviewError(reason)

    errors = []
    for thread_id, approved in resume_payloads(session).items():
        if thread_id in session.resumed_threads:
            continue
        try:
            final_state = backend.submit_review(thread_id, approved)
            confirmed = final_state.get("approved_findings") if isinstance(final_state, dict) else None
            if not isinstance(confirmed, list) or len(confirmed) != len(approved):
                raise ContractError("The pipeline didn't confirm the approved findings.")
        except Exception as exc:  # noqa: BLE001 - report and let the reviewer retry
            log.exception("Resuming %s failed", thread_id)
            errors.append(f"{thread_id}: {describe_error(exc)}")
            continue
        session.resumed_threads.add(thread_id)

    if not errors:
        mark_finalized(session, reviewer)
    return errors
