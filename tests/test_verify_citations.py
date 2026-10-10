"""
Tests for the citation checker. Runs on the placeholder backend (no Phase 1
deps, no API calls), which is enough to prove the checker's logic: a good
citation passes, a broken one is reported, and a made-up control ID is caught.
"""
import json
import pytest

import verify_citations as vc


def _finding(chunk_id="policy-security-001", source="Public_Release_Computer_Security_Policy.pdf",
             locator="Section 3.5 Authentication", control="IA-2"):
    return {"requirement": "r", "coverage": "Partial", "finding": "", "recommendation": "",
            "plain_language": "", "clarifying_questions": [],
            "citation": {"chunk_id": chunk_id, "source": source, "locator": locator},
            "framework_control": control, "status": "pending"}


def _run(tmp_path, findings):
    p = tmp_path / "report.json"
    p.write_text(json.dumps(findings))
    return vc.verify(str(p))


def test_good_citation_and_control_pass(tmp_path):
    r = _run(tmp_path, [_finding()])
    assert r["citation_resolves"] == r["source_matches"] == r["locator_matches"] == 1
    assert r["control_resolves"] == r["controls_cited"] == 1
    assert r["problems"] == []


def test_unknown_chunk_id_is_reported(tmp_path):
    r = _run(tmp_path, [_finding(chunk_id="does-not-exist")])
    assert r["citation_resolves"] == 0
    assert any("chunk_id not found" in p["problem"] for p in r["problems"])


def test_wrong_source_and_locator_are_reported(tmp_path):
    r = _run(tmp_path, [_finding(source="Other.pdf", locator="Section 99")])
    assert r["source_matches"] == 0 and r["locator_matches"] == 0
    assert len(r["problems"]) == 2


def test_invented_control_id_is_caught(tmp_path):
    r = _run(tmp_path, [_finding(control="ZZ-99")])
    assert r["control_resolves"] == 0
    assert any("framework control not found" in p["problem"] for p in r["problems"])


def test_finding_without_a_control_is_not_counted_as_a_control_failure(tmp_path):
    r = _run(tmp_path, [_finding(control=None)])
    assert r["controls_cited"] == 0 and r["problems"] == []
