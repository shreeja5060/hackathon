"""The dashboard's boundary check on Phase 2 output."""

import pytest

from phase3_dashboard.core.contracts import (
    INVALID_COVERAGE,
    ContractError,
    format_pipeline_errors,
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
        "finding_id": "F001",
        "requirement": "No credential sharing",
        "coverage": "Partial",
        "finding": "Doesn't say how sharing is detected.",
        "recommendation": "Add monitoring for shared credentials.",
        "plain_language": "Sharing passwords is banned, but nobody checks.",
        "clarifying_questions": ["Is shared-credential use monitored anywhere?"],
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
    assert finding.source_finding_id == "F001"
    assert finding.plain_language == "Sharing passwords is banned, but nobody checks."
    assert finding.clarifying_questions == ["Is shared-credential use monitored anywhere?"]


def test_mapper_details_are_matched_by_name_even_if_order_shifts():
    # The auditor skips items that fail, so mapped and findings can be out of step.
    mapped = [{"requirement": "Something that failed", "requirement_text": "x"},
              {"requirement": "No credential sharing", "requirement_text": "Users must not share passwords."}]
    [finding] = normalize(auditor_entry(), mapped=mapped)
    assert finding.requirement_text == "Users must not share passwords."


def test_findings_need_a_unique_pipeline_id_to_be_approvable():
    [no_id] = normalize(auditor_entry(finding_id=None))
    assert not no_id.approvable and any("finding_id" in p for p in no_id.problems)
    first, second = normalize(auditor_entry(), auditor_entry())
    assert first.approvable and not second.approvable


def test_pipeline_errors_are_readable():
    state = {"errors": [
        {"stage": "extractor", "chunk_id": "policy-" + "a" * 64, "error": "Expecting value: line 1"},
        {"stage": "auditor", "requirement": "MFA", "error": "timeout"},
    ]}
    assert format_pipeline_errors(state) == [
        "Extractor failed: Expecting value: line 1",
        "Auditor failed on MFA: timeout",
    ]
    assert format_pipeline_errors({}) == []


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
    no_score = {k: v for k, v in search_hit().items() if k not in ("score", "doc_kind")}  # like fake_search
    assert validate_search_result(no_score) == {**search_hit(), "score": None, "doc_kind": None}
    assert validate_search_result({**search_hit(), "parent_chunk_id": "x"}) == search_hit()  # extras dropped
    for bad in ({"score": 3.0}, {"score": float("nan")}, {"type": "policy"}, {"page": 0}, {"text": " "}):
        assert validate_search_result(search_hit(**bad)) is None


def test_chat_answers_are_validated():
    answer = normalize_chat_answer({"answer": "  Use MFA.  ", "citations": [search_hit(), {"bad": 1}]})
    assert answer.answer == "Use MFA." and len(answer.citations) == 1 and answer.dropped_citations == 1
    for bad in (None, {"answer": ""}, {"citations": []}):
        with pytest.raises(ContractError):
            normalize_chat_answer(bad)


def test_chat_suggestions_are_validated_and_citations_keep_their_numbers():
    answer = normalize_chat_answer({
        "answer": "Use MFA [1] and see [3].",
        "citations": [search_hit(), {"bad": 1}, search_hit(chunk_id="d", locator="IA-2")],
        "suggestions": [
            {"finding_id": "F-001", "recommendation": "  Require   MFA  "},
            {"finding_id": "F-001", "recommendation": "Second one for the same finding"},
            {"finding_id": "F-404", "recommendation": "Not in this review"},
            {"finding_id": "F-002", "recommendation": "   "},
            "not a dict",
        ],
    }, known_finding_ids={"F-001", "F-002"})
    assert [c["ref"] for c in answer.citations] == [1, 3]  # [3] still points at the IA-2 passage
    assert answer.suggestions == ({"finding_id": "F-001", "recommendation": "Require MFA"},)
    assert answer.dropped_suggestions == 4


def test_answers_without_suggestions_still_work():
    answer = normalize_chat_answer({"answer": "Yes.", "citations": []})
    assert answer.suggestions == () and answer.dropped_suggestions == 0
