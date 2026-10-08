import json
import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE = REPO_ROOT / "phase2_agents" / "pipeline.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def make_pdf(pages: list[str]) -> bytes:
    """Build a small text PDF in memory (one string per page)."""
    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        page.insert_text((72, 72), text, fontsize=11)
    data = document.tobytes()
    document.close()
    return data


@pytest.fixture
def sim_backend():
    from phase3_dashboard.backends.simulated import SimulatedBackend

    return SimulatedBackend(delay=0)


@pytest.fixture
def sample_session(sim_backend):
    """A paused review of the first sample policy."""
    from phase3_dashboard.core import analysis

    document = sim_backend.load_policy(sim_backend.list_policies()[0])
    return analysis.analyze_policy(sim_backend, document, [c["chunk_id"] for c in document.chunks])


# ---------------------------------------------------------------- live mode: the real Phase 1 + 2 code
# Real agents and LangGraph pipeline; only Claude's replies and retrieval are faked.

HAS_REVIEW_API = PIPELINE.is_file() and "def start_review" in PIPELINE.read_text(encoding="utf-8")
needs_pipeline = pytest.mark.skipif(not HAS_REVIEW_API, reason="pipeline.py with start_review() isn't on this branch")
needs_chunker = pytest.mark.skipif(not (REPO_ROOT / "phase1_ingestion" / "chunk_policies.py").is_file(),
                                   reason="phase1_ingestion isn't on this branch")

CHUNKS = [
    {"chunk_id": "policy-1", "text": "3.3 Privileged Access\n\nPrivileged accounts should use multi-factor "
     "authentication where required. Passwords must be at least 14 characters long.",
     "source": "Computer Security Policy.pdf", "type": "internal", "doc_kind": "policy",
     "page": 2, "locator": "Section 3.3 Privileged Access"},
    {"chunk_id": "policy-2", "text": "3.5 Credential Sharing\n\nUsers must not share passwords. FAIL-ME",
     "source": "Computer Security Policy.pdf", "type": "internal", "doc_kind": "policy",
     "page": 2, "locator": "Section 3.5 Credential Sharing"},
]


# ------------------------------------------------------------------ fake Claude

class _Reply:
    def __init__(self, text):
        self.content = [types.SimpleNamespace(text=text)]


def _extractor_reply(prompt):
    if "FAIL-ME" in prompt:
        return "Sure! Here are the requirements:"  # not JSON: extractor.py's json.loads fails
    return json.dumps([
        {"requirement": "MFA for privileged accounts",
         "requirement_text": "Privileged accounts should use multi-factor authentication where required."},
        {"requirement": "Minimum password length",
         "requirement_text": "Passwords must be at least 14 characters long."},
    ])


def _mapper_reply(prompt):
    if "multi-factor" in prompt:
        return json.dumps({"control_id": "IA-2", "reasoning": "IA-2 covers authentication of users."})
    return json.dumps({"control_id": None, "reasoning": "None of the candidates fit."})


def _auditor_reply(prompt):
    return json.dumps({
        "coverage": "Partial",
        "finding": "MFA is required 'where required', but the scope is never defined.",
        "recommendation": "Define which systems and user roles require MFA.",
        "plain_language": "The rule says MFA is needed but not where.",
        "clarifying_questions": ["Which systems count as privileged?"],
    })


@pytest.fixture
def live(monkeypatch):
    for package in ("langgraph", "anthropic", "dotenv"):
        pytest.importorskip(package)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")  # the agents build a client at import
    monkeypatch.delenv("COPILOT_QA_MODULE", raising=False)

    from phase3_dashboard.backends.live import LiveBackend

    backend = LiveBackend()
    agents = {name: sys.modules[name] for name in ("extractor", "mapper", "auditor")}
    fake_search = sys.modules.get("fake_search") or __import__("fake_search")
    sent = []

    def answer_with(reply_for):
        def create(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            sent.append(prompt)
            return _Reply(reply_for(prompt))
        return create

    for name, reply_for in (("extractor", _extractor_reply), ("mapper", _mapper_reply), ("auditor", _auditor_reply)):
        monkeypatch.setattr(agents[name].client.messages, "create", answer_with(reply_for))
    # Same retrieval for everyone: the placeholder pipeline.py falls back to.
    monkeypatch.setattr(agents["mapper"], "search", fake_search.search)
    monkeypatch.setattr(agents["auditor"], "get_original_chunk", fake_search.get_original_chunk)
    monkeypatch.setattr(backend._pipeline, "search", fake_search.search)
    backend.sent_prompts = sent
    return backend
