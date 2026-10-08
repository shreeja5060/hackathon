"""
Gemini on Vertex AI, behind the same call the agents already make to Claude:

    client.messages.create(model=..., max_tokens=..., messages=[...])  ->  response.content[0].text

so the Extractor, Mapper and Auditor run on Gemini without any change.

No extra package: this calls Vertex AI's generateContent web endpoint directly,
using the Google credentials google-auth finds (automatic in Cloud Shell and
Cloud Run; on a laptop run `gcloud auth application-default login` once).
google-auth ships with `anthropic[vertex]`, which requirements.txt already has.

What it does NOT do: Claude-style tool use. The Q&A chat agent needs that, so
it cannot run on this backend; asking for tools raises NotImplementedError
instead of failing in a confusing way.

Settings (all optional, read from the environment):
  GEMINI_TEMPERATURE      default 0.2   (lower = steadier JSON from the agents)
  GEMINI_TOKEN_FACTOR     default 8     output limit = max_tokens * factor ...
  GEMINI_TOKEN_FLOOR      default 1024  ... but never below this
  GEMINI_THINKING_BUDGET  unset         set (e.g. 0) only if the model supports it

Why the token factor: Gemini's "thinking" models count their hidden reasoning
against the output limit. The agents ask for small limits (20, 300, 1000) that
were sized for Claude; passed through unchanged, Gemini can spend the whole
budget thinking and return no text at all.
"""

import json
import os
import time
from types import SimpleNamespace

RETRY_STATUS = {429, 500, 502, 503, 504}
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class GeminiError(RuntimeError):
    """A Gemini call failed. The message says why, in words."""


class _Block:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _Response:
    """Looks like an Anthropic message for the parts our code reads."""

    def __init__(self, text: str, stop_reason: str, usage: dict):
        self.content = [_Block(text)]
        self.stop_reason = stop_reason
        self.usage = SimpleNamespace(
            input_tokens=usage.get("promptTokenCount", 0),
            output_tokens=usage.get("candidatesTokenCount", 0),
        )


def _text_of(content) -> str:
    """Anthropic message content is a string or a list of blocks; Gemini wants text."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type", "text") == "text":
            parts.append(block.get("text", ""))
        elif getattr(block, "type", "text") == "text":
            parts.append(getattr(block, "text", ""))
    return "\n".join(p for p in parts if p)


def _parse(data: dict) -> _Response:
    candidates = data.get("candidates") or []
    if not candidates:
        feedback = json.dumps(data.get("promptFeedback", {}))[:200]
        raise GeminiError(f"Gemini returned no answer (the prompt may have been blocked): {feedback}")
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    finish = candidate.get("finishReason")
    if not text.strip():
        raise GeminiError(
            f"Gemini returned no text (finishReason={finish}). If that is MAX_TOKENS, "
            "raise GEMINI_TOKEN_FACTOR: the model used its budget on reasoning.")
    stop = {"STOP": "end_turn", "MAX_TOKENS": "max_tokens"}.get(finish, str(finish).lower())
    return _Response(text, stop, data.get("usageMetadata", {}))


class _Messages:
    def __init__(self, owner: "GeminiClient"):
        self._owner = owner

    def create(self, *, model, max_tokens, messages, system=None, temperature=None, tools=None, **_ignored):
        if tools:
            raise NotImplementedError(
                "Tool use is not supported on the Gemini backend (the Q&A chat agent needs it). "
                "Use CLAUDE_BACKEND=anthropic or vertex for the chat.")
        owner = self._owner
        contents = [{"role": "model" if m["role"] == "assistant" else "user",
                     "parts": [{"text": _text_of(m["content"])}]} for m in messages]
        config = {"maxOutputTokens": max(int(max_tokens) * owner.token_factor, owner.token_floor)}
        temp = temperature if temperature is not None else owner.temperature
        if temp is not None:
            config["temperature"] = temp
        if owner.thinking_budget is not None:
            config["thinkingConfig"] = {"thinkingBudget": owner.thinking_budget}
        body = {"contents": contents, "generationConfig": config}
        if system:
            body["systemInstruction"] = {"parts": [{"text": _text_of(system)}]}
        return _parse(owner._generate(model, body))


class GeminiClient:
    """Drop-in for the part of the Anthropic client the agents use: `.messages.create(...)`."""

    def __init__(self, project_id: str, location: str = "global", timeout: float = 120, retries: int = 2):
        self.project_id = project_id
        self.location = location
        self.timeout = timeout
        self.retries = retries
        self.temperature = float(os.getenv("GEMINI_TEMPERATURE", "0.2"))
        self.token_factor = int(os.getenv("GEMINI_TOKEN_FACTOR", "8"))
        self.token_floor = int(os.getenv("GEMINI_TOKEN_FLOOR", "1024"))
        budget = os.getenv("GEMINI_THINKING_BUDGET")
        self.thinking_budget = int(budget) if budget not in (None, "") else None
        self._creds = None
        self.messages = _Messages(self)

    def _url(self, model: str) -> str:
        host = "aiplatform.googleapis.com" if self.location == "global" else f"{self.location}-aiplatform.googleapis.com"
        return (f"https://{host}/v1/projects/{self.project_id}/locations/{self.location}"
                f"/publishers/google/models/{model}:generateContent")

    def _headers(self) -> dict:
        # Imported here so that constructing a client needs no credentials or network.
        import google.auth
        from google.auth.transport.requests import Request
        if self._creds is None:
            self._creds, _ = google.auth.default(scopes=SCOPES)
        if not self._creds.valid:
            self._creds.refresh(Request())
        return {"Authorization": f"Bearer {self._creds.token}", "Content-Type": "application/json"}

    def _generate(self, model: str, body: dict) -> dict:
        import requests
        response = None
        for attempt in range(self.retries + 1):
            response = requests.post(self._url(model), json=body, headers=self._headers(), timeout=self.timeout)
            if response.status_code == 200:
                return response.json()
            if response.status_code in RETRY_STATUS and attempt < self.retries:
                time.sleep(2 ** (attempt + 1))
                continue
            break
        try:
            reason = response.json()["error"]["message"]
        except Exception:
            reason = (response.text or "")[:300]
        raise GeminiError(f"Gemini request failed ({response.status_code}) for model {model!r}: {reason}")
