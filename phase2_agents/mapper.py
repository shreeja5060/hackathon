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
from claude_client import make_client, MODEL
client = make_client()


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


# Which framework the Mapper maps to. A substring of the catalog's source file
# name: "800-53" matches NIST_SP-800-53_rev5_catalog.json and excludes the
# CSF 2.0 catalog, which lives in the same index (type="framework") but whose
# outcomes (ID.RA-07, DE.AE-06, ...) are not 800-53 controls.
FRAMEWORK = os.getenv("MAPPER_FRAMEWORK", "800-53")
SHORTLIST_SIZE = 15      # candidate controls shown to Claude
FETCH_SIZE = 60          # fetch extra, because other frameworks and repeats are dropped


def format_candidates(chunks: list[dict]) -> str:
    lines = []
    for c in chunks:
        lines.append(f"  - {c['locator']}: {c['text']}")
    return "\n".join(lines)


def shortlist(requirement_text: str) -> list[dict]:
    """
    The candidate controls Claude chooses from: the best matches that belong to
    the chosen framework, one per control. The index stores a control as
    several fragments, so the same control (e.g. SI-3) can appear more than
    once in the raw results; repeats are dropped so they don't use up slots.
    """
    hits = search(requirement_text, type="framework", top_k=FETCH_SIZE)
    seen, out = set(), []
    for h in hits:
        locator = h.get("locator")
        if not locator or locator in seen:
            continue
        if FRAMEWORK not in (h.get("source") or ""):
            continue
        seen.add(locator)
        out.append(h)
        if len(out) == SHORTLIST_SIZE:
            break
    return out


def _lookup_control(control_id: str):
    """
    Exact lookup of one control by ID, for when Claude picks a control that was
    not on the shortlist. Returns {"chunk_id": ...} or None.

    Reads the index metadata directly (exact match). Searching by meaning is
    the wrong tool for this: it ranks by similarity, so a real control can miss
    the top results for its own ID. Any failure returns None, which the caller
    treats as "not verifiable" rather than guessing.
    """
    try:
        import retriever
        idx = retriever._index()
        col = idx if hasattr(idx, "query") else next(x for x in idx if hasattr(x, "query"))
        got = col.get(where={"$and": [{"type": "framework"}, {"locator": control_id}]},
                      include=["metadatas"])
        for chunk_id, meta in zip(got["ids"], got["metadatas"]):
            if FRAMEWORK in (meta.get("source") or "") and get_original_chunk(chunk_id):
                return {"chunk_id": chunk_id}
    except Exception:
        pass
    # Placeholder backend (or the index call failed): an exact match among search results.
    try:
        for h in search(control_id, type="framework", top_k=FETCH_SIZE):
            if h.get("locator") == control_id and FRAMEWORK in (h.get("source") or ""):
                return {"chunk_id": h["chunk_id"]}
    except Exception:
        pass
    return None


def map_requirement(requirement: dict) -> dict:
    """
    Takes one extracted requirement (from extractor.py) and finds the best
    matching control using the framework chunks from search().

    mapped_control is only ever set to a control that was verified to exist:
    either one from the shortlist, or one found by exact lookup in the index.
    If Claude names anything else, the requirement is left unmapped (and the
    Auditor reports it as "Not observable") rather than judged against a
    control whose text we do not have.
    """
    candidates = shortlist(requirement["requirement_text"])
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
    if not isinstance(result, dict) or "control_id" not in result:
        raise ValueError("Mapper reply has no control_id (expected {\"control_id\": ..., \"reasoning\": ...})")

    picked = result["control_id"]
    reasoning = result.get("reasoning") or ""
    chunk_id = control_id_to_chunk_id.get(picked) if picked else None

    if picked and chunk_id is None:
        # Claude chose a control that was not on the shortlist.
        found = _lookup_control(picked)
        if found:
            chunk_id = found["chunk_id"]
            reasoning = f"{reasoning} (Not on the shortlist; confirmed in the index by exact lookup.)".strip()
        else:
            reasoning = (f"{reasoning} (Claude suggested {picked}, but it is not a verifiable "
                         f"{FRAMEWORK} control in the index, so no mapping was made.)").strip()
            picked = None

    # Attach the mapping result onto the original requirement, so nothing
    # already known (source, chunk_id, locator) gets lost as data flows
    # through the pipeline
    requirement["mapped_control"] = picked
    requirement["mapped_control_chunk_id"] = chunk_id
    requirement["mapping_reasoning"] = reasoning
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
