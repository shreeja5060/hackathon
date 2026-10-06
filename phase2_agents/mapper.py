"""
Phase 2: The Mapper Agent.

Takes requirements the Extractor found (Day 1) and matches each one to the
NIST control it relates to. Same call pattern as before - the new skill
today is FEW-SHOT PROMPTING: showing Claude one worked example inside the
prompt so its answers stay consistent, instead of just describing the task
in words and hoping.
"""

import os
import sys
import json
from dotenv import load_dotenv


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
from claude_client import client, MODEL


# Notice the "Example" block below - this is the few-shot technique.
# Instead of just TELLING Claude the output format, we SHOW it one, worked,
# correct example. This dramatically improves consistency across many calls.
MAPPER_PROMPT = """You are a compliance analyst. You are given ONE extracted \
policy requirement and a list of candidate NIST controls. Pick the single \
best-matching control. If none genuinely match, say so.

Example:
Requirement: "Passwords must not be shared with any other person."
Candidate controls:
  - IA-2: Identification and Authentication (multifactor authentication for privileged accounts)
  - AC-2: Account Management (establishing, activating, reviewing accounts)
Correct answer:
{{"control_id": "IA-2", "reasoning": "Password sharing is an authentication integrity issue, which IA-2 governs."}}

Now do the same for this requirement:

Requirement: "{requirement_text}"

Candidate controls:
{candidate_controls}

Return ONLY JSON in this exact shape:
{{"control_id": "the best match, or null if none fit", "reasoning": "one sentence why"}}
"""


def format_candidates(chunks: list[dict]) -> str:
    lines = []
    for c in chunks:
        lines.append(f"  - {c['locator']}: {c['text']}")
    return "\n".join(lines)


def map_requirement(requirement: dict) -> dict:
    """
    Takes one extracted requirement (from extractor.py) and finds the best
    matching NIST control using the framework chunks from search().
    """
    # Pull candidate NIST controls from our (placeholder) search function
    candidates = search(requirement["requirement_text"], type="framework", top_k=15)
    candidates_text = format_candidates(candidates)
    # Build a lookup so we can find the exact chunk_id for whichever
    # control_id Claude picks - this is what lets the Auditor fetch the
    # EXACT chunk later instead of searching again.
    control_id_to_chunk_id = {c["locator"]: c["chunk_id"] for c in candidates}

    prompt = MAPPER_PROMPT.format(
        requirement_text=requirement["requirement_text"],
        candidate_controls=candidates_text
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )

    raw_text = response.content[0].text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`").replace("json\n", "", 1)

    result = json.loads(raw_text)

    # Attach the mapping result onto the original requirement, so nothing
    # already known (source, chunk_id, locator) gets lost as data flows
    # through the pipeline
    requirement["mapped_control"] = result["control_id"]
    requirement["mapped_control_chunk_id"] = control_id_to_chunk_id.get(result["control_id"])
    requirement["mapping_reasoning"] = result["reasoning"]
    return requirement


if __name__ == "__main__":
    # Load Day 1's output
    with open("extracted_requirements.json") as f:
        requirements = json.load(f)

    mapped = []
    for req in requirements:
        print(f"\nMapping: {req['requirement']}")
        result = map_requirement(req)
        mapped.append(result)
        print(f"  -> {result['mapped_control']}: {result['mapping_reasoning']}")

    with open("mapped_requirements.json", "w") as f:
        json.dump(mapped, f, indent=2)
    print("\nSaved to mapped_requirements.json")
