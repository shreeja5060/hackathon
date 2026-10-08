"""
Tests for the LangGraph review flow (pipeline.start_review / submit_decisions).
No API calls: the three agents are replaced with fakes. What is being tested
is the graph itself: it pauses, nothing is final until a human decides, the
decisions are applied correctly, and a failing chunk doesn't stop the run.
"""
import json
import pytest

import pipeline


@pytest.fixture
def fake_agents(monkeypatch):
    """Stand-in agents: each chunk yields one requirement -> one finding."""
    def extract(chunk):
        return [{"requirement": f"req-{chunk['chunk_id']}", "requirement_text": chunk["text"],
                 "source": chunk["source"], "chunk_id": chunk["chunk_id"], "locator": chunk["locator"]}]
    def map_(req):
        return {**req, "mapped_control": "IA-2", "mapped_control_chunk_id": "framework-nist-ia2",
                "mapping_reasoning": "test"}
    def audit(req):
        return {"requirement": req["requirement"], "coverage": "Partial", "finding": "f",
                "recommendation": "original rec", "plain_language": "p", "clarifying_questions": [],
                "citation": {"chunk_id": req["chunk_id"], "source": req["source"], "locator": req["locator"]},
                "framework_control": "IA-2", "status": "pending"}
    monkeypatch.setattr(pipeline, "extract_requirements", extract)
    monkeypatch.setattr(pipeline, "map_requirement", map_)
    monkeypatch.setattr(pipeline, "audit_requirement", audit)


def _chunks(n):
    return [{"chunk_id": f"c{i}", "text": f"text {i}", "source": "s.pdf", "type": "internal",
             "page": 1, "locator": f"Section {i}"} for i in range(n)]


def test_start_review_pauses_with_pending_findings(fake_agents):
    run = pipeline.start_review(_chunks(3))
    assert run["status"] == "awaiting_human_review"
    assert len(run["findings"]) == 3
    assert all(f["status"] == "pending" for f in run["findings"])
    assert [f["finding_id"] for f in run["findings"]] == ["F001", "F002", "F003"]
    assert run["run_id"]


def test_nothing_is_final_until_decisions_are_submitted(fake_agents):
    run = pipeline.start_review(_chunks(2))
    # The run is suspended: the graph state has an open interrupt and no final findings yet.
    pending = pipeline.get_pending(run["run_id"])
    assert pending is not None and len(pending["findings"]) == 2
    state = pipeline.app.get_state({"configurable": {"thread_id": run["run_id"]}})
    assert state.values.get("final_findings", []) == []


def test_submit_decisions_applies_approve_reject_and_edits(fake_agents):
    run = pipeline.start_review(_chunks(3))
    final = pipeline.submit_decisions(run["run_id"], {
        "F001": {"decision": "approved"},
        "F002": {"decision": "rejected"},
        "F003": {"decision": "approved", "recommendation": "human-edited rec"},
    })
    assert final["status"] == "complete"
    by_id = {f["finding_id"]: f for f in final["findings"]}
    assert by_id["F001"]["status"] == "approved"
    assert by_id["F002"]["status"] == "rejected"
    assert by_id["F003"]["status"] == "approved"
    assert by_id["F003"]["recommendation"] == "human-edited rec"
    assert by_id["F003"]["recommendation_edited_by_human"] is True
    assert by_id["F001"]["recommendation"] == "original rec"   # untouched
    assert len(final["approved"]) == 2 and len(final["rejected"]) == 1


def test_undecided_findings_stay_pending_not_silently_approved(fake_agents):
    run = pipeline.start_review(_chunks(2))
    final = pipeline.submit_decisions(run["run_id"], {"F001": {"decision": "approved"}})
    by_id = {f["finding_id"]: f for f in final["findings"]}
    assert by_id["F001"]["status"] == "approved"
    assert by_id["F002"]["status"] == "pending"
    assert len(final["pending"]) == 1


def test_invalid_decision_value_is_treated_as_pending(fake_agents):
    run = pipeline.start_review(_chunks(1))
    final = pipeline.submit_decisions(run["run_id"], {"F001": {"decision": "maybe"}})
    assert final["findings"][0]["status"] == "pending"


def test_failing_chunk_is_isolated_and_run_still_pauses(fake_agents, monkeypatch):
    def extract(chunk):
        if chunk["chunk_id"] == "c1":
            raise ValueError("simulated Claude failure")
        return [{"requirement": f"req-{chunk['chunk_id']}", "requirement_text": chunk["text"],
                 "source": chunk["source"], "chunk_id": chunk["chunk_id"], "locator": chunk["locator"]}]
    monkeypatch.setattr(pipeline, "extract_requirements", extract)
    run = pipeline.start_review(_chunks(3))
    assert run["status"] == "awaiting_human_review"
    assert len(run["findings"]) == 2                      # c0 and c2 made it
    assert len(run["errors"]) == 1 and run["errors"][0]["stage"] == "extractor"


def test_two_runs_do_not_interfere(fake_agents):
    a = pipeline.start_review(_chunks(1))
    b = pipeline.start_review(_chunks(2))
    fa = pipeline.submit_decisions(a["run_id"], {"F001": {"decision": "rejected"}})
    fb = pipeline.submit_decisions(b["run_id"], {"F001": {"decision": "approved"}, "F002": {"decision": "approved"}})
    assert len(fa["findings"]) == 1 and fa["findings"][0]["status"] == "rejected"
    assert len(fb["findings"]) == 2 and all(f["status"] == "approved" for f in fb["findings"])
