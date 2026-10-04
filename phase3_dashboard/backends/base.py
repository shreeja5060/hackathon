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
        """Run Extractor -> Mapper -> Auditor on one chunk. Returns the state paused at human_review."""
        return self._app.invoke({"policy_chunk": chunk}, config=self._config(thread_id))

    def submit_review(self, thread_id: str, approved: list[dict]) -> dict:
        """Resume a paused thread with the findings the reviewer approved."""
        return self._app.invoke(self._command(resume={"approved": approved}), config=self._config(thread_id))

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
    def ask(self, question: str) -> dict[str, Any]:
        """Answer a question: {"answer": str, "citations": [search() results]}."""
