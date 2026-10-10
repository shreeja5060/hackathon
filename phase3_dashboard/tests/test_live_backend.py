"""End to end against the real code on main.

Real: Phase 1's chunker, Phase 2's Extractor, Mapper and Auditor (their prompt
building, JSON parsing, citation handling and no-control path), Phase 2's
LangGraph pipeline (start_review / submit_decisions, with its real pause), and
this dashboard's review flow.

Faked, so no API key or search index is needed:
* Claude's replies: each agent's client returns canned JSON, the same way
  Phase 2's own tests do it;
* retrieval: the agents use shared/fake_search.py, the placeholder that
  pipeline.py itself falls back to.

Skipped until a pipeline.py with start_review() is on the branch.
"""

import json
import sys
import types

import pytest

from phase3_dashboard.backends.base import BackendUnavailable, PolicyDocument
from phase3_dashboard.core import analysis, review
from phase3_dashboard.core.contracts import normalize_chat_answer
from phase3_dashboard.core.security import UploadRejected

from .conftest import CHUNKS, make_pdf, needs_chunker, needs_pipeline, needs_qa_agent

def analyze(backend, chunks=CHUNKS):
    document = PolicyDocument(source="Computer Security Policy.pdf", chunks=chunks, origin="indexed")
    return analysis.analyze_policy(backend, document, [c["chunk_id"] for c in chunks])


# ------------------------------------------------------------------ tests

@needs_pipeline
def test_end_to_end_review_through_the_real_pipeline(live):
    session = analyze(live, CHUNKS[:1])
    assert session.run.problem_sections == []
    assert session.run.retrieval_backends  # the dashboard knows which retriever Phase 2 used
    mfa, password = session.findings

    # The real Auditor's output, as the reviewer sees it.
    assert (mfa.coverage, mfa.framework_control, mfa.source_finding_id) == ("Partial", "IA-2", "F001")
    assert mfa.plain_language == "The rule says MFA is needed but not where."
    assert mfa.clarifying_questions == ["Which systems count as privileged?"]
    assert mfa.requirement_text.startswith("Privileged accounts should use")  # from the mapper output
    assert mfa.citation.page == 2 and mfa.problems == []
    # The real Auditor's no-control path, without a Claude call.
    assert (password.coverage, password.framework_control) == ("Not observable", None)
    assert password.flags == ["No framework control was mapped."]

    review.approve(session, mfa.finding_id, "Mahsa", edited_recommendation="Require MFA on all admin accounts.")
    review.reject(session, password.finding_id, "Mahsa", "Covered by the password standard")
    assert analysis.finalize_review(live, session, "Mahsa") == []
    assert session.finalized

    # Phase 2's own record of the run now matches the human's decisions.
    final = live._app.get_state(live._config(mfa.thread_id)).values["final_findings"]
    assert [f["status"] for f in final] == ["approved", "rejected"]
    assert final[0]["recommendation"] == "Require MFA on all admin accounts."
    assert final[0]["recommendation_edited_by_human"] is True


@needs_pipeline
def test_a_bad_model_reply_is_recorded_and_the_rest_still_runs(live):
    session = analyze(live)
    good, bad = session.run.sections
    assert good.status == "ok" and good.finding_count == 2
    assert bad.status == "error" and bad.error.startswith("Extractor failed")
    assert bad.paused  # pipeline.py records the failure and still pauses
    for finding in session.findings:
        review.approve(session, finding.finding_id, "Mahsa")
    assert analysis.finalize_review(live, session, "Mahsa") == []


@needs_pipeline
def test_nothing_is_final_until_the_reviewer_finalizes(live):
    session = analyze(live, CHUNKS[:1])
    values = live._app.get_state(live._config(session.findings[0].thread_id)).values
    assert values.get("final_findings") == []  # the real run is suspended at human review
    review.approve(session, session.findings[0].finding_id, "Mahsa")
    with pytest.raises(review.ReviewError):
        analysis.finalize_review(live, session, "Mahsa")  # one finding is still pending


@needs_pipeline
def test_policies_and_framework_version_come_from_phase1_files(live, tmp_path):
    processed = tmp_path / "data" / "processed"
    processed.mkdir(parents=True)
    (processed / "policy_chunks.json").write_text(json.dumps(CHUNKS))
    (tmp_path / "chroma_db").mkdir()
    (tmp_path / "chroma_db" / "index_manifest.json").write_text(json.dumps({"sources": {
        "NIST_SP-800-53_rev5_catalog.json": {"title": "NIST SP 800-53 Revision 5, Release 5.2.0",
                                             "version": "5.2.0", "publication_date": "2025-08-27"}}}))
    live._root, live._indexed = tmp_path, None
    assert live.list_policies() == ["Computer Security Policy.pdf"]
    assert len(live.load_policy("Computer Security Policy.pdf").chunks) == 2
    info = live.framework_info()
    assert (info["version"], info["publication_date"]) == ("5.2.0", "2025-08-27")


@needs_pipeline
@needs_chunker
def test_uploads_are_split_by_phase1s_chunker(live):
    document = live.parse_upload("Uploaded Policy.pdf", make_pdf(
        ["1 Purpose\nThis policy protects data.\n2 Passwords\nPasswords must be long."]))
    assert [c["locator"] for c in document.chunks] == ["Section 1 Purpose", "Section 2 Passwords"]
    # A PDF without numbered sections: rejected by the chunker on main today; split into
    # "Page N" chunks once Maryam's phase1-document-ingestion branch is merged. Both are fine.
    try:
        plain = live.parse_upload("No Headings.pdf", make_pdf(["Just a paragraph without numbered sections."]))
    except UploadRejected as exc:
        assert "chunker" in str(exc)
    else:
        assert [c["locator"] for c in plain.chunks] == ["Page 1"] and plain.chunks[0]["page"] == 1


@needs_pipeline
def test_chat_falls_back_to_search_then_uses_the_qa_agent_with_review_context(live, monkeypatch):
    if live._qa_function() is None:  # before Anu's qa_agent is merged: search results only
        fallback = normalize_chat_answer(live.ask("multifactor authentication"))
        assert fallback.answer.startswith("Retrieval only") and fallback.citations

    received = {}

    def ask(question, findings=None, history=None):
        received.update(question=question, findings=findings, history=history)
        return {"answer": "Use MFA for admins.", "citations": [], "suggestions": []}

    monkeypatch.setitem(sys.modules, "team_qa_agent", types.SimpleNamespace(ask=ask))
    monkeypatch.setenv("COPILOT_QA_MODULE", "team_qa_agent:ask")
    findings = [{"finding_id": "F-001", "requirement": "MFA"}]
    history = [{"role": "user", "content": "hi"}]
    answer = normalize_chat_answer(live.ask("What about MFA?", findings=findings, history=history))
    assert answer.answer == "Use MFA for admins."
    assert received == {"question": "What about MFA?", "findings": findings, "history": history}


def test_missing_phase2_code_gives_a_clear_message(tmp_path):
    from phase3_dashboard.backends.live import LiveBackend

    with pytest.raises(BackendUnavailable) as error:
        LiveBackend(repo_root=tmp_path)
    assert "phase2" in str(error.value).lower() or "langgraph" in str(error.value).lower()


# ---------------------------------------------------------------- Anu's Q&A agent inside the review

@needs_pipeline
@needs_qa_agent
def test_qa_agent_reads_the_review_and_its_suggestion_reaches_the_reviewer(live, qa_agent):
    from phase3_dashboard.core.contracts import normalize_chat_answer

    from .conftest import SUGGESTED

    session = analyze(live, CHUNKS[:1])
    mfa = session.findings[0]
    raw = live.ask(f"Suggest a stronger recommendation for finding {mfa.finding_id}.",
                   findings=review.assistant_context(session), history=[])
    answer = normalize_chat_answer(raw, known_finding_ids={f.finding_id for f in session.findings})

    # Anu's agent saw the review, fetched the finding's evidence and proposed wording.
    first_prompt = qa_agent.calls[0]["messages"][-1]["content"]
    assert mfa.finding_id in first_prompt and "Partial" in first_prompt
    tool_results = qa_agent.calls[1]["messages"][-1]["content"]
    evidence = next(r["content"] for r in tool_results if "Policy text the finding cites" in r["content"])
    assert "multi-factor authentication where required" in evidence  # the dashboard's cited_text
    assert answer.suggestions == ({"finding_id": mfa.finding_id, "recommendation": SUGGESTED},)
    assert [c["ref"] for c in answer.citations] == [1, 2] and "[1]" in answer.answer and "[2]" in answer.answer

    # The suggestion waits; the reviewer applies it and approves; the pipeline records it.
    review.record_suggestion(session, mfa.finding_id, SUGGESTED, "Mahsa", "Suggest a stronger recommendation")
    assert mfa.recommendation != SUGGESTED and mfa.status == "pending"
    review.approve(session, mfa.finding_id, "Mahsa", edited_recommendation=SUGGESTED)
    review.reject(session, session.findings[1].finding_id, "Mahsa", "No control applies here")
    assert analysis.finalize_review(live, session, "Mahsa") == []
    final = live._app.get_state(live._config(mfa.thread_id)).values["final_findings"]
    assert final[0]["recommendation"] == SUGGESTED and final[0]["recommendation_edited_by_human"] is True
