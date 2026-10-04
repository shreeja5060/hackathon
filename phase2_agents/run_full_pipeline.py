"""
Run the full Phase 2 pipeline across EVERY available policy chunk, not just
one. This is the real-scale test: it proves the system works across a whole
document's worth of requirements, not a single cherry-picked sentence.

This does NOT use LangGraph's interrupt (that's proven working separately in
pipeline.py). This script is about scale and robustness: process everything,
skip anything that fails, and produce one complete gap report at the end.

Today this runs against fake_search.py. The moment Maryam's real retriever is
merged, changing ONE import line below makes this run against every real
policy PDF and NIST control instead - nothing else in this file changes.
"""

import os
import sys
import json

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


from extractor import extract_requirements
from mapper import map_requirement
from auditor import audit_requirement


def run_full_pipeline() -> list[dict]:
    """
    Pulls every available policy chunk, runs each through the full
    Extractor -> Mapper -> Auditor pipeline, and returns one combined list
    of gap-report entries. Failures on one chunk don't stop the others -
    this is the error-handling safety net for a live demo.
    """
    # top_k set high so we get every policy chunk available, not just the
    # top few matches for one query
    policy_chunks = search("security policy requirements users must", type="internal", top_k=200)
    print(f"Retrieval backend: {RETRIEVER_BACKEND}")
    print(f"Found {len(policy_chunks)} policy chunk(s) to process.\n")

    full_gap_report = []
    errors = []

    for i, chunk in enumerate(policy_chunks, start=1):
        print(f"[{i}/{len(policy_chunks)}] Processing: {chunk['source']} ({chunk['locator']})")

        try:
            requirements = extract_requirements(chunk)
        except Exception as e:
            errors.append({"stage": "extractor", "chunk": chunk["chunk_id"], "error": str(e)})
            print(f"    Extractor failed, skipping this chunk: {e}")
            continue

        for req in requirements:
            try:
                mapped = map_requirement(req)
            except Exception as e:
                errors.append({"stage": "mapper", "requirement": req["requirement"], "error": str(e)})
                print(f"    Mapper failed on '{req['requirement']}', skipping: {e}")
                continue

            try:
                audited = audit_requirement(mapped)
            except Exception as e:
                errors.append({"stage": "auditor", "requirement": req["requirement"], "error": str(e)})
                print(f"    Auditor failed on '{req['requirement']}', skipping: {e}")
                continue

            full_gap_report.append(audited)
            print(f"    -> {audited['requirement']}: {audited['coverage']}")

    print(f"\nDone. {len(full_gap_report)} finding(s) produced, {len(errors)} error(s).")

    with open("full_gap_report.json", "w") as f:
        json.dump(full_gap_report, f, indent=2)

    if errors:
        with open("pipeline_errors.json", "w") as f:
            json.dump(errors, f, indent=2)
        print("Some chunks failed - see pipeline_errors.json for details.")

    print("Saved complete report to full_gap_report.json")
    return full_gap_report


if __name__ == "__main__":
    run_full_pipeline()
