"""
One place that decides how the agents talk to Claude.

    CLAUDE_BACKEND=anthropic   (default)  -> api.anthropic.com with ANTHROPIC_API_KEY
    CLAUDE_BACKEND=vertex                 -> Claude on Google Cloud Vertex AI, billed to
                                             the GCP project, no Anthropic key needed

Every agent does `from claude_client import client, MODEL` and calls
`client.messages.create(model=MODEL, ...)`. The call signature is identical
on both backends, so nothing else in the agents changes.

Vertex needs:
  - Vertex AI API enabled and the Claude model enabled in Model Garden for the
    project (done by the organizers for uc2-cyber-policy-compliance)
  - Google credentials: automatic in Cloud Shell / Cloud Run; on a laptop run
    `gcloud auth application-default login` once
  - ANTHROPIC_VERTEX_PROJECT_ID (or GOOGLE_CLOUD_PROJECT) and CLOUD_ML_REGION
"""

import os
from dotenv import load_dotenv

load_dotenv()

BACKEND = os.getenv("CLAUDE_BACKEND", "anthropic").strip().lower()

if BACKEND == "vertex":
    from anthropic import AnthropicVertex

    PROJECT_ID = os.getenv("ANTHROPIC_VERTEX_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
    REGION = os.getenv("CLOUD_ML_REGION", "global")
    if not PROJECT_ID:
        raise RuntimeError("CLAUDE_BACKEND=vertex but ANTHROPIC_VERTEX_PROJECT_ID is not set")
    # Vertex model ids carry a version suffix
    MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5@20250929")
    DESCRIPTION = f"vertex ({PROJECT_ID}, {REGION}, {MODEL})"

    def make_client():
        return AnthropicVertex(project_id=PROJECT_ID, region=REGION)
else:
    from anthropic import Anthropic

    MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
    DESCRIPTION = f"anthropic ({MODEL})"

    def make_client():
        return Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# Each agent calls make_client() to get its OWN client object. They must not
# share one: tests (and any future per-agent instrumentation) patch
# `<agent>.client.messages.create`, and a shared object would make one agent's
# stub answer for all of them.
client = make_client()   # used only by the smoke test below


if __name__ == "__main__":
    # Smoke test: one tiny call. Run this first after switching backends.
    print(f"Claude backend: {DESCRIPTION}")
    r = client.messages.create(model=MODEL, max_tokens=20,
                               messages=[{"role": "user", "content": "Reply with the single word OK."}])
    print("Response:", r.content[0].text)
