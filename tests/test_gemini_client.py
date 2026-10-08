"""
Tests for the Gemini backend (shared/gemini_client.py). No network, no credentials:
the single method that talks to Google (_generate) is replaced with a fake.
What these prove is that our request is shaped correctly, replies are read
correctly, failures are explained, and the agents can run on top of it. They
cannot prove Google accepts the request; the Cloud Shell probe does that.
"""
import importlib
import json
import pytest

import gemini_client as gc
from gemini_client import GeminiClient, GeminiError

import extractor


def _reply(text="OK", finish="STOP", parts=None):
    return {"candidates": [{"content": {"parts": parts or [{"text": text}]}, "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 3}}


@pytest.fixture
def gem(monkeypatch):
    """A client whose Google call is faked; records what it was asked."""
    for var in ("GEMINI_TEMPERATURE", "GEMINI_TOKEN_FACTOR", "GEMINI_TOKEN_FLOOR", "GEMINI_THINKING_BUDGET"):
        monkeypatch.delenv(var, raising=False)
    client = GeminiClient("proj", "global")
    client.calls = []
    client.next = _reply()

    def fake_generate(model, body):
        client.calls.append((model, body))
        if isinstance(client.next, Exception):
            raise client.next
        return client.next
    monkeypatch.setattr(client, "_generate", fake_generate)
    return client


def test_request_is_shaped_the_way_vertex_expects(gem):
    gem.messages.create(model="gem-x", max_tokens=300, system="be brief",
                        messages=[{"role": "user", "content": "hello"}])
    model, body = gem.calls[0]
    assert model == "gem-x"
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
    assert body["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    assert body["generationConfig"]["temperature"] == 0.2


def test_output_limit_leaves_room_for_the_models_reasoning(gem):
    gem.messages.create(model="m", max_tokens=300, messages=[{"role": "user", "content": "x"}])
    assert gem.calls[-1][1]["generationConfig"]["maxOutputTokens"] == 2400      # 300 * 8
    gem.messages.create(model="m", max_tokens=20, messages=[{"role": "user", "content": "x"}])
    assert gem.calls[-1][1]["generationConfig"]["maxOutputTokens"] == 1024      # floor


def test_assistant_turns_become_model_turns_and_block_lists_are_flattened(gem):
    gem.messages.create(model="m", max_tokens=10, messages=[
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
        {"role": "assistant", "content": "c"}])
    contents = gem.calls[0][1]["contents"]
    assert [c["role"] for c in contents] == ["user", "model"]
    assert contents[0]["parts"][0]["text"] == "a\nb"


def test_reply_looks_like_an_anthropic_message(gem):
    r = gem.messages.create(model="m", max_tokens=10, messages=[{"role": "user", "content": "x"}])
    assert r.content[0].text == "OK"
    assert r.stop_reason == "end_turn"
    assert (r.usage.input_tokens, r.usage.output_tokens) == (11, 3)


def test_hidden_reasoning_parts_are_not_returned(gem):
    gem.next = _reply(parts=[{"text": "private reasoning", "thought": True}, {"text": "the answer"}])
    r = gem.messages.create(model="m", max_tokens=10, messages=[{"role": "user", "content": "x"}])
    assert r.content[0].text == "the answer"


def test_empty_reply_is_an_explained_error_not_an_empty_string(gem):
    gem.next = {"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}
    with pytest.raises(GeminiError, match="MAX_TOKENS"):
        gem.messages.create(model="m", max_tokens=10, messages=[{"role": "user", "content": "x"}])


def test_blocked_prompt_is_an_explained_error(gem):
    gem.next = {"promptFeedback": {"blockReason": "SAFETY"}}
    with pytest.raises(GeminiError, match="blocked"):
        gem.messages.create(model="m", max_tokens=10, messages=[{"role": "user", "content": "x"}])


def test_tool_use_is_refused_clearly(gem):
    with pytest.raises(NotImplementedError, match="Tool use"):
        gem.messages.create(model="m", max_tokens=10, tools=[{"name": "t"}],
                            messages=[{"role": "user", "content": "x"}])


def test_thinking_budget_is_only_sent_when_asked_for(gem, monkeypatch):
    gem.messages.create(model="m", max_tokens=10, messages=[{"role": "user", "content": "x"}])
    assert "thinkingConfig" not in gem.calls[-1][1]["generationConfig"]
    monkeypatch.setenv("GEMINI_THINKING_BUDGET", "0")
    GeminiClient("p").messages  # constructed fine
    assert GeminiClient("p").thinking_budget == 0


def test_global_and_regional_urls():
    g = GeminiClient("proj", "global")._url("gem-x")
    assert g == ("https://aiplatform.googleapis.com/v1/projects/proj/locations/global"
                 "/publishers/google/models/gem-x:generateContent")
    r = GeminiClient("proj", "us-central1")._url("gem-x")
    assert r.startswith("https://us-central1-aiplatform.googleapis.com/v1/projects/proj/locations/us-central1/")


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.text = status, payload, json.dumps(payload)

    def json(self):
        return self._payload


def test_a_busy_response_is_retried_then_succeeds(monkeypatch):
    requests = pytest.importorskip("requests")
    sent = []
    answers = iter([_Resp(429, {"error": {"message": "slow down"}}), _Resp(200, _reply("done"))])
    monkeypatch.setattr(requests, "post", lambda *a, **k: sent.append(1) or next(answers))
    monkeypatch.setattr(gc.time, "sleep", lambda s: None)
    client = GeminiClient("proj")
    monkeypatch.setattr(client, "_headers", lambda: {})
    r = client.messages.create(model="m", max_tokens=10, messages=[{"role": "user", "content": "x"}])
    assert r.content[0].text == "done" and len(sent) == 2


def test_failure_message_carries_googles_explanation(monkeypatch):
    requests = pytest.importorskip("requests")
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(404, {"error": {"message": "model not found"}}))
    client = GeminiClient("proj")
    monkeypatch.setattr(client, "_headers", lambda: {})
    with pytest.raises(GeminiError, match=r"404.*gem-x.*model not found"):
        client.messages.create(model="gem-x", max_tokens=10, messages=[{"role": "user", "content": "x"}])


def test_the_extractor_runs_on_top_of_it(gem, monkeypatch, sample_chunk):
    gem.next = _reply('```json\n[{"requirement": "MFA", "requirement_text": "use MFA where required"}]\n```')
    monkeypatch.setattr(extractor, "client", gem)
    reqs = extractor.extract_requirements(sample_chunk)
    assert reqs[0]["requirement"] == "MFA"
    assert reqs[0]["source"] == sample_chunk["source"]          # citation still attached


def test_backend_is_chosen_by_environment_and_needs_a_model(monkeypatch):
    import claude_client
    try:
        monkeypatch.setenv("CLAUDE_BACKEND", "gemini")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
        monkeypatch.setenv("GEMINI_MODEL", "gem-x")
        importlib.reload(claude_client)
        assert claude_client.BACKEND == "gemini" and claude_client.MODEL == "gem-x"
        assert isinstance(claude_client.make_client(), GeminiClient)
        monkeypatch.delenv("GEMINI_MODEL")
        with pytest.raises(RuntimeError, match="GEMINI_MODEL"):
            importlib.reload(claude_client)
    finally:
        monkeypatch.undo()
        importlib.reload(claude_client)
