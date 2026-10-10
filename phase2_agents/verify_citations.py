"""
Citation checker for Phase 2 gap reports. No API calls.

Answers one question a judge will ask: "Are these citations real?"

For every finding in a report file it:
  1. looks up citation.chunk_id in the retrieval index (get_original_chunk)
  2. checks the chunk exists
  3. checks the chunk's source document and locator match what the finding claims
  4. if a framework control is cited, checks it resolves in the index too

Run from the repo root:
    python phase2_agents/verify_citations.py full_gap_report.json

On a Mac without Phase 1 deps this runs against shared/fake_search.py (proves
the checker works). In Cloud Shell it runs against Maryam's real 1,241-chunk
index and the real report - that is the proof that matters.
"""

import os
import sys
import json

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


def _framework_control_ids():
    """
    Every framework control ID in the index, read straight from the index
    metadata (exact match). Returns None when the backend cannot enumerate
    the index (the placeholder fallback), in which case verify() falls back
    to a small search.

    Why not just search for the ID? search() ranks by meaning, not by exact
    text, so a real control like IA-2 can rank 6th for its own name and look
    "missing". An exact metadata lookup has no such false alarms.
    """
    try:
        import retriever
        idx = retriever._index()
        col = idx if hasattr(idx, "query") else next(x for x in idx if hasattr(x, "query"))
        metas = col.get(include=["metadatas"])["metadatas"]
        return {m.get("locator") for m in metas if m.get("type") == "framework"}
    except Exception:
        return None


def verify(report_path: str) -> dict:
    with open(report_path) as f:
        findings = json.load(f)
    known_controls = _framework_control_ids()

    results = {
        "backend": RETRIEVER_BACKEND,
        "findings": len(findings),
        "citation_resolves": 0,
        "source_matches": 0,
        "locator_matches": 0,
        "control_resolves": 0,
        "controls_cited": 0,
        "problems": [],
    }

    for i, f in enumerate(findings):
        cit = f.get("citation") or {}
        chunk_id = cit.get("chunk_id")
        chunk = get_original_chunk(chunk_id) if chunk_id else None

        if chunk is None:
            results["problems"].append({"index": i, "requirement": f.get("requirement"),
                                        "problem": f"citation chunk_id not found: {chunk_id}"})
            continue
        results["citation_resolves"] += 1

        if chunk.get("source") == cit.get("source"):
            results["source_matches"] += 1
        else:
            results["problems"].append({"index": i, "requirement": f.get("requirement"),
                                        "problem": f"source mismatch: finding says {cit.get('source')!r}, "
                                                   f"index says {chunk.get('source')!r}"})

        if chunk.get("locator") == cit.get("locator"):
            results["locator_matches"] += 1
        else:
            results["problems"].append({"index": i, "requirement": f.get("requirement"),
                                        "problem": f"locator mismatch: finding says {cit.get('locator')!r}, "
                                                   f"index says {chunk.get('locator')!r}"})

        control = f.get("framework_control")
        if control:
            results["controls_cited"] += 1
            if known_controls is not None:
                found = control in known_controls
            else:  # placeholder backend: small search is enough
                found = any(h.get("locator") == control
                            for h in search(control, type="framework", top_k=5))
            if found:
                results["control_resolves"] += 1
            else:
                results["problems"].append({"index": i, "requirement": f.get("requirement"),
                                            "problem": f"framework control not found in index: {control}"})

    return results


def main():
    if len(sys.argv) != 2:
        print("usage: python phase2_agents/verify_citations.py <gap_report.json>")
        sys.exit(2)

    r = verify(sys.argv[1])
    n = r["findings"]
    print(f"Retrieval backend: {r['backend']}")
    print(f"Findings checked:            {n}")
    print(f"Citation chunk_id resolves:  {r['citation_resolves']}/{n}")
    print(f"Source document matches:     {r['source_matches']}/{n}")
    print(f"Section/locator matches:     {r['locator_matches']}/{n}")
    print(f"Framework control resolves:  {r['control_resolves']}/{r['controls_cited']} "
          f"(of findings that cite a control; exact match against the index)")

    if r["problems"]:
        print(f"\n{len(r['problems'])} problem(s):")
        for p in r["problems"]:
            print(f"  [{p['index']}] {p['requirement']}: {p['problem']}")
        sys.exit(1)
    else:
        print("\nAll citations verified: every finding points at a real chunk, "
              "in the stated document and section.")


if __name__ == "__main__":
    main()
