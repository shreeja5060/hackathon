"""The interface every backend implements.

The dashboard only talks to a ComplianceBackend. The simulator and the live
pipeline drive their graph the same way (run_section / submit_review below),
so switching backends changes where the data comes from, not how it's used.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class BackendUnavailable(RuntimeError):
    """The backend can't run here (missing code, packages, keys or data). Safe to show."""


@dataclass
class PolicyDocument:
    """A policy split into Phase 1 chunks: chunk_id, text, source, type, doc_kind, page, locator."""

    source: str
    chunks: list[dict[str, Any]]
    origin: str  # "upload", "indexed" or "sample"
    notes: list[str] = field(default_factory=list)

    @staticmethod
    def chunk_label(chunk: dict[str, Any]) -> str:
        if chunk.get("locator"):
            return chunk["locator"]
        if chunk.get("page") is not None:
            return f"Page {chunk['page']}"
        return "Untitled section"


class ComplianceBackend(ABC):
    name = "backend"
    simulated = False

    def __init__(self, app, command_cls):
        # `app` behaves like the compiled LangGraph in phase2_agents/pipeline.py;
        # `command_cls` like langgraph.types.Command.
        self._app = app
        self._command = command_cls

    @staticmethod
    def _config(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    def run_section(self, chunk: dict, thread_id: str) -> dict:
        """Run Extractor -> Mapper -> Auditor on one chunk and stop at human review.

        Returns the graph state: "mapped", "findings", "errors" and the pause
        under "__interrupt__" (whose value holds {"message", "findings"}).
        """
        empty = {key: [] for key in ("requirements", "mapped", "findings", "final_findings", "errors")}
        return self._app.invoke({"chunks": [chunk], **empty}, config=self._config(thread_id))

    def submit_review(self, thread_id: str, decisions: dict) -> dict:
        """Resume a paused run with {finding_id: {"decision", "recommendation"?}}.

        Returns {"findings": final findings with their status, "errors": [...]}.
        """
        state = self._app.invoke(self._command(resume=decisions), config=self._config(thread_id))
        return {"findings": state.get("final_findings", []), "errors": state.get("errors", [])}

    def is_paused(self, thread_id: str) -> bool:
        """Is this run still paused at human review in this process? (False after a restart.)"""
        try:
            state = self._app.get_state(self._config(thread_id))
        except Exception:  # noqa: BLE001 - no state means nothing to resume
            return False
        return bool(state and any(getattr(task, "interrupts", None) for task in state.tasks or ()))

    model_description: str | None = None  # which model wrote the findings, for the permanent record

    def policy_drafter(self):
        """A function that adapts starter-policy sections with AI, or None to use the baseline clauses."""
        return None

    @abstractmethod
    def list_policies(self) -> list[str]:
        """Names of policies that are already loaded (samples or Phase 1's index)."""

    @abstractmethod
    def load_policy(self, source: str) -> PolicyDocument:
        ...

    @abstractmethod
    def parse_upload(self, filename: str, data: bytes) -> PolicyDocument:
        """Validate an uploaded PDF and split it into chunks."""

    @abstractmethod
    def framework_info(self) -> dict[str, Any]:
        """Title, version and source of the framework, for the sidebar and the report."""

    @abstractmethod
    def ask(self, question: str, findings: list[dict] | None = None,
            history: list[dict] | None = None) -> dict[str, Any]:
        """Answer a question: {"answer": str, "citations": [search() results]}.

        findings (the current review) and history (earlier chat turns) give the
        Q&A agent context; backends that can't use them ignore them.
        """
