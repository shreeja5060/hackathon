"""
Shared test setup for Phase 2.

These tests never call the Anthropic API. The agents build an Anthropic
client at import time and need *some* key to exist, so we set a dummy one
before anything is imported. Each test then replaces `client.messages.create`
with a fake that returns canned text, so we test OUR code (prompt building,
JSON parsing, citation handling, error handling) without spending credit.
"""
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

# These tests are written against the placeholder search (shared/fake_search.py),
# so force it, whatever is installed. Without this, a machine that has Phase 1's
# real index (Cloud Shell, a Linux box, Apple Silicon) makes the agents pick the
# real retriever and a number of tests fail because the data is different, even
# though the code is fine. Setting a module to None in sys.modules makes
# `from retriever import ...` raise ImportError, which the agents catch and
# fall back. This must run before any agent module is imported.
# Run these tests separately from phase3_dashboard/tests (as CI does), because
# that suite chooses its own backend.
sys.modules["retriever"] = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "phase2_agents"))
sys.path.insert(0, os.path.join(ROOT, "shared"))

import pytest


class _FakeContent:
    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeContent(text)]


@pytest.fixture
def fake_claude(monkeypatch):
    """
    Returns a function `use(module, text)` that makes module.client.messages.create
    return `text` as if Claude had said it. Also records every prompt sent.
    """
    sent = []

    def use(module, text):
        def _create(**kwargs):
            sent.append(kwargs)
            return _FakeResponse(text)
        monkeypatch.setattr(module.client.messages, "create", _create)

    use.sent = sent
    return use


@pytest.fixture
def sample_chunk():
    return {
        "chunk_id": "policy-security-001",
        "text": "Users must comply with current organizational authentication "
                "requirements, including multifactor authentication where required.",
        "source": "Public_Release_Computer_Security_Policy.pdf",
        "type": "internal",
        "page": 2,
        "locator": "Section 3.5 Authentication",
    }
