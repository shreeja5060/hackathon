"""The dashboard's permanent memory, built on Shreeja's ledger (shared/ledger.py).

A review used to live only as long as the browser session. With memory on, everything
that matters is written to the ledger as it happens:

* each policy document, by name and content hash, with versions (uploaded PDFs and
  drafted policies are also saved, so the evidence itself is kept),
* each analysis with all of its findings, in enough detail to reopen the review later,
* each review action (decisions, edits, assistant suggestions, confirmations by the
  second reviewer, sign-off) in the ledger's append-only, hash-chained trail.

The review itself still runs in a ReviewSession (review.py). After every action the
dashboard calls sync(), which writes the new audit entries to the ledger. Any later
session, on any machine that shares the ledger, can reopen the review with restore():
that is how a second person confirms decisions under the two-person rule.

Two sessions working on the same review: before writing, sync() checks that nobody else
has added to that review since this session last read it. If they have, it refuses
(StaleReview) and the reviewer reopens the latest version from the library. The ledger
also enforces the two-person rule itself, so a stale page can't confirm its own decision.

Simulator runs are kept in a separate file, so demo data never mixes with real records.

Settings
    LEDGER_PATH            the ledger file (default data/ledger.sqlite3); the simulator uses
                           the same name with "-simulator" added
    COPILOT_DOCUMENT_DIR   where uploaded PDFs and drafted policies are saved
                           (default: a "documents" folder next to the ledger)
    COPILOT_MEMORY=off     turn memory off (reviews then last only as long as the session)
"""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from pathlib import Path
from typing import Any

from .contracts import AnalysisRun, Citation, Finding, SectionResult
from .review import AuditEvent, ReviewSession

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = REPO_ROOT / "data" / "ledger.sqlite3"

# Dashboard audit actions -> how they're written to the ledger.
APPROVALS = ("approved", "approved_with_edits", "approved_with_ai_suggestion")
ACTION_LABELS = {
    "document_added": "Document added",
    "analysis_run": "Analysis recorded",
    "approved_first": "Approved",
    "approved_final": "Approval confirmed",
    "rejected": "Rejected",
    "rejection_confirmed": "Rejection confirmed",
    "reopened": "Reopened",
    "assistant_suggested": "Assistant suggested wording",
    "review_signed": "Review signed off",
    "policy_drafted": "Starter policy drafted",
    "policy_approved": "Starter policy approved",
}


class MemoryUnavailable(RuntimeError):
    """The ledger can't be opened. Safe to show."""


class StaleReview(RuntimeError):
    """Someone else changed this review since this session read it. Safe to show."""


def enabled() -> bool:
    return os.getenv("COPILOT_MEMORY", "on").strip().lower() not in {"off", "0", "false", "no"}


def default_paths(simulated: bool) -> tuple[Path, Path]:
    ledger = Path(os.getenv("LEDGER_PATH") or DEFAULT_LEDGER)
    documents = Path(os.getenv("COPILOT_DOCUMENT_DIR") or ledger.parent / "documents")
    if simulated:
        ledger = ledger.with_name(f"{ledger.stem}-simulator{ledger.suffix or '.sqlite3'}")
        documents = documents.with_name(f"{documents.name}-simulator")
    return ledger, documents


def document_content(document) -> bytes:
    """What identifies a loaded (not uploaded) policy's version: the text of its sections."""
    return "\n\n".join(chunk.get("text", "") for chunk in document.chunks).encode("utf-8")


# ------------------------------------------------------------------ findings <-> stored snapshots

def finding_record(finding: Finding, passage: dict | None) -> dict[str, Any]:
    """Everything needed to show the finding again later, as the ledger's snapshot."""
    return {
        "finding_id": finding.finding_id,
        "requirement": finding.requirement,
        "requirement_text": finding.requirement_text,
        "coverage": finding.coverage,
        "finding": finding.finding,
        "recommendation": finding.recommendation,
        "framework_control": finding.framework_control,
        "mapping_reasoning": finding.mapping_reasoning,
        "plain_language": finding.plain_language,
        "clarifying_questions": list(finding.clarifying_questions),
        "citation": {"source": finding.citation.source, "chunk_id": finding.citation.chunk_id,
                     "locator": finding.citation.locator, "page": finding.citation.page},
        "thread_id": finding.thread_id,
        "position": finding.position,
        "source_finding_id": finding.source_finding_id,
        "problems": list(finding.problems),
        "flags": list(finding.flags),
        "extras": dict(finding.extras),
        "raw": dict(finding.raw),
        "passage": dict(passage) if passage else None,
    }


def finding_from_record(record: dict) -> Finding:
    citation = record.get("citation") or {}
    return Finding(
        finding_id=record["finding_id"],
        thread_id=record.get("thread_id") or "",
        position=record.get("position") or 0,
        requirement=record.get("requirement") or "(unnamed requirement)",
        requirement_text=record.get("requirement_text"),
        coverage=record.get("coverage") or "Invalid",
        finding=record.get("finding") or "(no finding text)",
        recommendation=record.get("recommendation"),
        framework_control=record.get("framework_control"),
        mapping_reasoning=record.get("mapping_reasoning"),
        citation=Citation(source=citation.get("source") or "Unknown source", chunk_id=citation.get("chunk_id"),
                          locator=citation.get("locator"), page=citation.get("page")),
        raw=dict(record.get("raw") or {}),
        source_finding_id=record.get("source_finding_id"),
        plain_language=record.get("plain_language"),
        clarifying_questions=list(record.get("clarifying_questions") or []),
        extras=dict(record.get("extras") or {}),
        problems=list(record.get("problems") or []),
        flags=list(record.get("flags") or []),
    )


def _section_record(section: SectionResult) -> dict[str, Any]:
    return {"thread_id": section.thread_id, "chunk_id": section.chunk_id, "label": section.label,
            "page": section.page, "status": section.status, "finding_count": section.finding_count,
            "error": section.error, "paused": section.paused, "warnings": list(section.warnings),
            "retrieval": section.retrieval}


# ------------------------------------------------------------------ replaying the trail

def _decision_state() -> dict[str, Any]:
    return {"status": "pending", "decided_by": None, "decided_at": None, "note": "", "edited": None,
            "origin": None, "confirmed_by": None, "confirmed_at": None, "confirmation_note": "",
            "suggestion": None}


def replay(events: list[dict]) -> tuple[dict[str, dict], dict[str, Any]]:
    """Each finding's review state, and the run's sign-off, from the ledger entries of one run."""
    findings: dict[str, dict] = {}
    signed: dict[str, Any] = {}
    for event in events:
        fid, action, detail = event.get("finding_id"), event["action"], event.get("detail") or {}
        if action == "review_signed":
            signed = {"by": event["actor"], "at": event["ts"], **detail}
            continue
        if not fid:
            continue
        state = findings.setdefault(fid, _decision_state())
        if action == "approved_first":
            suggestion = state["suggestion"]
            state.update(_decision_state(), suggestion=suggestion, status="approved", decided_by=event["actor"],
                         decided_at=event["ts"], note=detail.get("note") or "",
                         edited=detail.get("recommendation_after"),
                         origin={"approved_with_ai_suggestion": "assistant",
                                 "approved_with_edits": "reviewer"}.get(detail.get("kind")))
        elif action == "rejected":
            suggestion = state["suggestion"]
            state.update(_decision_state(), suggestion=suggestion, status="rejected", decided_by=event["actor"],
                         decided_at=event["ts"], note=detail.get("note") or "")
        elif action in ("approved_final", "rejection_confirmed"):
            expected = "approved" if action == "approved_final" else "rejected"
            if state["status"] == expected:
                state.update(confirmed_by=event["actor"], confirmed_at=event["ts"],
                             confirmation_note=detail.get("note") or "")
        elif action == "reopened":
            suggestion = state["suggestion"]
            state.update(_decision_state(), suggestion=suggestion)
        elif action == "assistant_suggested":
            state["suggestion"] = detail.get("suggestion") or state["suggestion"]
    return findings, signed


def _audit_event(event: dict) -> AuditEvent | None:
    """A ledger entry as the dashboard's AuditEvent (None for entries the review log doesn't show)."""
    action, detail = event["action"], dict(event.get("detail") or {})
    if action == "approved_first":
        name = detail.pop("kind", "approved")
    elif action in ("approved_final", "rejection_confirmed"):
        name = "confirmed"
    elif action == "reopened":
        name = "sent_back" if detail.pop("sent_back", False) else "reopened"
    elif action == "review_signed":
        name = "finalized"
    elif action == "rejected":
        name = "rejected"
        if "note" in detail:
            detail["reason"] = detail.pop("note")
    elif action in ("assistant_suggested",):
        name = action
    else:
        return None
    return AuditEvent(event["ts"], event["actor"], name, event.get("finding_id"), detail)


# ------------------------------------------------------------------ the memory

class Memory:
    def __init__(self, ledger_path: Path | str, documents_dir: Path | str):
        try:
            from shared.ledger import ApprovalError, Ledger
        except ImportError as exc:  # pragma: no cover - the ledger ships with the repo
            raise MemoryUnavailable("shared/ledger.py isn't on this branch, so reviews can't be kept.") from exc
        self.ApprovalError = ApprovalError
        try:
            self.ledger = Ledger(ledger_path)
        except Exception as exc:  # noqa: BLE001 - a read-only or missing disk shouldn't crash the app
            raise MemoryUnavailable(f"The ledger at {ledger_path} can't be opened ({type(exc).__name__}: {exc}).") \
                from exc
        self.path = Path(ledger_path)
        self.documents_dir = Path(documents_dir)

    # -------------------------------------------------------------- documents

    def register(self, document, content: bytes | None, actor: str | None, extension: str = ".pdf") -> dict:
        """Note a policy when it's loaded. Returns the ledger's version info plus what's known about it."""
        raw = content if content is not None else document_content(document)
        info = self.ledger.register_document(document.source, raw)
        if not info["already_seen"]:
            self.ledger.record_event(actor or "system", "document_added", role="reviewer" if actor else "system",
                                     detail={"document": document.source, "version": info["version"],
                                             "sha256": info["sha256"], "origin": document.origin,
                                             "sections": len(document.chunks)})
        if content is not None:
            self._save_file(info["sha256"], content, extension)
        info["runs"] = [r for r in self.ledger.runs(document.source) if r["version"] == info["version"]]
        info["previous_summary"] = None
        if info["is_new_version"]:
            previous = [r for r in self.ledger.runs(document.source) if r["version"] == info["previous_version"]]
            if previous:
                info["previous_summary"] = self.run_summary(previous[-1]["id"])
        return info

    def _save_file(self, sha256: str, content: bytes, extension: str) -> None:
        path = self.documents_dir / f"{sha256}{extension}"
        if path.exists():
            return
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(content)
        tmp.replace(path)  # content-addressed: the name is the file's own hash

    def stored_file(self, sha256: str) -> Path | None:
        for path in self.documents_dir.glob(f"{sha256}.*"):
            if path.suffix != ".part" and hashlib.sha256(path.read_bytes()).hexdigest() == sha256:
                return path
        return None

    # -------------------------------------------------------------- analyses

    def record_analysis(self, session: ReviewSession, document_info: dict, actor: str | None,
                        model: str | None = None) -> int:
        run = session.run
        records = [finding_record(f, session.passages.get(f.citation.chunk_id) if f.citation.chunk_id else None)
                   for f in session.findings]
        detail = {
            "run_key": run.run_id, "policy_source": run.policy_source, "policy_origin": run.policy_origin,
            "simulated": run.simulated, "framework": dict(run.framework), "started_at": run.started_at,
            "finished_at": run.finished_at, "sections": [_section_record(s) for s in run.sections],
            "two_person": session.two_person, "version": document_info.get("version"),
        }
        run_id = self.ledger.record_run(document_info["id"], records, backend=run.backend_name, model=model,
                                        actor=actor or "system", detail=detail)
        session.ledger_run_id = run_id
        session.document_record = {key: document_info.get(key) for key in ("id", "name", "version", "sha256")}
        session.synced_events = len(session.audit_log)
        session.ledger_seq = self.ledger.last_seq(run_id)
        return run_id

    # -------------------------------------------------------------- decisions

    def check_fresh(self, session: ReviewSession) -> None:
        if session.ledger_run_id is None:
            return
        latest = self.ledger.last_seq(session.ledger_run_id)
        if latest > session.ledger_seq:
            last = self.ledger.events(run_id=session.ledger_run_id)[-1]
            raise StaleReview(f"{last['actor']} changed this review at {last['ts'][11:16]} UTC in another session. "
                              "Reopen it from the library to see the latest decisions.")

    def unsynced(self, session: ReviewSession) -> int:
        return max(0, len(session.audit_log) - session.synced_events) if session.ledger_run_id else 0

    def sync(self, session: ReviewSession) -> int:
        """Write the session's new audit entries to the ledger, in order. Returns how many were written."""
        if session.ledger_run_id is None:
            return 0
        pending = session.audit_log[session.synced_events:]
        if not pending:
            return 0
        self.check_fresh(session)
        written = 0
        for event in pending:
            try:
                seq = self._write(session, event)
            except self.ApprovalError as exc:
                raise StaleReview(f"The ledger refused this step: {exc}. Reopen the review from the library.") from exc
            session.synced_events += 1
            session.ledger_seq = max(session.ledger_seq, seq or 0)
            written += 1
        return written

    def _write(self, session: ReviewSession, event: AuditEvent) -> int:
        run, fid, actor, detail = session.ledger_run_id, event.finding_id, event.reviewer, dict(event.detail)
        record = self.ledger.record_event
        if event.action in APPROVALS:
            return record(actor, "approved_first", run, fid, "reviewer", {**detail, "kind": event.action},
                          ts=event.timestamp)
        if event.action == "rejected":
            detail["note"] = detail.pop("reason", "")
            return record(actor, "rejected", run, fid, "reviewer", detail, ts=event.timestamp)
        if event.action == "confirmed":
            if detail.get("decision") == "approved":
                return self.ledger.approve_final(actor, run, fid, detail.get("note") or None)
            return record(actor, "rejection_confirmed", run, fid, "approver", detail, ts=event.timestamp)
        if event.action == "sent_back":
            return record(actor, "reopened", run, fid, "approver", {**detail, "sent_back": True}, ts=event.timestamp)
        if event.action == "reopened":
            return record(actor, "reopened", run, fid, "reviewer", detail, ts=event.timestamp)
        if event.action == "finalized":
            return record(actor, "review_signed", run, None, "approver", detail, ts=event.timestamp)
        role = "reviewer" if event.action == "assistant_suggested" else None
        return record(actor, event.action, run, fid, role, detail, ts=event.timestamp)

    # -------------------------------------------------------------- reopening a review

    def restore(self, run_id: int) -> ReviewSession:
        events = self.ledger.events(run_id=run_id)
        head = next((e for e in events if e["action"] == "analysis_run"), None)
        if head is None:
            raise MemoryUnavailable(f"Review {run_id} isn't in the ledger.")
        meta = head["detail"]
        run = AnalysisRun(
            run_id=meta.get("run_key") or f"ledger-{run_id}",
            backend_name=meta.get("backend") or "unknown",
            simulated=bool(meta.get("simulated")),
            policy_source=meta.get("policy_source") or head.get("document") or "Unknown policy",
            policy_origin=meta.get("policy_origin") or "upload",
            framework=dict(meta.get("framework") or {}),
            started_at=meta.get("started_at") or head["ts"],
            finished_at=meta.get("finished_at") or head["ts"],
            sections=[SectionResult(**{k: s.get(k) for k in SectionResult.__dataclass_fields__ if k in s})
                      for s in meta.get("sections") or []],
        )
        records = self.ledger.run_findings(run_id)
        findings = [finding_from_record(record) for record in records]
        passages = {r["passage"]["chunk_id"]: r["passage"] for r in records if r.get("passage")}
        session = ReviewSession(run=run, findings=findings, passages=passages,
                                two_person=bool(meta.get("two_person")))

        states, signed = replay(events)
        for finding in findings:
            state = states.get(finding.finding_id)
            if not state:
                continue
            finding.status = state["status"]
            finding.reviewer = state["decided_by"]
            finding.reviewed_at = state["decided_at"]
            finding.reviewer_note = state["note"]
            finding.recommendation_edited = state["edited"]
            finding.recommendation_origin = state["origin"]
            finding.confirmed_by = state["confirmed_by"]
            finding.confirmed_at = state["confirmed_at"]
            finding.confirmation_note = state["confirmation_note"]
            finding.assistant_suggestion = state["suggestion"]
        if signed:
            session.finalized, session.finalized_by, session.finalized_at = True, signed["by"], signed["at"]
        session.audit_log = [entry for entry in map(_audit_event, events) if entry is not None]
        session.ledger_run_id = run_id
        session.document_record = {"id": None, "name": head.get("document"), "version": head.get("version"),
                                   "sha256": None}
        docs = [d for d in self.ledger.documents(head.get("document")) if d["version"] == head.get("version")]
        if docs:
            session.document_record.update(id=docs[0]["id"], sha256=docs[0]["sha256"])
        session.synced_events = len(session.audit_log)
        session.ledger_seq = self.ledger.last_seq(run_id)
        session.restored = True
        return session

    # -------------------------------------------------------------- the library

    def run_summary(self, run_id: int) -> dict[str, Any]:
        events = self.ledger.events(run_id=run_id)
        head = next((e for e in events if e["action"] == "analysis_run"), {"detail": {}, "ts": None})
        records = self.ledger.run_findings(run_id)
        states, signed = replay(events)
        two_person = bool(head["detail"].get("two_person"))
        status = Counter()
        for record in records:
            state = states.get(record["finding_id"], _decision_state())
            status[state["status"]] += 1
            if state["status"] != "pending" and not state["confirmed_by"]:
                status["unconfirmed"] += 1
        total = len(records)
        decided = total - status["pending"]
        waiting = status["unconfirmed"] if two_person else 0
        if signed:
            label = f"Signed off by {signed['by']}"
        elif status["pending"]:
            label = f"In review: {decided} of {total} decided"
        elif waiting:
            label = f"Waiting for a second reviewer ({waiting})"
        else:
            label = "Ready to sign off"
        return {
            "run_id": run_id, "created_at": head.get("ts"), "backend": head["detail"].get("backend"),
            "findings": total, "decided": decided, "pending": status["pending"], "approved": status["approved"],
            "rejected": status["rejected"], "awaiting_confirmation": waiting, "two_person": two_person,
            "coverage": dict(Counter(r.get("coverage") for r in records)),
            "gaps": sum(1 for r in records if r.get("coverage") in ("Partial", "Missing")),
            "signed": signed or None, "label": label, "simulated": bool(head["detail"].get("simulated")),
            "last_activity": events[-1]["ts"] if events else None,
            "last_actor": events[-1]["actor"] if events else None,
        }

    def library(self) -> list[dict[str, Any]]:
        """One row per policy (its latest version), newest activity first."""
        documents = self.ledger.documents()
        runs = self.ledger.runs()
        rows = []
        for name in dict.fromkeys(d["name"] for d in documents):
            versions = [d for d in documents if d["name"] == name]
            latest = versions[-1]
            doc_runs = [r for r in runs if r["document"] == name]
            latest_runs = [r for r in doc_runs if r["version"] == latest["version"]]
            summary = self.run_summary(latest_runs[-1]["id"]) if latest_runs else None
            rows.append({
                "name": name, "version": latest["version"], "versions": len(versions), "sha256": latest["sha256"],
                "first_seen": versions[0]["created_at"], "added": latest["created_at"],
                "runs": len(doc_runs), "latest": summary,
                "last_activity": (summary or {}).get("last_activity") or latest["created_at"],
            })
        rows.sort(key=lambda row: row["last_activity"] or "", reverse=True)
        return rows

    def document_history(self, name: str) -> dict[str, Any]:
        versions = self.ledger.documents(name)
        runs = self.ledger.runs(name)
        return {
            "versions": versions,
            "runs": [{**r, **self.run_summary(r["id"])} for r in runs],
        }

    def compare(self, name: str, old_version: int, new_version: int) -> dict | None:
        try:
            return self.ledger.compare_versions(name, old_version, new_version)
        except ValueError:
            return None

    def prior_decisions(self, session: ReviewSession) -> dict[str, list[dict]]:
        """For this review's sections whose text is unchanged since an earlier version: what was decided then."""
        if session.ledger_run_id is None or not session.document_record:
            return {}
        sections = sorted({f.citation.chunk_id for f in session.findings if f.citation.chunk_id})
        try:
            return self.ledger.prior_decisions(session.document_record["name"], sections,
                                               before_run_id=session.ledger_run_id)
        except Exception:  # noqa: BLE001 - earlier decisions are extra context, never a blocker
            return {}

    def statements(self) -> list[dict[str, Any]]:
        """What every stored policy says, per control: the latest analysis of each document's latest version.

        Rejected findings are left out (a reviewer said they're wrong). Used to find overlaps and conflicts.
        """
        out = []
        for row in self.library():
            summary = row["latest"]
            if not summary:
                continue
            run_id = summary["run_id"]
            states, _ = replay(self.ledger.events(run_id=run_id))
            for record in self.ledger.run_findings(run_id):
                state = states.get(record["finding_id"], _decision_state())
                if state["status"] == "rejected" or not record.get("framework_control"):
                    continue
                out.append({
                    "policy": row["name"], "version": row["version"], "run_id": run_id,
                    "finding_id": record["finding_id"], "requirement": record.get("requirement"),
                    "requirement_text": record.get("requirement_text"), "control": record["framework_control"],
                    "coverage": record.get("coverage"), "locator": (record.get("citation") or {}).get("locator"),
                    "status": state["status"], "confirmed": bool(state["confirmed_by"]),
                })
        return out

    def trail(self, limit: int | None = None) -> list[dict]:
        events = self.ledger.events()
        events.reverse()
        return events[:limit] if limit else events

    def verify(self) -> dict:
        return self.ledger.verify_chain()

    def note(self, name: str) -> str:
        return self.ledger.memory_note(name)

    def record(self, actor: str, action: str, detail: dict | None = None) -> int:
        """A trail entry that isn't part of a review (e.g. a starter policy was drafted)."""
        return self.ledger.record_event(actor or "system", action, detail=detail or {})
