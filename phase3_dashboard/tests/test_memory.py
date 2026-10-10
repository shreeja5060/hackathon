"""The permanent memory: reviews written to the ledger as they happen, and reopened later."""

import sqlite3

import pytest

from phase3_dashboard.backends.simulated import SimulatedBackend
from phase3_dashboard.core import analysis, memory, review
from phase3_dashboard.core.report import build_report, to_markdown

A, B = "Mahsa", "Shreeja"


@pytest.fixture
def mem(tmp_path):
    return memory.Memory(tmp_path / "ledger.sqlite3", tmp_path / "documents")


def analyze(backend, mem, name=None, two_person=True, actor=A):
    document = backend.load_policy(name or backend.list_policies()[0])
    info = mem.register(document, None, actor)
    session = analysis.analyze_policy(backend, document, [c["chunk_id"] for c in document.chunks])
    session.two_person = two_person
    mem.record_analysis(session, info, actor, backend.model_description)
    return session


def first(session, coverage):
    return next(f for f in session.findings if f.coverage == coverage and f.status == "pending")


def test_a_review_survives_and_reopens_exactly_as_it_was(sim_backend, mem):
    session = analyze(sim_backend, mem)
    partial, missing, other = first(session, "Partial"), first(session, "Missing"), session.findings[-1]
    review.record_suggestion(session, partial.finding_id, "Name who approves accounts.", A, "Improve it")
    review.approve(session, partial.finding_id, A, edited_recommendation="Name who approves accounts.")
    review.reject(session, missing.finding_id, A, "Handled by HR onboarding")
    assert mem.sync(session) == 3 and mem.unsynced(session) == 0

    later = mem.restore(session.ledger_run_id)  # a new session, e.g. after a restart
    again = later.get(partial.finding_id)
    assert (again.status, again.reviewer, again.recommendation_edited, again.recommendation_origin) == (
        "approved", A, "Name who approves accounts.", "assistant")
    assert again.assistant_suggestion == "Name who approves accounts."
    assert later.get(missing.finding_id).status == "rejected"
    assert later.get(missing.finding_id).reviewer_note == "Handled by HR onboarding"
    assert later.get(other.finding_id).status == "pending"
    assert later.restored and later.two_person and later.run.policy_source == session.run.policy_source
    assert [f.finding_id for f in later.findings] == [f.finding_id for f in session.findings]
    cited = {f.citation.chunk_id for f in session.findings}
    assert {cid: later.passages[cid] for cid in cited} == {cid: session.passages[cid] for cid in cited}  # evidence too
    assert [e.action for e in later.audit_log] == ["assistant_suggested", "approved_with_ai_suggestion", "rejected"]


def test_a_second_person_confirms_in_another_session_and_signs_off(sim_backend, mem):
    session = analyze(sim_backend, mem)
    for finding in session.findings:
        if finding.approvable:
            review.approve(session, finding.finding_id, A)
        else:
            review.reject(session, finding.finding_id, A, "Failed validation")
    mem.sync(session)
    ok, reason = review.can_finalize(session, A)
    assert not ok and "second reviewer" in reason

    second = mem.restore(session.ledger_run_id)
    with pytest.raises(review.ReviewError, match="different person"):
        review.confirm(second, second.findings[0].finding_id, "mahsa ")  # same person, other spelling
    assert review.confirm_all(second, B) == (len(second.findings), 0)
    mem.sync(second)
    fresh = SimulatedBackend(delay=0)  # the paused pipeline runs are gone, as after a restart
    assert analysis.finalize_review(fresh, second, B) == []
    mem.sync(second)
    assert second.finalized and second.closed_threads == set(second.run.ok_threads)

    final = mem.restore(session.ledger_run_id)
    assert final.finalized and final.finalized_by == B
    assert all(f.confirmed_by == B for f in final.findings)
    report = to_markdown(build_report(final))
    assert "Two-person rule" in report and f"Confirmed by:** {B}" in report
    summary = mem.run_summary(session.ledger_run_id)
    assert summary["label"] == f"Signed off by {B}" and summary["awaiting_confirmation"] == 0


def test_a_page_that_is_out_of_date_cannot_overwrite_newer_decisions(sim_backend, mem):
    session = analyze(sim_backend, mem)
    target = session.findings[0]
    review.approve(session, target.finding_id, A)
    mem.sync(session)

    other = mem.restore(session.ledger_run_id)
    review.send_back(other, target.finding_id, B, "Recommendation is too vague")
    mem.sync(other)

    review.approve(session, session.findings[1].finding_id, A)  # the first page still shows the old state
    with pytest.raises(memory.StaleReview, match="another session"):
        mem.sync(session)
    latest = mem.restore(session.ledger_run_id)
    assert latest.get(target.finding_id).status == "pending"  # sent back
    assert latest.get(session.findings[1].finding_id).status == "pending"  # the stale write never landed


def test_the_ledger_itself_refuses_a_self_confirmation(sim_backend, mem):
    session = analyze(sim_backend, mem)
    target = session.findings[0]
    review.approve(session, target.finding_id, A)
    mem.sync(session)
    # Even if the dashboard's own rule were bypassed, the ledger checks the two people differ.
    session.audit_log.append(review.AuditEvent(review.utc_now(), A, "confirmed", target.finding_id,
                                               {"decision": "approved"}))
    with pytest.raises(memory.StaleReview, match="different person"):
        mem.sync(session)


def test_documents_are_versioned_and_uploads_are_kept(tmp_path, mem):
    from .conftest import make_pdf

    backend = SimulatedBackend(delay=0)
    first_pdf = make_pdf(["1 Purpose\nThis policy protects data.\n2 Passwords\nPasswords must be long."])
    doc = backend.parse_upload("Policy.pdf", first_pdf)
    info = mem.register(doc, first_pdf, A)
    assert (info["version"], info["already_seen"], info["is_new_version"]) == (1, False, False)
    assert mem.stored_file(info["sha256"]).read_bytes() == first_pdf  # the evidence itself is kept

    assert mem.register(doc, first_pdf, A)["already_seen"]
    second_pdf = make_pdf(["1 Purpose\nThis policy protects data.\n2 Passwords\nPasswords must be 14 characters."])
    info2 = mem.register(backend.parse_upload("Policy.pdf", second_pdf), second_pdf, A)
    assert (info2["version"], info2["is_new_version"], info2["previous_version"]) == (2, True, 1)
    assert [row["version"] for row in mem.library()] == [2]


def test_unchanged_sections_remember_earlier_decisions(sim_backend, mem):
    session = analyze(sim_backend, mem, two_person=False)
    for finding in session.findings:
        review.reject(session, finding.finding_id, A, "Checked by hand") if finding.problems else \
            review.approve(session, finding.finding_id, A)
    mem.sync(session)

    document = sim_backend.load_policy(sim_backend.list_policies()[0])
    passwords = next(i for i, c in enumerate(document.chunks) if c["locator"] == "Section 3.4 Passwords")
    changed = [dict(c) for c in document.chunks]
    changed[passwords] = dict(changed[passwords], chunk_id="policy-changed",
                              text=changed[passwords]["text"].replace("14 characters", "16 characters"))
    from phase3_dashboard.backends.base import PolicyDocument
    version2 = PolicyDocument(source=document.source, chunks=changed, origin="sample")
    info = mem.register(version2, None, A)
    assert info["is_new_version"] and info["previous_summary"]["label"].startswith("Ready to sign off")
    new_session = analysis.analyze_policy(sim_backend, version2, [c["chunk_id"] for c in changed])
    mem.record_analysis(new_session, info, A)
    prior = mem.prior_decisions(new_session)
    assert prior and all(item["state"] == "pending" or item["first_approver"] == A
                         for items in prior.values() for item in items)
    assert "policy-changed" not in prior  # changed text carries nothing over
    changes = mem.compare(document.source, 1, 2)
    assert changes["changed"] == ["Section 3.4 Passwords"] and len(changes["unchanged"]) > 5


def test_the_trail_shows_tampering(sim_backend, mem):
    session = analyze(sim_backend, mem)
    review.approve(session, session.findings[0].finding_id, A)
    mem.sync(session)
    assert mem.verify()["ok"]
    with sqlite3.connect(mem.path) as db:
        db.execute("UPDATE events SET actor = 'Someone Else' WHERE action = 'approved_first'")
    assert not mem.verify()["ok"]


def test_simulator_records_are_kept_apart(monkeypatch, tmp_path):
    monkeypatch.setenv("LEDGER_PATH", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.delenv("COPILOT_DOCUMENT_DIR")
    live, live_docs = memory.default_paths(simulated=False)
    sim, sim_docs = memory.default_paths(simulated=True)
    assert live.name == "ledger.sqlite3" and sim.name == "ledger-simulator.sqlite3"
    assert live_docs.name == "documents" and sim_docs.name == "documents-simulator"
    monkeypatch.setenv("LEDGER_PATH", "data/ledger.sqlite3")  # relative: to the repo, not the start folder
    assert memory.default_paths(simulated=False)[0] == memory.REPO_ROOT / "data" / "ledger.sqlite3"


def test_statements_leave_out_rejected_findings(sim_backend, mem):
    session = analyze(sim_backend, mem)
    rejected = session.findings[0]
    review.reject(session, rejected.finding_id, A, "Wrong control")
    mem.sync(session)
    statements = mem.statements()
    assert statements and rejected.finding_id not in {s["finding_id"] for s in statements}
    assert all(s["control"] for s in statements)


def test_a_self_confirmed_rejection_is_refused_by_the_ledger(sim_backend, mem):
    session = analyze(sim_backend, mem)
    target = session.findings[0]
    review.reject(session, target.finding_id, A, "Out of scope here")
    mem.sync(session)
    session.audit_log.append(review.AuditEvent(review.utc_now(), "mahsa", "confirmed", target.finding_id,
                                               {"decision": "rejected"}))
    with pytest.raises(memory.StaleReview, match="different person"):
        mem.sync(session)
    assert mem.restore(session.ledger_run_id).get(target.finding_id).confirmed_by is None


def test_a_sign_off_over_an_unseen_change_is_refused(sim_backend, mem):
    session = analyze(sim_backend, mem, two_person=False)
    for finding in session.findings:
        review.reject(session, finding.finding_id, A, "Checked by hand") if finding.problems else \
            review.approve(session, finding.finding_id, A)
    mem.sync(session)
    other = mem.restore(session.ledger_run_id)
    review.reopen(other, other.findings[-1].finding_id, B)  # lands after the first page's last read
    mem.sync(other)
    assert analysis.finalize_review(sim_backend, session, A) == []  # locally it looks finished...
    with pytest.raises(memory.StaleReview):
        mem.sync(session)                                       # ...but the ledger refuses the sign-off
    review.undo_unsaved_sign_off(session)
    assert not session.finalized and session.audit_log[-1].action != "finalized"
    assert not mem.restore(session.ledger_run_id).finalized


def test_confirmations_keep_their_time_and_details(sim_backend, mem):
    session = analyze(sim_backend, mem)
    target = session.findings[0]
    review.approve(session, target.finding_id, A)
    mem.sync(session)
    other = mem.restore(session.ledger_run_id)
    review.confirm(other, target.finding_id, B, "Agreed")
    confirmed_at = other.get(target.finding_id).confirmed_at
    mem.sync(other)
    again = mem.restore(session.ledger_run_id)
    assert again.get(target.finding_id).confirmed_at == confirmed_at
    event = next(e for e in again.audit_log if e.action == "confirmed")
    assert event.detail["decided_by"] == A and event.detail["note"] == "Agreed"


def test_a_new_version_loaded_but_not_analyzed_keeps_the_last_review_visible(sim_backend, mem):
    session = analyze(sim_backend, mem)
    document = sim_backend.load_policy(sim_backend.list_policies()[0])
    looked_up = mem.lookup(document, None)
    assert looked_up["already_seen"] and looked_up["runs"]          # lookup records nothing
    from phase3_dashboard.backends.base import PolicyDocument
    changed = PolicyDocument(document.source, [dict(c, text=c["text"] + " Updated.") for c in document.chunks],
                             "sample")
    preview = mem.lookup(changed, None)
    assert (preview["version"], preview["is_new_version"]) == (2, True)
    assert [d["version"] for d in mem.ledger.documents()] == [1]
    mem.register(changed, None, A)                                 # e.g. an approved draft not checked yet
    row = mem.library()[0]
    assert row["latest"]["run_id"] == session.ledger_run_id and "v2 not analyzed" in row["latest"]["label"]
    assert mem.statements()                                        # its findings still count for overlaps
