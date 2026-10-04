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
from anthropic import Anthropic

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
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


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


def extract_requirements(chunk: dict) -> list[dict]:
    """
    Takes one chunk (from search(), matching the shared schema) and returns
    a list of extracted requirements, each tagged with the chunk's citation
    info so we never lose track of where a requirement came from.
    """
    prompt = EXTRACTOR_PROMPT.format(policy_text=chunk["text"])

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw_text = response.content[0].text.strip()

    # Claude sometimes wraps JSON in ```json fences - strip those if present
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        raw_text = raw_text.replace("json\n", "", 1)

    requirements = json.loads(raw_text)

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
