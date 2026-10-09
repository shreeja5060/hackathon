"""The two-person rule: a different reviewer confirms every decision before sign-off."""

import pytest

from phase3_dashboard.core import analysis, review
from phase3_dashboard.core.report import build_report, to_csv

A, B = "Mahsa", "Rashmi Rao"


@pytest.fixture
def two(sample_session):
    sample_session.two_person = True
    return sample_session


def decide_all(session, who=A):
    for f in session.findings:
        if f.status != "pending":
            continue
        if f.approvable:
            review.approve(session, f.finding_id, who)
        else:
            review.reject(session, f.finding_id, who, "Failed validation")


def test_sign_off_waits_for_a_second_reviewer(sim_backend, two):
    decide_all(two)
    ok, reason = review.can_finalize(two, A)
    assert not ok and f"{len(two.findings)} decision(s) still need confirming" in reason
    assert review.counts(two)["awaiting_confirmation"] == len(two.findings)
    with pytest.raises(review.ReviewError):
        analysis.finalize_review(sim_backend, two, A)


def test_nobody_confirms_their_own_decision(two):
    target = two.findings[0]
    review.approve(two, target.finding_id, A)
    for same in (A, " mahsa", "MAHSA "):
        with pytest.raises(review.ReviewError, match="different person"):
            review.confirm(two, target.finding_id, same)
    review.confirm(two, target.finding_id, B, "Agreed")
    assert (target.confirmed_by, target.confirmation_note) == (B, "Agreed")
    with pytest.raises(review.ReviewError, match="already confirmed"):
        review.confirm(two, target.finding_id, "Anu")
    assert two.audit_log[-1].action == "confirmed" and two.audit_log[-1].detail["decided_by"] == A


def test_rejections_need_a_second_reviewer_too(two):
    """Rejecting hides a gap from the report, so it is checked like an approval."""
    gap = next(f for f in two.findings if f.coverage == "Missing")
    review.reject(two, gap.finding_id, A, "Covered by the HR handbook")
    assert gap in review.awaiting_confirmation(two)
    review.confirm(two, gap.finding_id, B)
    assert gap.confirmed and gap not in review.awaiting_confirmation(two)


def test_confirm_all_skips_the_reviewers_own_decisions(two):
    review.approve(two, two.findings[0].finding_id, A)
    review.approve(two, two.findings[1].finding_id, B)
    assert review.confirm_all(two, B) == (1, 1)
    assert two.findings[0].confirmed_by == B and not two.findings[1].confirmed


def test_sending_back_needs_a_reason_and_reopens_the_finding(two):
    target = two.findings[0]
    review.approve(two, target.finding_id, A)
    with pytest.raises(review.ReviewError, match="Say why"):
        review.send_back(two, target.finding_id, B, "no")
    review.send_back(two, target.finding_id, B, "The control mapping looks wrong")
    assert target.status == "pending" and target.reviewer is None
    event = two.audit_log[-1]
    assert event.action == "sent_back" and event.detail["decided_by"] == A


def test_only_the_decider_can_undo_and_undoing_clears_the_confirmation(two):
    target = two.findings[0]
    review.approve(two, target.finding_id, A)
    review.confirm(two, target.finding_id, B)
    with pytest.raises(review.ReviewError, match="Only Mahsa can undo"):
        review.reopen(two, target.finding_id, B)
    review.reopen(two, target.finding_id, A)
    assert target.status == "pending" and target.confirmed_by is None


def test_signed_report_names_both_people(sim_backend, two):
    decide_all(two)
    review.confirm_all(two, B)
    assert analysis.finalize_review(sim_backend, two, B) == []
    report = build_report(two)
    assert report["review"]["two_person"] and report["review"]["confirmed"] == len(two.findings)
    first = report["approved_findings"][0]
    assert (first["reviewer"], first["confirmed_by"]) == (A, B)
    assert {"reviewer", "confirmed_by", "confirmed_at"} <= set(to_csv(report).splitlines()[0].split(","))
    assert two.audit_log[-1].detail["two_person"] is True


def test_the_pipeline_hears_who_decided(sim_backend, two):
    decide_all(two)
    review.confirm_all(two, B)
    sent = {}
    original = sim_backend.submit_review

    def spy(thread_id, decisions):
        sent.update(decisions)
        return original(thread_id, decisions)

    sim_backend.submit_review = spy
    assert analysis.finalize_review(sim_backend, two, B) == []
    assert all(d["reviewer"] == A and d["confirmed_by"] == B for d in sent.values())
