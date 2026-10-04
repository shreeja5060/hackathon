"""The dashboard's boundary check on Phase 2 output."""

import pytest

from phase3_dashboard.core.contracts import (
    INVALID_COVERAGE,
    ContractError,
    normalize_chat_answer,
    normalize_pipeline_state,
    validate_search_result,
)

CHUNK = {
    "chunk_id": "policy-abc", "text": "3.5 Credential Sharing\n\nUsers must not share passwords.",
    "source": "Computer Security Policy.pdf", "type": "internal", "doc_kind": "policy",
    "page": 2, "locator": "Section 3.5 Credential Sharing",
}


class Pause:  # what langgraph.types.Interrupt looks like to the dashboard
    def __init__(self, value):
        self.value = value


def auditor_entry(**overrides):
    entry = {
        "requirement": "No credential sharing",
        "coverage": "Partial",
        "finding": "Doesn't say how sharing is detected.",
        "recommendation": "Add monitoring for shared credentials.",
        "citation": {"chunk_id": "policy-abc", "source": "Computer Security Policy.pdf",
                     "locator": "Section 3.5 Credential Sharing"},
        "framework_control": "IA-5",
        "status": "pending",
    }
    entry.update(overrides)
    return entry


def state_with(*entries, mapped=None):
    return {
        "mapped": mapped or [],
        "gap_report": list(entries),
        "__interrupt__": [Pause({"message": "Please review these findings", "findings": list(entries)})],
    }


def normalize(*entries, **kwargs):
    return normalize_pipeline_state(state_with(*entries, **kwargs), thread_id="t-1", policy_chunk=CHUNK)


def test_auditor_output_becomes_a_clean_pending_finding():
    mapped = [{"requirement": "No credential sharing", "requirement_text": "Users must not share passwords.",
               "mapping_reasoning": "IA-5 covers authenticators."}]
    [finding] = normalize(auditor_entry(), mapped=mapped)
    assert finding.status == "pending" and finding.approvable
    assert finding.problems == [] and finding.flags == []
    assert finding.citation.page == 2  # recovered from the analyzed chunk
    assert finding.requirement_text == "Users must not share passwords."  # recovered from the mapper
    assert finding.mapping_reasoning == "IA-5 covers authenticators."
    assert finding.citation.label() == "Computer Security Policy.pdf, Section 3.5 Credential Sharing, p. 2"


def test_recommendation_none_means_no_change_needed():
    [finding] = normalize(auditor_entry(coverage="Full", recommendation="None"))
    assert finding.recommendation is None and finding.flags == []


def test_coverage_spelling_is_normalized_but_unknown_values_block_approval():
    [ok] = normalize(auditor_entry(coverage=" not OBSERVABLE "))
    assert ok.coverage == "Not observable"
    [bad] = normalize(auditor_entry(coverage="Mostly full"))
    assert bad.coverage == INVALID_COVERAGE and not bad.approvable


def test_missing_required_fields_block_approval():
    [finding] = normalize({"coverage": "Partial", "status": "pending"})
    assert not finding.approvable
    assert any("requirement" in p for p in finding.problems)
    assert any("citation" in p for p in finding.problems)


def test_ai_cannot_preapprove_a_finding():
    [finding] = normalize(auditor_entry(status="approved"))
    assert finding.status == "pending"
    assert any("only a reviewer" in flag for flag in finding.flags)


def test_mismatched_citation_and_odd_control_are_flagged():
    citation = {"chunk_id": "policy-other", "source": "Other.pdf", "locator": "Section 9"}
    [finding] = normalize(auditor_entry(citation=citation, framework_control="Password stuff"))
    assert finding.approvable  # flags inform the reviewer; they don't block
    assert any("different passage" in f for f in finding.flags)
    assert any("different file" in f for f in finding.flags)
    assert any("doesn't look like a NIST control" in f for f in finding.flags)


def test_unmapped_control_and_gap_without_recommendation_are_flagged():
    [finding] = normalize(auditor_entry(framework_control=None, coverage="Missing", recommendation="None"))
    assert any("No framework control" in f for f in finding.flags)
    assert any("no recommendation" in f for f in finding.flags)


def test_extra_fields_from_the_agents_are_kept():
    [finding] = normalize(auditor_entry(ambiguity="'where required' is undefined"))
    assert finding.extras == {"ambiguity": "'where required' is undefined"}


def test_findings_without_a_review_pause_are_refused():
    with pytest.raises(ContractError):
        normalize_pipeline_state({"gap_report": [auditor_entry()]}, thread_id="t-1")
    with pytest.raises(ContractError):
        normalize_pipeline_state({"__interrupt__": [Pause({"message": "x"})]}, thread_id="t-1")
    with pytest.raises(ContractError):
        normalize_pipeline_state(["not a dict"], thread_id="t-1")


def test_plain_dict_pause_also_works():
    state = {"__interrupt__": [{"findings": [auditor_entry()]}]}
    assert len(normalize_pipeline_state(state, thread_id="t-1")) == 1


def search_hit(**overrides):
    hit = {"chunk_id": "c", "text": "IA-5 text", "source": "NIST_SP-800-53_rev5_catalog.json", "page": None,
           "type": "framework", "doc_kind": "control_catalog", "locator": "IA-5", "score": 0.71}
    hit.update(overrides)
    return hit


def test_search_results_are_validated():
    assert validate_search_result(search_hit()) == search_hit()
    assert validate_search_result({**search_hit(), "parent_chunk_id": "x"}) == search_hit()  # extras dropped
    for bad in ({"score": 3.0}, {"score": float("nan")}, {"type": "policy"}, {"page": 0}, {"text": " "}):
        assert validate_search_result(search_hit(**bad)) is None


def test_chat_answers_are_validated():
    answer = normalize_chat_answer({"answer": "  Use MFA.  ", "citations": [search_hit(), {"bad": 1}]})
    assert answer.answer == "Use MFA." and len(answer.citations) == 1 and answer.dropped_citations == 1
    for bad in (None, {"answer": ""}, {"citations": []}):
        with pytest.raises(ContractError):
            normalize_chat_answer(bad)
