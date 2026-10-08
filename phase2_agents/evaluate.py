"""
Phase 2 accuracy evaluation against a human-labeled gold set.

Needs the API key (it runs the Mapper and Auditor for real). Run from the
repo root once eval/gold_set.json has been filled in:

    python phase2_agents/evaluate.py

Why skip the Extractor? The gold set already supplies the exact requirement
sentence, so we feed that straight to the Mapper. That isolates the two
judgment steps we want to measure (which control? how well covered?) from
the extraction step, which is measured separately by the full-run output.

What it reports:
  - Mapper accuracy:    picked control is in the item's acceptable_controls
  - Auditor agreement:  coverage verdict == expected_coverage
  - Citation validity:  the finding's citation resolves to the gold chunk
  - Latency per item
Unlabeled items (blank expected fields) are run but not scored.
"""

import os
import sys
import json
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(_HERE)
sys.path.append(os.path.join(_HERE, "..", "shared"))
sys.path.append(os.path.join(_HERE, "..", "phase1_ingestion"))
try:
    from retriever import search
    RETRIEVER_BACKEND = "real (phase1_ingestion/retriever.py)"
except Exception:
    from fake_search import search
    RETRIEVER_BACKEND = "placeholder (shared/fake_search.py)"

from mapper import map_requirement
from auditor import audit_requirement

GOLD_PATH = os.path.join(_HERE, "..", "eval", "gold_set.json")
OUT_PATH = os.path.join(_HERE, "..", "eval", "results.json")


def find_gold_chunk_id(item: dict) -> str | None:
    """Locate the real chunk this gold sentence came from, so the pipeline
    gets a genuine citation to carry through (and we can verify it)."""
    hits = search(item["requirement_text"], type="internal", top_k=5)
    for h in hits:
        if item["requirement_text"][:60] in h.get("text", ""):
            return h["chunk_id"]
    return hits[0]["chunk_id"] if hits else None


def run_item(item: dict) -> dict:
    t0 = time.time()
    chunk_id = find_gold_chunk_id(item)
    req = {
        "requirement": item["id"],
        "requirement_text": item["requirement_text"],
        "source": item["source"],
        "chunk_id": chunk_id,
        "locator": item["locator"],
    }
    mapped = map_requirement(req)
    finding = audit_requirement(mapped)
    return {
        "id": item["id"],
        "picked_control": mapped.get("mapped_control"),
        "picked_coverage": finding.get("coverage"),
        "citation_chunk_id": finding.get("citation", {}).get("chunk_id"),
        "gold_chunk_id": chunk_id,
        "latency_s": round(time.time() - t0, 2),
        "finding": finding,
    }


def main():
    with open(GOLD_PATH) as f:
        gold = json.load(f)

    print(f"Retrieval backend: {RETRIEVER_BACKEND}")
    print(f"Gold items: {len(gold)}\n")

    rows, errors = [], []
    for item in gold:
        try:
            rows.append(run_item(item))
            r = rows[-1]
            print(f"{item['id']}: control={r['picked_control']} coverage={r['picked_coverage']} ({r['latency_s']}s)")
        except Exception as e:
            errors.append({"id": item["id"], "error": str(e)})
            print(f"{item['id']}: ERROR {e}")

    # ---- scoring (labeled items only) ----
    labeled = [(g, r) for g, r in zip(gold, rows) if g.get("expected_control") and g.get("expected_coverage")]
    control_ok = cov_ok = cit_ok = 0
    detail = []
    for g, r in labeled:
        acceptable = set(g.get("acceptable_controls") or []) | {g["expected_control"]}
        c = r["picked_control"] in acceptable
        v = r["picked_coverage"] == g["expected_coverage"]
        k = r["citation_chunk_id"] == r["gold_chunk_id"] and r["gold_chunk_id"] is not None
        control_ok += c; cov_ok += v; cit_ok += k
        detail.append({"id": g["id"], "expected_control": g["expected_control"], "picked_control": r["picked_control"],
                       "control_correct": c, "expected_coverage": g["expected_coverage"],
                       "picked_coverage": r["picked_coverage"], "coverage_correct": v, "citation_valid": k})

    n = len(labeled)
    print("\n=== RESULTS ===")
    print(f"Labeled items:       {n} of {len(gold)}")
    if n:
        print(f"Mapper accuracy:     {control_ok}/{n} = {100*control_ok/n:.0f}%  (picked control in acceptable set)")
        print(f"Auditor agreement:   {cov_ok}/{n} = {100*cov_ok/n:.0f}%  (coverage verdict matches human)")
        print(f"Citation validity:   {cit_ok}/{n} = {100*cit_ok/n:.0f}%")
    else:
        print("No labeled items yet - fill expected_control / expected_coverage in eval/gold_set.json")
    if rows:
        print(f"Avg latency/item:    {sum(r['latency_s'] for r in rows)/len(rows):.1f}s")
    if errors:
        print(f"Errors:              {len(errors)}")

    with open(OUT_PATH, "w") as f:
        json.dump({"backend": RETRIEVER_BACKEND, "labeled": n, "mapper_accuracy": control_ok,
                   "auditor_agreement": cov_ok, "citation_validity": cit_ok,
                   "detail": detail, "runs": rows, "errors": errors}, f, indent=2)
    print(f"\nSaved to {os.path.relpath(OUT_PATH)}")


if __name__ == "__main__":
    main()
