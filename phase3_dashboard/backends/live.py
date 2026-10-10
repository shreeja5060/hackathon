"""Live backend: connects the dashboard to the real Phase 1 and Phase 2 code on main.

It doesn't change their code; it only calls it:

* phase2_agents/pipeline.py ........... start_review([chunk], run_id) runs Extractor -> Mapper ->
                                         Auditor and pauses; submit_decisions(run_id, decisions)
                                         resumes it with the reviewer's choices
* phase1_ingestion/chunk_policies.py .. chunk_pages(), to split uploaded PDFs
* data/processed/policy_chunks.json ... the policies Phase 1 already indexed
* chroma_db/index_manifest.json ....... the framework version, for the sidebar and report
* qa_agent (Anu's Q&A agent) .......... used for the chat when it's on the branch

pipeline.py quietly falls back to shared/fake_search.py when Phase 1's
retriever can't load. The dashboard reports which one is in use, so placeholder
results are never mistaken for real ones.

COPILOT_QA_MODULE="package.module:function" overrides which Q&A function is used.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import os
import sys
import threading
from pathlib import Path

from ..core.security import UploadRejected, check_upload
from .base import BackendUnavailable, ComplianceBackend, PolicyDocument
from .pdf_text import parse_pdf_pages

REPO_ROOT = Path(__file__).resolve().parents[2]
SP80053_FILE = "NIST_SP-800-53_rev5_catalog.json"

_LOCK = threading.Lock()
_PIPELINES: dict[str, object] = {}  # one loaded pipeline per file, shared by all sessions


def _short(exc: BaseException) -> str:
    message = " ".join(str(exc).split())[:200]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _load_pipeline(repo_root: Path):
    path = repo_root / "phase2_agents" / "pipeline.py"
    if not path.is_file():
        raise BackendUnavailable(
            "Phase 2 isn't on this branch yet (phase2_agents/pipeline.py is missing). "
            "Merge or pull phase2-shreeja, or use the simulator."
        )
    key = str(path.resolve())
    with _LOCK:
        if key not in _PIPELINES:
            spec = importlib.util.spec_from_file_location(f"copilot_live_pipeline_{len(_PIPELINES)}", path)
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as exc:  # noqa: BLE001 - surface any import problem as a clear message
                raise BackendUnavailable(
                    f"Phase 2's pipeline couldn't be loaded ({_short(exc)}). Install the repo's requirements "
                    "and set ANTHROPIC_API_KEY in .env."
                ) from exc
            if not hasattr(module, "app"):
                raise BackendUnavailable("phase2_agents/pipeline.py doesn't define the compiled graph `app`.")
            _PIPELINES[key] = module
        return _PIPELINES[key]


class LiveBackend(ComplianceBackend):
    name = "Live pipeline"
    simulated = False

    def __init__(self, repo_root: Path | str = REPO_ROOT):
        self._root = Path(repo_root).resolve()
        if str(self._root) not in sys.path:
            sys.path.insert(0, str(self._root))
        try:
            from langgraph.types import Command
        except ImportError as exc:
            raise BackendUnavailable("LangGraph isn't installed. Run: pip install -r requirements.txt") from exc
        module = _load_pipeline(self._root)
        missing = [name for name in ("app", "start_review", "submit_decisions") if not hasattr(module, name)]
        if missing:
            raise BackendUnavailable(
                f"phase2_agents/pipeline.py has no {', '.join(missing)}. Pull the latest main."
            )
        super().__init__(module.app, Command)
        self._pipeline = module
        self.retrieval_backend = getattr(module, "RETRIEVER_BACKEND", None)
        self._indexed: dict[str, list[dict]] | None = None

    @property
    def placeholder_retrieval(self) -> bool:
        return "placeholder" in (self.retrieval_backend or "").lower()

    # ------------------------------------------------------------ pipeline

    def run_section(self, chunk: dict, thread_id: str) -> dict:
        run = self._pipeline.start_review([chunk], run_id=thread_id)
        try:  # the mapper output isn't in start_review()'s result, but it's in the paused state
            values = self._app.get_state(self._config(thread_id)).values or {}
        except Exception:  # noqa: BLE001 - extra detail only; the findings are still usable
            values = {}
        state = {
            "mapped": values.get("mapped", []),
            "errors": run.get("errors", []),
            "retrieval_backend": run.get("backend", self.retrieval_backend),
        }
        if run.get("status") == "awaiting_human_review":
            state["__interrupt__"] = [{"message": "Awaiting human review", "findings": run.get("findings", [])}]
        return state

    def submit_review(self, thread_id: str, decisions: dict) -> dict:
        return self._pipeline.submit_decisions(thread_id, decisions)

    # ------------------------------------------------------------ policies

    def _indexed_chunks(self) -> dict[str, list[dict]]:
        if self._indexed is None:
            path = self._root / "data" / "processed" / "policy_chunks.json"
            grouped: dict[str, list[dict]] = {}
            if path.is_file():
                for record in json.loads(path.read_text(encoding="utf-8")):
                    grouped.setdefault(record["source"], []).append(record)
            self._indexed = grouped
        return self._indexed

    def list_policies(self) -> list[str]:
        return sorted(self._indexed_chunks())

    def load_policy(self, source: str) -> PolicyDocument:
        chunks = self._indexed_chunks().get(source)
        if not chunks:
            raise BackendUnavailable(f"{source} isn't in data/processed/policy_chunks.json.")
        return PolicyDocument(
            source=source, chunks=[dict(chunk) for chunk in chunks], origin="indexed",
            notes=["Loaded from Phase 1's processed policy chunks."],
        )

    def parse_upload(self, filename: str, data: bytes) -> PolicyDocument:
        name = check_upload(filename, data)
        pages = parse_pdf_pages(name, data)
        try:
            from phase1_ingestion.chunk_policies import chunk_pages
        except ImportError as exc:
            raise BackendUnavailable(
                "Phase 1 isn't on this branch yet (phase1_ingestion is missing), so uploads can't be split."
            ) from exc
        try:
            chunks, _sources = chunk_pages(pages)
        except ValueError as exc:
            raise UploadRejected(f"Phase 1's chunker couldn't split this PDF into sections: {exc}") from exc
        return PolicyDocument(
            source=name, chunks=chunks, origin="upload",
            notes=["Uploaded policies are analyzed directly but aren't added to the search index, "
                   "so the chat can't answer questions about them until Phase 1 re-indexes."],
        )

    # ------------------------------------------------------------ framework

    def framework_info(self) -> dict:
        info = {
            "title": "NIST SP 800-53 Revision 5",
            "version": None,
            "publication_date": None,
            "source": None,
            "simulated": False,
            "note": "Framework version not recorded yet. Run Phase 1's download_frameworks.py.",
        }
        candidates = (
            self._root / "chroma_db" / "index_manifest.json",
            self._root / "data" / "frameworks" / "framework_sources.json",
        )
        for path in candidates:
            if not path.is_file():
                continue
            try:
                content = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            record = content.get("sources", content).get(SP80053_FILE)
            if isinstance(record, dict):
                info.update(
                    title=record.get("title", info["title"]),
                    version=record.get("version"),
                    publication_date=record.get("publication_date"),
                    source=record.get("source_url"),
                    sha256=record.get("sha256"),
                    note="The index also holds NIST CSF 2.0, so the Mapper can return CSF outcomes "
                         "until Phase 2 filters by framework.",
                )
                return info
        return info

    # ------------------------------------------------------------ chat

    def _qa_function(self):
        """COPILOT_QA_MODULE if set; otherwise Anu's qa_agent once it's merged."""
        target = os.getenv("COPILOT_QA_MODULE", "").strip()
        if not target:
            if importlib.util.find_spec("qa_agent") is None:
                return None
            target = "qa_agent.agent:ask"
        module_name, _, function_name = target.partition(":")
        try:
            return getattr(importlib.import_module(module_name), function_name or "ask")
        except Exception as exc:  # noqa: BLE001 - surface as a clear chat message
            raise BackendUnavailable(f"The Q&A agent ({target}) couldn't be loaded ({_short(exc)}).") from exc

    def ask(self, question: str, findings: list[dict] | None = None, history: list[dict] | None = None) -> dict:
        qa = self._qa_function()
        if qa is not None:
            accepted = inspect.signature(qa).parameters
            extras = {name: value for name, value in (("findings", findings), ("history", history))
                      if name in accepted and value}
            return qa(question, **extras)
        answer = ("Retrieval only: the Q&A agent isn't on this branch yet, so these are the closest passages "
                  "from the index, without an AI-written answer.")
        if self.placeholder_retrieval:
            answer += " Phase 2 is using placeholder search, so these come from a few sample chunks."
        return {"answer": answer, "citations": self._pipeline.search(question, type=None, top_k=4)}
