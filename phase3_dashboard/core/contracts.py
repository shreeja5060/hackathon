"""Data contracts between Phase 2 (the agents) and Phase 3 (this dashboard).

Every finding the agents produce goes through normalize_pipeline_state()
before the dashboard shows it. That function:

* checks each finding against the shape phase2_agents/pipeline.py returns
  (the auditor's entry plus the finding_id that start_review() adds),
* turns it into a typed Finding the UI can rely on,
* records structural problems (these block approval) and quality flags
  (shown to the reviewer, not blocking), and
* starts every finding as "pending", because only a person approves or rejects.

The simulator and the live pipeline both go through this function, so the
code path you test in simulator mode is the one used after the merge.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

COVERAGE_VALUES = ("Full", "Partial", "Missing", "Not observable")
GAP_COVERAGE = ("Partial", "Missing")
INVALID_COVERAGE = "Invalid"
STATUS_VALUES = ("pending", "approved", "rejected")
SEARCH_TYPES = ("internal", "framework", "evidence")
SEARCH_FIELDS = ("chunk_id", "text", "source", "page", "type", "doc_kind", "locator", "score")

# What pipeline.py's findings contain today, plus two fields the dashboard
# recovers from the mapper output. Anything else is kept as "Other fields".
KNOWN_FINDING_FIELDS = frozenset({
    "finding_id", "requirement", "coverage", "finding", "recommendation", "plain_language",
    "clarifying_questions", "citation", "framework_control", "status",
    "requirement_text", "mapping_reasoning",
})
MAX_FIELD_CHARS = 4000
MAX_QUESTIONS = 5
MAX_SUGGESTION_CHARS = 2000
MAX_SUGGESTIONS = 10
_NO_RECOMMENDATION = frozenset({"", "none", "n/a", "na", "null", "-", "—"})
_CONTROL_ID = re.compile(
    r"^(?:[A-Z]{2}-\d{1,2}(?:\(\d{1,2}\))?"  # SP 800-53, e.g. AC-2 or IA-2(1)
    r"|[A-Z]{2}\.[A-Z]{2}-\d{2})$"  # CSF 2.0, e.g. PR.AA-01
)


def is_control_id(value) -> bool:
    """True for SP 800-53 (AC-2, IA-2(1)) and CSF 2.0 (PR.AA-01) identifiers."""
    return isinstance(value, str) and bool(_CONTROL_ID.match(value))


class ContractError(ValueError):
    """Pipeline output that can't be used at all, e.g. findings with no review pause."""


@dataclass(frozen=True)
class Citation:
    source: str
    chunk_id: str | None = None
    locator: str | None = None
    page: int | None = None

    def label(self) -> str:
        parts = [self.source or "Unknown source"]
        if self.locator:
            parts.append(self.locator)
        if self.page is not None:
            parts.append(f"p. {self.page}")
        return ", ".join(parts)


@dataclass
class Finding:
    """One gap-report entry as the reviewer sees it."""

    finding_id: str
    thread_id: str
    position: int  # index inside its thread's gap_report
    requirement: str
    requirement_text: str | None
    coverage: str
    finding: str
    recommendation: str | None  # None means "no change needed"
    framework_control: str | None
    mapping_reasoning: str | None
    citation: Citation
    raw: dict[str, Any]  # the pipeline's original dict
    source_finding_id: str | None = None  # the pipeline's own ID (e.g. "F003"), used to send the decision back
    plain_language: str | None = None
    clarifying_questions: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)  # block approval
    flags: list[str] = field(default_factory=list)  # shown, not blocking
    status: str = "pending"
    reviewer: str | None = None
    reviewed_at: str | None = None
    reviewer_note: str = ""
    recommendation_edited: str | None = None
    recommendation_origin: str | None = None  # "reviewer" or "assistant" when the approved text isn't the agents'
    assistant_suggestion: str | None = None  # latest wording the Q&A assistant proposed; never applied by itself
    confirmed_by: str | None = None  # the second reviewer who confirmed the decision (two-person rule)
    confirmed_at: str | None = None
    confirmation_note: str = ""

    @property
    def approvable(self) -> bool:
        return not self.problems

    @property
    def decided(self) -> bool:
        return self.status != "pending"

    @property
    def confirmed(self) -> bool:
        return self.decided and self.confirmed_by is not None

    @property
    def final_recommendation(self) -> str | None:
        if self.recommendation_edited is not None:
            return self.recommendation_edited or None
        return self.recommendation


@dataclass
class SectionResult:
    thread_id: str
    chunk_id: str
    label: str
    page: int | None
    status: str  # "ok", "partial" (some items failed) or "error"
    finding_count: int = 0
    error: str | None = None
    paused: bool = False  # the pipeline thread is waiting for review decisions
    warnings: list[str] = field(default_factory=list)  # per-item failures the pipeline recorded
    retrieval: str | None = None  # which search backend Phase 2 used


@dataclass
class AnalysisRun:
    run_id: str
    backend_name: str
    simulated: bool
    policy_source: str
    policy_origin: str
    framework: dict[str, Any]
    started_at: str
    finished_at: str | None = None
    sections: list[SectionResult] = field(default_factory=list)

    @property
    def section_errors(self) -> list[SectionResult]:
        return [s for s in self.sections if s.status == "error"]

    @property
    def ok_threads(self) -> list[str]:
        """Threads paused at human review; each one gets the reviewer's decisions on finalize."""
        return [s.thread_id for s in self.sections if s.paused]

    @property
    def problem_sections(self) -> list[SectionResult]:
        return [s for s in self.sections if s.status != "ok"]

    @property
    def retrieval_backends(self) -> list[str]:
        return sorted({s.retrieval for s in self.sections if s.retrieval})


@dataclass(frozen=True)
class ChatAnswer:
    answer: str
    citations: tuple[dict[str, Any], ...]  # each carries "ref": its [n] number in the answer
    dropped_citations: int = 0
    suggestions: tuple[dict[str, str], ...] = ()  # {"finding_id", "recommendation"}
    dropped_suggestions: int = 0


# --------------------------------------------------------------------- helpers

def _clean_text(value, name, problems, flags, *, required=True):
    """Return stripped text, or None. Required fields record problems; optional ones flags."""
    sink = problems if required else flags
    if value is None:
        if required:
            problems.append(f"Missing '{name}'.")
        return None
    if not isinstance(value, str):
        sink.append(f"'{name}' should be text, but it's {type(value).__name__}.")
        return None
    text = value.strip()
    if not text:
        if required:
            problems.append(f"'{name}' is empty.")
        return None
    if len(text) > MAX_FIELD_CHARS:
        flags.append(f"'{name}' was shortened to {MAX_FIELD_CHARS} characters.")
        text = text[:MAX_FIELD_CHARS].rstrip() + "…"
    return text


def _positive_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def normalize_coverage(value) -> str | None:
    """Map 'full', ' Partial ' etc. to the exact allowed value, or None."""
    if not isinstance(value, str):
        return None
    key = " ".join(value.split()).lower()
    for option in COVERAGE_VALUES:
        if key == option.lower():
            return option
    return None


def _clean_recommendation(value, coverage, problems, flags):
    if value is None or (isinstance(value, str) and value.strip().lower() in _NO_RECOMMENDATION):
        if coverage in GAP_COVERAGE:
            flags.append("This gap has no recommendation.")
        return None
    text = _clean_text(value, "recommendation", problems, flags, required=False)
    if text and coverage == "Full":
        flags.append("Coverage is Full but a recommendation was given. Check that they agree.")
    return text


def _parse_citation(raw, problems, flags, policy_chunk) -> Citation:
    if not isinstance(raw, dict):
        problems.append("Missing 'citation'. Every finding needs a source.")
        return Citation(source="Unknown source")

    source = _clean_text(raw.get("source"), "citation.source", problems, flags)
    chunk_id = raw.get("chunk_id") if isinstance(raw.get("chunk_id"), str) else None
    locator = raw.get("locator") if isinstance(raw.get("locator"), str) else None
    page = _positive_int(raw.get("page"))

    if policy_chunk:
        # The auditor copies the citation from the chunk it was given, so any
        # mismatch means the citation was changed or invented along the way.
        if chunk_id and chunk_id != policy_chunk.get("chunk_id"):
            flags.append("The citation points to a different passage than the one analyzed.")
        if source and source != policy_chunk.get("source"):
            flags.append("The citation names a different file than the one analyzed.")
        if page is None and chunk_id and chunk_id == policy_chunk.get("chunk_id"):
            page = _positive_int(policy_chunk.get("page"))

    if not chunk_id:
        flags.append("The citation has no chunk_id, so the exact passage can't be shown.")
        chunk_id = None
    if not locator:
        flags.append("The citation has no section locator.")
        locator = None
    return Citation(source=source or "Unknown source", chunk_id=chunk_id, locator=locator, page=page)


def format_pipeline_errors(state) -> list[str]:
    """pipeline.py records per-item failures in state["errors"] instead of raising."""
    errors = state.get("errors") if isinstance(state, dict) else None
    lines = []
    for error in errors or []:
        if isinstance(error, dict):
            stage = error.get("stage") or "pipeline"
            subject = error.get("requirement")  # the section label already says which chunk
            message = " ".join(str(error.get("error", "")).split())
            line = f"{stage.capitalize()} failed" + (f" on {subject}" if subject else "") + f": {message}"
        else:
            line = " ".join(str(error).split())
        lines.append(line if len(line) <= 300 else line[:300] + "…")
    return lines


def _clean_questions(value, flags) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        flags.append("'clarifying_questions' should be a list.")
        return []
    questions = [" ".join(q.split()) for q in value if isinstance(q, str) and q.strip()]
    if len(questions) > MAX_QUESTIONS:
        flags.append(f"Only the first {MAX_QUESTIONS} clarifying questions are shown.")
    return [q[:MAX_FIELD_CHARS] for q in questions[:MAX_QUESTIONS]]


def read_review_pause(state) -> list:
    """Return the findings list from the pipeline's human_review pause.

    pipeline.py calls interrupt({"message": ..., "findings": [...]}), and
    LangGraph returns it under state["__interrupt__"][0].value.
    """
    if not isinstance(state, dict):
        raise ContractError("The pipeline returned something other than a state dictionary.")
    pauses = state.get("__interrupt__")
    if not pauses:
        raise ContractError(
            "The pipeline finished without pausing for human review, so its findings were not accepted."
        )
    try:
        first = pauses[0]
    except (TypeError, IndexError, KeyError) as exc:
        raise ContractError("The review pause has an unexpected format.") from exc
    payload = getattr(first, "value", first)
    if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list):
        raise ContractError("The review pause did not include a 'findings' list.")
    return payload["findings"]


# ------------------------------------------------------------------ main entry

def normalize_pipeline_state(state, *, thread_id: str, policy_chunk: dict | None = None) -> list[Finding]:
    """Turn one paused pipeline run into validated findings (IDs are set by the caller)."""
    raw_findings = read_review_pause(state)
    # The mapper's output carries requirement_text and mapping_reasoning that the
    # findings don't. Match by requirement name, because the auditor skips items
    # that failed, so positions can shift.
    unmatched = [m for m in (state.get("mapped") or []) if isinstance(m, dict)]
    seen_ids: set[str] = set()
    findings = []

    for position, raw in enumerate(raw_findings):
        problems: list[str] = []
        flags: list[str] = []
        if not isinstance(raw, dict):
            problems.append("This finding is not a JSON object.")
            raw = {"value": raw}

        requirement = _clean_text(raw.get("requirement"), "requirement", problems, flags)
        coverage = normalize_coverage(raw.get("coverage"))
        if coverage is None:
            problems.append(
                f"Coverage {raw.get('coverage')!r} isn't one of: {', '.join(COVERAGE_VALUES)}."
            )
            coverage = INVALID_COVERAGE
        finding_text = _clean_text(raw.get("finding"), "finding", problems, flags)
        recommendation = _clean_recommendation(raw.get("recommendation"), coverage, problems, flags)

        control = _clean_text(raw.get("framework_control"), "framework_control", problems, flags, required=False)
        if control is None:
            flags.append("No framework control was mapped.")
        elif not _CONTROL_ID.match(control):
            flags.append(f"'{control}' doesn't look like a NIST control ID.")

        citation = _parse_citation(raw.get("citation"), problems, flags, policy_chunk)

        incoming_status = raw.get("status")
        if incoming_status != "pending":
            flags.append(
                f"Arrived with status {incoming_status!r}. It was reset to 'pending' "
                "because only a reviewer can approve or reject."
            )

        source_id = raw.get("finding_id")
        if not isinstance(source_id, str) or not source_id.strip():
            problems.append("This finding has no finding_id, so a decision on it can't be sent back to the pipeline.")
            source_id = None
        elif source_id in seen_ids:
            problems.append(f"The pipeline sent two findings with the ID {source_id}.")
        else:
            seen_ids.add(source_id)

        plain = _clean_text(raw.get("plain_language"), "plain_language", problems, flags, required=False)
        questions = _clean_questions(raw.get("clarifying_questions"), flags)

        partner = next((m for m in unmatched if m.get("requirement") == raw.get("requirement")), {})
        if partner:
            unmatched.remove(partner)
        requirement_text = _clean_text(
            raw.get("requirement_text", partner.get("requirement_text")),
            "requirement_text", problems, flags, required=False,
        )
        reasoning = _clean_text(
            raw.get("mapping_reasoning", partner.get("mapping_reasoning")),
            "mapping_reasoning", problems, flags, required=False,
        )

        findings.append(Finding(
            finding_id=f"{thread_id}#{position}",
            thread_id=thread_id,
            position=position,
            requirement=requirement or "(unnamed requirement)",
            requirement_text=requirement_text,
            coverage=coverage,
            finding=finding_text or "(no finding text)",
            recommendation=recommendation,
            framework_control=control,
            mapping_reasoning=reasoning,
            citation=citation,
            raw=dict(raw),
            source_finding_id=source_id,
            plain_language=plain,
            clarifying_questions=questions,
            extras={k: v for k, v in raw.items() if k not in KNOWN_FINDING_FIELDS},
            problems=problems,
            flags=flags,
        ))
    return findings


# --------------------------------------------------------- search and chat

def validate_search_result(item) -> dict | None:
    """Return a clean copy of one search() result, or None if it's malformed.

    chunk_id, text, source and type are required. page, locator, doc_kind and
    score may be missing (shared/fake_search.py has no score) and become None.
    """
    if not isinstance(item, dict) or not {"chunk_id", "text", "source", "type"} <= item.keys():
        return None
    item = {key: item.get(key) for key in SEARCH_FIELDS}
    if not isinstance(item["text"], str) or not item["text"].strip():
        return None
    if not isinstance(item["source"], str) or not item["source"]:
        return None
    if item["type"] not in SEARCH_TYPES:
        return None
    page = item["page"]
    if page is not None and _positive_int(page) is None:
        return None
    if item["locator"] is not None and not isinstance(item["locator"], str):
        return None
    score = item["score"]
    if score is not None:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return None
        if not math.isfinite(score) or not -1.0 <= score <= 1.0:
            return None
    return item


def _clean_suggestions(raw, known_finding_ids) -> tuple[list[dict], int]:
    """Keep well-formed suggestions for findings that exist, one per finding."""
    if not isinstance(raw, list):
        return [], 0
    clean, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        finding_id, text = item.get("finding_id"), item.get("recommendation")
        if not isinstance(finding_id, str) or not isinstance(text, str) or not text.strip():
            continue
        if known_finding_ids is not None and finding_id not in known_finding_ids:
            continue
        if finding_id in seen or len(clean) == MAX_SUGGESTIONS:
            continue
        seen.add(finding_id)
        clean.append({"finding_id": finding_id, "recommendation": " ".join(text.split())[:MAX_SUGGESTION_CHARS]})
    return clean, len(raw) - len(clean)


def normalize_chat_answer(payload, known_finding_ids=None) -> ChatAnswer:
    """Validate a Q&A reply: {"answer": str, "citations": [search() results], "suggestions": [...]}.

    Each kept citation gets "ref", its position in the agent's list, so the
    answer's [1], [2] markers still point at the right source if a malformed
    citation is dropped. Suggestions are kept only for findings in the review.
    """
    if not isinstance(payload, dict):
        raise ContractError("The Q&A agent returned an unexpected format.")
    answer = payload.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise ContractError("The Q&A agent returned an empty answer.")
    raw = payload.get("citations") or []
    if not isinstance(raw, list):
        raw = []
    clean = []
    for ref, item in enumerate(raw, start=1):
        citation = validate_search_result(item)
        if citation is not None:
            clean.append({**citation, "ref": ref})
    suggestions, dropped = _clean_suggestions(payload.get("suggestions") or [], known_finding_ids)
    return ChatAnswer(
        answer=answer.strip()[:MAX_FIELD_CHARS],
        citations=tuple(clean),
        dropped_citations=len(raw) - len(clean),
        suggestions=tuple(suggestions),
        dropped_suggestions=dropped,
    )
