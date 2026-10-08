"""
Phase 2: The Extractor Agent.

Takes policy text and pulls out discrete, individually-checkable requirements
as structured JSON, each with a citation back to its source chunk.

This is the same pattern as day1_first_call.py: load key, create client,
send a message, read the reply. The only two things that changed:
  1. The prompt is now specific and asks for JSON.
  2. We parse that JSON so other code (the Mapper, the dashboard) can use it.
"""

import os
import sys
import json
from dotenv import load_dotenv

# Import our shared placeholder search function (Maryam's real one will
# replace this later - same function name, so nothing else has to change).

# Retrieval backend: use Maryam's real retriever (phase1_ingestion) when its
# dependencies are installed (Linux / Apple Silicon / Cloud Shell / Docker);
# fall back to the placeholder fake_search on machines that can't run it
# (e.g. Intel Macs). Same search() / get_original_chunk() interface either way.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(_HERE)
sys.path.append(os.path.join(_HERE, "..", "shared"))
sys.path.append(os.path.join(_HERE, "..", "phase1_ingestion"))
try:
    from retriever import search, get_original_chunk
    RETRIEVER_BACKEND = "real (phase1_ingestion/retriever.py)"
except Exception:
    from fake_search import search, get_original_chunk
    RETRIEVER_BACKEND = "placeholder (shared/fake_search.py)"

load_dotenv()
from claude_client import make_client, MODEL
client = make_client()


EXTRACTOR_PROMPT = """You are a compliance analyst. Read the policy text below \
and extract every discrete, individually-checkable security requirement it \
contains. Ignore purpose statements, definitions, and general commentary - \
only extract concrete "must" / "must not" / "should" style requirements.

Return ONLY a JSON array, no other text, in this exact shape:
[
  {{"requirement": "short name", "requirement_text": "the exact sentence or clause from the policy"}}
]

Policy text:
\"\"\"
{policy_text}
\"\"\"
"""


def _normalize_requirements(parsed) -> list[dict]:
    """
    Claude is asked for a JSON list of {"requirement", "requirement_text"}
    objects, but it does not always comply exactly. Accept the sensible
    variants, and raise a clear error (instead of a cryptic TypeError later)
    for anything else, so the pipeline logs it and moves on.

      [{"requirement": ..., "requirement_text": ...}]   expected
      {"requirements": [ ... ]}                          list wrapped under "requirements"
      ["plain sentence", ...]                            bare strings
      {} / []                                            nothing to extract
    """
    if isinstance(parsed, dict):
        if not parsed:
            return []
        # Only unwrap the one wrapper we expect. Any other object is NOT a
        # requirements reply (e.g. another agent's reply), so fail loudly
        # instead of guessing and producing nonsense requirements.
        if isinstance(parsed.get("requirements"), list):
            parsed = parsed["requirements"]
        else:
            raise ValueError(f"Extractor reply was an object without a 'requirements' list: keys={list(parsed)[:5]}")

    if not isinstance(parsed, list):
        raise ValueError(f"Extractor reply was {type(parsed).__name__}, expected a list")

    out = []
    for item in parsed:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"requirement": text[:80], "requirement_text": text})
        elif isinstance(item, dict):
            text = item.get("requirement_text") or item.get("text") or item.get("requirement")
            if not text:
                raise ValueError(f"Extractor item has no requirement text: keys={list(item)[:5]}")
            out.append({**item,
                        "requirement": item.get("requirement") or str(text)[:80],
                        "requirement_text": text})
        else:
            raise ValueError(f"Extractor item was {type(item).__name__}, expected object or string")
    return out


def extract_requirements(chunk: dict) -> list[dict]:
    """
    Takes one chunk (from search(), matching the shared schema) and returns
    a list of extracted requirements, each tagged with the chunk's citation
    info so we never lose track of where a requirement came from.
    """
    prompt = EXTRACTOR_PROMPT.format(policy_text=chunk["text"])

    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw_text = response.content[0].text.strip()

    # Claude sometimes wraps JSON in ```json fences - strip those if present
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        raw_text = raw_text.replace("json\n", "", 1)

    requirements = _normalize_requirements(json.loads(raw_text))

    # Attach the citation so downstream agents (Mapper, Auditor) and the
    # dashboard can always show "where did this come from"
    for req in requirements:
        req["source"] = chunk["source"]
        req["chunk_id"] = chunk["chunk_id"]
        req["locator"] = chunk.get("locator")

    return requirements


if __name__ == "__main__":
    # Grab a couple of policy chunks from our placeholder search function
    policy_chunks = search("authentication accounts", type="policy")

    all_requirements = []
    for chunk in policy_chunks:
        print(f"\nExtracting from: {chunk['source']} ({chunk['locator']})")
        reqs = extract_requirements(chunk)
        all_requirements.extend(reqs)
        for r in reqs:
            print(f"  - {r['requirement']}: {r['requirement_text']}")

    print(f"\nTotal requirements extracted: {len(all_requirements)}")

    # Save the output so the Mapper (Day 2) can pick it up
    with open("extracted_requirements.json", "w") as f:
        json.dump(all_requirements, f, indent=2)
    print("Saved to extracted_requirements.json")
