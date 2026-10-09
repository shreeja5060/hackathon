"""The simulator must produce exactly what Phase 1 and Phase 2 produce."""

import json
from collections import Counter

import pytest

from phase3_dashboard.backends import sample_data
from phase3_dashboard.backends.simulated import (
    SimulatedCommand,
    SimulatedPipeline,
    chunk_pages_like_phase1,
    make_policy_chunk,
    match_topic,
    simulated_audit_requirement,
    simulated_extract_requirements,
    simulated_map_requirement,
)
from phase3_dashboard.core import analysis
from phase3_dashboard.core.contracts import COVERAGE_VALUES, SEARCH_FIELDS

from .conftest import make_pdf

# Key sets copied from the Phase 1 and Phase 2 code.
CHUNK_KEYS = {"chunk_id", "text", "source", "type", "doc_kind", "page", "locator"}  # chunk_policies.make_chunk
EXTRACTOR_KEYS = {"requirement", "requirement_text", "source", "chunk_id", "locator"}  # extractor.py
MAPPER_KEYS = EXTRACTOR_KEYS | {"mapped_control", "mapping_reasoning"}  # mapper.py
AUDITOR_KEYS = {"requirement", "coverage", "finding", "recommendation", "plain_language",
                "clarifying_questions", "citation", "framework_control", "status"}  # auditor.py
CITATION_KEYS = {"chunk_id", "source", "locator"}


def all_sample_chunks(backend):
    return [chunk for source in backend.list_policies() for chunk in backend.load_policy(source).chunks]


def test_policy_chunks_match_phase1_schema(sim_backend):
    for chunk in all_sample_chunks(sim_backend):
        assert set(chunk) == CHUNK_KEYS
        assert chunk["type"] == "internal" and chunk["doc_kind"] == "policy"
        assert chunk["chunk_id"].startswith("policy-") and len(chunk["chunk_id"]) == 7 + 64
        assert chunk["locator"].startswith("Section ")


def test_chunk_ids_follow_phase1_hashing():
    chunk = make_policy_chunk("Policy.pdf", 2, "3.5 Credential Sharing", "Users must not share passwords.")
    again = make_policy_chunk("Policy.pdf", 2, "3.5 Credential Sharing", "Users must not share passwords.")
    assert chunk == again
    assert chunk["locator"] == "Section 3.5 Credential Sharing"
    assert chunk["text"] == "3.5 Credential Sharing\n\nUsers must not share passwords."


def test_agent_outputs_have_the_phase2_shapes(sim_backend):
    for chunk in all_sample_chunks(sim_backend):
        requirements = simulated_extract_requirements(chunk)
        for requirement in requirements:
            assert set(requirement) == EXTRACTOR_KEYS
        mapped = [simulated_map_requirement(r) for r in requirements]
        for item in mapped:
            assert set(item) == MAPPER_KEYS
        assert mapped == requirements  # mapper.py mutates the same dicts
        for entry in (simulated_audit_requirement(m) for m in mapped):
            assert set(entry) == AUDITOR_KEYS
            assert set(entry["citation"]) == CITATION_KEYS
            assert entry["coverage"] in COVERAGE_VALUES
            assert entry["status"] == "pending"
            assert isinstance(entry["plain_language"], str) and entry["plain_language"]
            if entry["coverage"] == "Full":
                assert entry["recommendation"] == "None"  # auditor.py's convention
                assert entry["clarifying_questions"] == []


def test_mapper_agrees_with_the_extractor_topic(sim_backend):
    for chunk in all_sample_chunks(sim_backend):
        heading = chunk["text"].partition("\n\n")[0]
        for requirement in simulated_extract_requirements(chunk):
            expected = match_topic(requirement["requirement_text"]) or match_topic(heading)
            mapped = simulated_map_requirement(dict(requirement))
            assert mapped["mapped_control"] == (expected[1] if expected else None), requirement


def test_samples_cover_every_coverage_value(sim_backend):
    seen = Counter()
    for source in sim_backend.list_policies():
        document = sim_backend.load_policy(source)
        session = analysis.analyze_policy(sim_backend, document, [c["chunk_id"] for c in document.chunks])
        seen.update(f.coverage for f in session.findings)
    assert set(seen) == set(COVERAGE_VALUES)


def test_pipeline_pauses_then_resumes_like_pipeline_py(sim_backend):
    chunks = sim_backend.load_policy(sim_backend.list_policies()[0]).chunks[3:6]
    pipeline = SimulatedPipeline()
    config = {"configurable": {"thread_id": "t-1"}}
    empty = {k: [] for k in ("requirements", "mapped", "findings", "final_findings", "errors")}
    state = pipeline.invoke({"chunks": chunks, **empty}, config)
    assert {"chunks", "requirements", "mapped", "findings", "final_findings", "errors", "__interrupt__"} <= set(state)
    pause = state["__interrupt__"][0].value
    assert pause["message"] == "Review these findings. Nothing is final until you decide."
    ids = [f["finding_id"] for f in pause["findings"]]
    assert ids == [f"F{n:03d}" for n in range(1, len(ids) + 1)] and len(ids) >= 3

    final = pipeline.invoke(SimulatedCommand(resume={
        "F001": {"decision": "approved", "recommendation": "Edited."},
        "F002": {"decision": "rejected"},
        "F003": {"decision": "maybe"},  # not a valid decision: stays pending, like pipeline.py
    }), config)["final_findings"]
    assert [f["status"] for f in final[:3]] == ["approved", "rejected", "pending"]
    assert final[0]["recommendation"] == "Edited." and final[0]["recommendation_edited_by_human"] is True
    assert all(f["status"] == "pending" for f in final[3:])  # undecided stays pending
    with pytest.raises(RuntimeError):
        pipeline.invoke(SimulatedCommand(resume={}), config)  # already resumed


def test_a_failing_section_is_reported_and_the_rest_still_run(sim_backend):
    sim_backend.set_failing_section(1)
    document = sim_backend.load_policy(sim_backend.list_policies()[0])
    session = analysis.analyze_policy(sim_backend, document, [c["chunk_id"] for c in document.chunks])
    [failed] = session.run.section_errors
    assert failed.error.startswith("Extractor failed: Expecting value")
    assert failed.paused  # pipeline.py records the error and still pauses
    assert len(session.run.ok_threads) == len(document.chunks)


def test_search_matches_the_retriever_contract(sim_backend):
    results = sim_backend.search("Can employees share passwords or authentication tokens?", type="internal", top_k=3)
    assert len(results) == 3
    assert all(set(r) == set(SEARCH_FIELDS) and r["type"] == "internal" for r in results)
    assert [r["score"] for r in results] == sorted((r["score"] for r in results), reverse=True)
    assert "3.5 Credential Sharing" in results[0]["locator"]

    framework = sim_backend.search("multifactor authentication MFA", type="framework", top_k=2)
    assert framework[0]["locator"] == "IA-2(1)" and framework[0]["page"] is None
    assert sim_backend.search("password", type="evidence") == []


@pytest.mark.parametrize("kwargs", [
    {"query": "", "type": None},
    {"query": "passwords", "type": "policy"},  # the old placeholder type; the real retriever rejects it too
    {"query": "passwords", "type": None, "top_k": 0},
    {"query": "passwords", "type": None, "top_k": True},
])
def test_search_rejects_the_same_bad_arguments_as_the_retriever(sim_backend, kwargs):
    with pytest.raises(ValueError):
        sim_backend.search(**kwargs)


def test_chat_answers_with_citations(sim_backend):
    reply = sim_backend.ask("Can staff share passwords?")
    assert "Credential Sharing" in reply["answer"]
    assert reply["citations"] and set(reply["citations"][0]) == set(SEARCH_FIELDS)
    assert sim_backend.ask("zebra migration patterns")["citations"] == []


def test_uploaded_pdf_is_chunked_like_phase1(sim_backend):
    pdf = make_pdf([
        "PUBLIC RELEASE\n1 Purpose\nThis policy protects information.\n2.1 Passwords\n"
        "Passwords must be at least 12 characters long.",
        "2.2 Logging\nSecurity logs should be reviewed regularly.",
    ])
    document = sim_backend.parse_upload("My Policy.pdf", pdf)
    assert [c["locator"] for c in document.chunks] == ["Section 1 Purpose", "Section 2.1 Passwords", "Section 2.2 Logging"]
    assert [c["page"] for c in document.chunks] == [1, 1, 2]
    assert all("PUBLIC RELEASE" not in c["text"] for c in document.chunks)


def test_pdf_without_numbered_headings_falls_back_to_pages():
    chunks, notes = chunk_pages_like_phase1([
        {"source": "x.pdf", "page": 1, "text": "Staff must lock screens."},
        {"source": "x.pdf", "page": 2, "text": "Backups should run nightly."},
    ])
    assert [c["page"] for c in chunks] == [1, 2] and all(c["locator"] is None for c in chunks)
    assert notes and "each page became one section" in notes[0]


def test_sample_data_is_consistent():
    for _, _, control in sample_data.TOPIC_RULES:
        assert control in sample_data.CONTROLS
    for control_id in sample_data.CONTROLS:
        assert control_id.split("-")[0] in sample_data.FAMILIES
    json.dumps(sample_data.SAMPLE_POLICIES)  # plain data only


def test_simulated_assistant_suggests_wording_for_the_findings_named(sim_backend, sample_session):
    from phase3_dashboard.core import review

    review.reject(sample_session, "F-003", "Mahsa", "Too vague")
    context = [{**f.raw, "finding_id": f.finding_id, "status": f.status, "coverage": f.coverage}
               for f in sample_session.findings]
    rejected = sim_backend.ask("Suggest better wording for the rejected findings", findings=context)
    assert [s["finding_id"] for s in rejected["suggestions"]] == ["F-003"]
    assert rejected["citations"] and rejected["citations"][0]["type"] == "framework"
    named = sim_backend.ask("Improve the recommendation for F-4 and F-005", findings=context)
    assert [s["finding_id"] for s in named["suggestions"]] == ["F-004", "F-005"]
    listing = sim_backend.ask("Which findings have Missing coverage?", findings=context)
    assert listing["suggestions"] == [] and "F-001" in listing["answer"]
    assert sim_backend.ask("Suggest better wording", findings=[])["suggestions"] == []
