"""
Checks for the Gemini backend. Run these in Cloud Shell (they need Google credentials).

  python shared/check_gemini.py probe [model ...]
      Which Gemini models can this project call, in which region? Tries a list of
      model names (or the ones you give) with a tiny request each. The names in the
      default list are guesses: the real ids come from Model Garden in the console.

  python shared/check_gemini.py agents
      Runs Extractor -> Mapper -> Auditor on one sample sentence, on Gemini.
      Needs CLAUDE_BACKEND=gemini and GEMINI_MODEL set. Uses the placeholder
      search, so it needs no index. Makes 3 Gemini calls.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("ANTHROPIC_VERTEX_PROJECT_ID") or "uc2-cyber-policy-compliance"
DEFAULT_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash",
                  "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"]


def probe(models):
    from gemini_client import GeminiClient
    print(f"Project: {PROJECT}\n")
    for location in ("global", "us-central1"):
        client = GeminiClient(PROJECT, location, retries=0)
        for model in models:
            try:
                r = client.messages.create(model=model, max_tokens=20,
                                           messages=[{"role": "user", "content": "Reply with the single word OK."}])
                print(f"WORKS  {location:12} {model:26} -> {r.content[0].text.strip()[:20]!r}")
            except Exception as exc:                        # print, never crash the probe
                print(f"fail   {location:12} {model:26} {type(exc).__name__}: {str(exc)[:150]}")


def agents():
    if os.getenv("CLAUDE_BACKEND", "").lower() != "gemini" or not os.getenv("GEMINI_MODEL"):
        sys.exit("Set CLAUDE_BACKEND=gemini and GEMINI_MODEL=<a model that WORKS in the probe> first.")
    sys.modules["retriever"] = None                          # use the placeholder search: no index needed
    sys.path[:0] = [os.path.join(HERE, "..", "phase2_agents")]
    import claude_client, extractor, mapper, auditor
    print("Backend:", claude_client.DESCRIPTION, "\n")
    chunk = {"chunk_id": "policy-security-001", "source": "Computer_Security_Policy.pdf", "type": "internal",
             "page": 2, "locator": "Section 3.5 Authentication",
             "text": "Users must comply with current organizational authentication requirements, "
                     "including multifactor authentication where required."}
    reqs = extractor.extract_requirements(chunk)
    print(f"1. Extractor found {len(reqs)} requirement(s):", [r["requirement"] for r in reqs])
    mapped = mapper.map_requirement(reqs[0])
    print("2. Mapper picked:", mapped["mapped_control"], "-", (mapped["mapping_reasoning"] or "")[:100])
    finding = auditor.audit_requirement(mapped)
    print("3. Auditor judged:", finding["coverage"])
    print(json.dumps({k: finding[k] for k in ("finding", "recommendation", "plain_language", "citation")}, indent=2))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "probe":
        probe(sys.argv[2:] or DEFAULT_MODELS)
    elif mode == "agents":
        agents()
    else:
        sys.exit(__doc__)
