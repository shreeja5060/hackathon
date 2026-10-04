"""Live backend: connects the dashboard to the real Phase 1 and Phase 2 code.

It works once phase1-maryam and phase2-shreeja are merged into your branch.
It doesn't change their code; it only calls it:

* phase2_agents/pipeline.py ........... `app`, the compiled LangGraph. invoke() runs a
                                         section and pauses; Command(resume=...) resumes it
* phase1_ingestion/chunk_policies.py .. chunk_pages(), to split uploaded PDFs
* phase1_ingestion/retriever.py ....... search(), for the chat while the Q&A agent is missing
* data/processed/policy_chunks.json ... the policies Phase 1 already indexed
* chroma_db/index_manifest.json ....... the framework version, for the sidebar and report

To plug in the Q&A agent, set COPILOT_QA_MODULE="package.module:function".
The function must take a question and return {"answer": str, "citations": [search() results]}.
"""

from __future__ import annotations

import importlib
import importlib.util
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
        super().__init__(module.app, Command)
        self._indexed: dict[str, list[dict]] | None = None

    # ------------------------------------------------------------ pipeline

    def run_section(self, chunk: dict, thread_id: str) -> dict:
        state = super().run_section(chunk, thread_id)
        if isinstance(state, dict) and "__interrupt__" not in state:
            # Some LangGraph versions don't include the pause in invoke()'s
            # result. Read it from the saved checkpoint instead.
            snapshot = self._app.get_state(self._config(thread_id))
            pauses = [p for task in getattr(snapshot, "tasks", ()) for p in getattr(task, "interrupts", ())]
            if pauses:
                state = {**state, "__interrupt__": pauses}
        return state

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
        target = os.getenv("COPILOT_QA_MODULE", "").strip()
        if not target:
            return None
        module_name, _, function_name = target.partition(":")
        try:
            return getattr(importlib.import_module(module_name), function_name or "ask")
        except (ImportError, AttributeError) as exc:
            raise BackendUnavailable(f"COPILOT_QA_MODULE={target!r} couldn't be loaded ({_short(exc)}).") from exc

    def ask(self, question: str) -> dict:
        qa = self._qa_function()
        if qa is not None:
            return qa(question)
        try:
            from phase1_ingestion.retriever import search
        except ImportError as exc:
            raise BackendUnavailable("Neither the Q&A agent nor Phase 1's retriever is on this branch.") from exc
        return {
            "answer": "Retrieval only: the Q&A agent isn't connected yet, so these are the closest passages "
                      "from the index, without an AI-written answer.",
            "citations": search(question, type=None, top_k=4),
        }
