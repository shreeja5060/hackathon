"""The live backend against the real LangGraph runtime and Shreeja's pipeline.py.

The three agents are swapped for stand-ins that return the same shapes as
extractor.py, mapper.py and auditor.py, so no Claude calls are made and no API
key is needed. These tests run once phase2_agents/pipeline.py (and, for the
upload test, phase1_ingestion/chunk_policies.py) are on this branch; before
the merge they're skipped.
"""

import json
import shutil
import sys
import textwrap
import types
from pathlib import Path

import pytest

from phase3_dashboard.backends.base import BackendUnavailable
from phase3_dashboard.backends.live import LiveBackend
from phase3_dashboard.core import analysis, review
from phase3_dashboard.core.contracts import normalize_chat_answer
from phase3_dashboard.core.security import UploadRejected

from .conftest import make_pdf

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE = REPO_ROOT / "phase2_agents" / "pipeline.py"
CHUNKER = REPO_ROOT / "phase1_ingestion" / "chunk_policies.py"
needs_pipeline = pytest.mark.skipif(not PIPELINE.is_file(), reason="phase2_agents/pipeline.py isn't merged yet")
needs_chunker = pytest.mark.skipif(not CHUNKER.is_file(), reason="phase1_ingestion isn't merged yet")
AGENT_MODULES = ("extractor", "mapper", "auditor", "fake_search", "phase1_ingestion",
                 "phase1_ingestion.chunk_policies", "phase1_ingestion.retriever")

# Stand-ins with the same inputs and outputs as the real agents.
STUB_AGENTS = {
    "extractor.py": '''
        def extract_requirements(chunk):
            body = chunk["text"].partition("\\n\\n")[2] or chunk["text"]
            requirements = [{"requirement": "Stub requirement", "requirement_text": body.split(".")[0] + "."}]
            for requirement in requirements:
                requirement["source"] = chunk["source"]
                requirement["chunk_id"] = chunk["chunk_id"]
                requirement["locator"] = chunk.get("locator")
            return requirements
    ''',
    "mapper.py": '''
        def map_requirement(requirement):
            requirement["mapped_control"] = "IA-5"
            requirement["mapping_reasoning"] = "Stand-in mapping."
            return requirement
    ''',
    "auditor.py": '''
        def audit_requirement(mapped_requirement):
            return {
                "requirement": mapped_requirement["requirement"],
                "coverage": "Partial",
                "finding": "Stand-in finding.",
                "recommendation": "Stand-in recommendation.",
                "citation": {
                    "chunk_id": mapped_requirement["chunk_id"],
                    "source": mapped_requirement["source"],
                    "locator": mapped_requirement["locator"],
                },
                "framework_control": mapped_requirement["mapped_control"],
                "status": "pending",
            }
    ''',
}

POLICY_CHUNKS = [
    {"chunk_id": "policy-1", "text": "3.4 Passwords\n\nPasswords must be at least 14 characters long.",
     "source": "Computer Security Policy.pdf", "type": "internal", "doc_kind": "policy",
     "page": 2, "locator": "Section 3.4 Passwords"},
    {"chunk_id": "policy-2", "text": "3.5 Credential Sharing\n\nUsers must not share passwords with anyone.",
     "source": "Computer Security Policy.pdf", "type": "internal", "doc_kind": "policy",
     "page": 2, "locator": "Section 3.5 Credential Sharing"},
]
MANIFEST = {"sources": {"NIST_SP-800-53_rev5_catalog.json": {
    "title": "NIST SP 800-53 Revision 5, Release 5.2.0", "version": "5.2.0",
    "publication_date": "2025-08-27", "source_url": "https://example.test/catalog.json", "sha256": "abc",
}}}


@pytest.fixture
def live_repo(tmp_path, monkeypatch):
    """A throwaway repo: the real pipeline.py, stand-in agents, and Phase 1 output files."""
    pytest.importorskip("langgraph")
    pytest.importorskip("dotenv")
    agents = tmp_path / "phase2_agents"
    agents.mkdir()
    shutil.copy(PIPELINE, agents / "pipeline.py")
    for name, code in STUB_AGENTS.items():
        (agents / name).write_text(textwrap.dedent(code))
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "fake_search.py").write_text("def search(query, type=None, top_k=5):\n    return []\n")
    # A small phase1_ingestion package. The stub retriever keeps this test fast
    # even after pipeline.py switches from fake_search to the real retriever.
    phase1 = tmp_path / "phase1_ingestion"
    phase1.mkdir()
    (phase1 / "__init__.py").write_text("")
    (phase1 / "retriever.py").write_text(
        "def search(query, type=None, top_k=5):\n    return []\n\n"
        "def get_original_chunk(chunk_id):\n    raise KeyError(chunk_id)\n"
    )
    if CHUNKER.is_file():
        shutil.copy(CHUNKER, phase1 / "chunk_policies.py")
    processed = tmp_path / "data" / "processed"
    processed.mkdir(parents=True)
    (processed / "policy_chunks.json").write_text(json.dumps(POLICY_CHUNKS))
    (tmp_path / "chroma_db").mkdir()
    (tmp_path / "chroma_db" / "index_manifest.json").write_text(json.dumps(MANIFEST))

    monkeypatch.setattr(sys, "path", list(sys.path))  # pipeline.py appends to sys.path
    for name in AGENT_MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-used-by-the-stand-ins")
    monkeypatch.delenv("COPILOT_QA_MODULE", raising=False)
    yield tmp_path
    for name in AGENT_MODULES:
        sys.modules.pop(name, None)


@needs_pipeline
def test_review_round_trip_through_real_langgraph(live_repo):
    backend = LiveBackend(repo_root=live_repo)
    document = backend.load_policy("Computer Security Policy.pdf")
    session = analysis.analyze_policy(backend, document, [c["chunk_id"] for c in document.chunks])

    assert session.run.section_errors == []
    assert len(session.findings) == 2
    first, second = session.findings
    assert first.requirement_text == "Passwords must be at least 14 characters long."  # recovered from mapper output
    assert first.citation.page == 2 and first.mapping_reasoning == "Stand-in mapping."
    assert first.problems == [] and first.flags == []

    review.approve(session, first.finding_id, "Mahsa", edited_recommendation="Require 14+ characters.")
    review.reject(session, second.finding_id, "Mahsa", "Covered by another section")
    assert analysis.finalize_review(backend, session, "Mahsa") == []
    assert session.finalized

    # LangGraph itself now holds only the approved finding for that thread.
    state = backend._app.get_state(backend._config(first.thread_id)).values
    [approved] = state["approved_findings"]
    assert approved["recommendation"] == "Require 14+ characters."
    assert approved["recommendation_ai_original"] == "Stand-in recommendation."
    assert approved["reviewer"] == "Mahsa" and approved["status"] == "approved"
    assert backend._app.get_state(backend._config(second.thread_id)).values["approved_findings"] == []


@needs_pipeline
def test_framework_version_comes_from_the_index_manifest(live_repo):
    info = LiveBackend(repo_root=live_repo).framework_info()
    assert info["version"] == "5.2.0" and info["publication_date"] == "2025-08-27"
    assert info["simulated"] is False


@needs_pipeline
@needs_chunker
def test_uploads_are_split_by_phase1s_chunker(live_repo):
    backend = LiveBackend(repo_root=live_repo)
    pdf = make_pdf(["1 Purpose\nThis policy protects data.\n2 Passwords\nPasswords must be long."])
    document = backend.parse_upload("Uploaded Policy.pdf", pdf)
    assert [c["locator"] for c in document.chunks] == ["Section 1 Purpose", "Section 2 Passwords"]
    assert all(c["type"] == "internal" and c["doc_kind"] == "policy" for c in document.chunks)
    with pytest.raises(UploadRejected, match="chunker"):
        backend.parse_upload("No Headings.pdf", make_pdf(["Just a paragraph without numbered sections."]))


@needs_pipeline
def test_chat_uses_the_qa_agent_or_falls_back_to_search(live_repo, monkeypatch):
    hit = {"chunk_id": "policy-2", "text": "Users must not share passwords.", "source": "Computer Security Policy.pdf",
           "page": 2, "type": "internal", "doc_kind": "policy", "locator": "Section 3.5", "score": 0.62}
    retriever = types.ModuleType("phase1_ingestion.retriever")
    retriever.search = lambda query, type=None, top_k=5: [hit]
    monkeypatch.setitem(sys.modules, "phase1_ingestion.retriever", retriever)
    backend = LiveBackend(repo_root=live_repo)

    fallback = normalize_chat_answer(backend.ask("Can staff share passwords?"))
    assert fallback.answer.startswith("Retrieval only") and fallback.citations[0]["locator"] == "Section 3.5"

    qa = types.ModuleType("team_qa_agent")
    qa.ask = lambda question: {"answer": f"Answer to: {question}", "citations": [hit]}
    monkeypatch.setitem(sys.modules, "team_qa_agent", qa)
    monkeypatch.setenv("COPILOT_QA_MODULE", "team_qa_agent:ask")
    agent = normalize_chat_answer(backend.ask("Can staff share passwords?"))
    assert agent.answer == "Answer to: Can staff share passwords?" and len(agent.citations) == 1


def test_missing_phase2_code_gives_a_clear_message(tmp_path):
    with pytest.raises(BackendUnavailable) as error:
        LiveBackend(repo_root=tmp_path)
    assert "phase2" in str(error.value).lower() or "langgraph" in str(error.value).lower()
