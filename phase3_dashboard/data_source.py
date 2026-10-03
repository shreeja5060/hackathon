"""
Phase 3: where the dashboard gets its findings from.

Phase 1 (now): load shared/sample_findings.json, which has the same shape as
the Auditor's output. Phase 2 swaps run_analysis() for Shreeja's LangGraph
pipeline; the rest of the dashboard only ever sees the normalized findings
returned here, so it doesn't change.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_DIR = REPO_ROOT / "data" / "policies"
SAMPLE_FINDINGS = REPO_ROOT / "shared" / "sample_findings.json"

COVERAGE_LEVELS = ["Full", "Partial", "Missing", "Not observable"]
STATUSES = ["pending", "approved", "rejected"]


def list_policies() -> list[str]:
    """Policy PDFs that ship with the repo."""
    return sorted(p.name for p in POLICY_DIR.glob("*.pdf"))


def normalize(raw_findings: list[dict]) -> list[dict]:
    """
    Give every finding a stable ID and the fields the UI relies on, so a
    missing key from an agent never crashes the page.
    """
    findings = []
    for i, f in enumerate(raw_findings, start=1):
        citation = f.get("citation") or {}
        findings.append({
            "id": f"F{i:02d}",
            "requirement": f.get("requirement", "(unnamed requirement)"),
            "coverage": f.get("coverage", "Not observable"),
            "finding": f.get("finding", ""),
            "recommendation": f.get("recommendation", ""),
            "framework_control": f.get("framework_control") or f.get("mapped_control", ""),
            "citation": {
                "chunk_id": citation.get("chunk_id", ""),
                "source": citation.get("source", ""),
                "locator": citation.get("locator", ""),
            },
            "cited_text": f.get("cited_text", ""),
            "status": f.get("status", "pending") if f.get("status") in STATUSES else "pending",
            "reviewer_note": f.get("reviewer_note", ""),
        })
    return findings


def run_analysis(policy_name: str) -> list[dict]:
    """
    Phase 1: return the sample findings for any policy.
    Phase 2: run Extractor -> Mapper -> Auditor on the chosen policy.
    """
    raw = json.loads(SAMPLE_FINDINGS.read_text(encoding="utf-8"))
    return normalize(raw)
