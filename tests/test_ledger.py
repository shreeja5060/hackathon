"""
Tests for the ledger (shared/ledger.py): permanent memory, versions, the audit
trail and two-person approval. Uses a throwaway database file per test; no
network, no AI calls.
"""
import sqlite3

import pytest

from ledger import ApprovalError, Ledger

NAME = "Computer_Security_Policy.pdf"


def _finding(fid, section, locator, requirement="Do the thing", control="IA-2"):
    return {"finding_id": fid, "requirement": requirement, "coverage": "Partial", "framework_control": control,
            "citation": {"chunk_id": section, "locator": locator}}


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.sqlite3")


@pytest.fixture
def run(ledger):
    doc = ledger.register_document(NAME, "v1 text")
    return ledger.record_run(doc["id"], [_finding("F001", "s-a", "Section 3.5"), _finding("F002", "s-b", "Section 3.2")])


# ---- documents and versions ----

def test_same_content_is_recognised_and_new_content_becomes_the_next_version(ledger):
    first = ledger.register_document(NAME, "text one")
    assert (first["version"], first["is_new_version"], first["already_seen"]) == (1, False, False)
    again = ledger.register_document(NAME, "text one")
    assert (again["version"], again["already_seen"], again["id"]) == (1, True, first["id"])
    second = ledger.register_document(NAME, "text two")
    assert (second["version"], second["is_new_version"], second["previous_version"]) == (2, True, 1)


def test_other_documents_have_their_own_version_numbers(ledger):
    ledger.register_document(NAME, "a")
    ledger.register_document(NAME, "b")
    assert ledger.register_document("Other_Policy.pdf", "a")["version"] == 1


# ---- memory survives a restart ----

def test_a_new_session_sees_what_an_earlier_one_recorded(tmp_path):
    path = tmp_path / "l.sqlite3"
    first = Ledger(path)
    doc = first.register_document(NAME, "text")
    run_id = first.record_run(doc["id"], [_finding("F001", "s-a", "Section 3.5")])
    first.approve_first("Rashmi", run_id, "F001")
    later = Ledger(path)                                   # as after an app restart
    assert later.register_document(NAME, "text")["already_seen"] is True
    assert later.finding_state(run_id, "F001")["state"] == "awaiting_second_approval"
    assert [e["actor"] for e in later.events(document_name=NAME)] == ["system", "Rashmi"]


# ---- the audit trail ----

def test_events_record_who_what_and_when(ledger, run):
    ledger.record_event("Mahsa", "viewed", run_id=run, finding_id="F001", detail={"page": "review"})
    e = ledger.events(run_id=run, finding_id="F001")[0]
    assert (e["actor"], e["action"], e["document"], e["version"]) == ("Mahsa", "viewed", NAME, 1)
    assert e["ts"] and e["detail"] == {"page": "review"}


def test_an_event_without_an_actor_is_refused(ledger, run):
    with pytest.raises(ValueError):
        ledger.record_event("  ", "viewed", run_id=run)


def test_the_trail_verifies_until_someone_edits_it(ledger, run):
    ledger.approve_first("Rashmi", run, "F001")
    assert ledger.verify_chain() == {"ok": True, "first_bad_seq": None, "events": 2}
    with sqlite3.connect(ledger.path) as db:
        db.execute("UPDATE events SET actor = 'Someone Else' WHERE seq = 2")
    result = ledger.verify_chain()
    assert result["ok"] is False and result["first_bad_seq"] == 2


def test_deleting_an_entry_is_detected(ledger, run):
    ledger.approve_first("Rashmi", run, "F001")
    ledger.reject("Mahsa", run, "F002", "wrong control")
    with sqlite3.connect(ledger.path) as db:
        db.execute("DELETE FROM events WHERE seq = 2")
    assert ledger.verify_chain()["ok"] is False


# ---- two-person approval ----

def test_a_finding_needs_two_different_people(ledger, run):
    assert ledger.finding_state(run, "F001")["state"] == "pending"
    ledger.approve_first("Rashmi", run, "F001")
    assert ledger.finding_state(run, "F001")["state"] == "awaiting_second_approval"
    with pytest.raises(ApprovalError, match="different person"):
        ledger.approve_final("rashmi ", run, "F001")       # same person, even with different spelling
    ledger.approve_final("Anu", run, "F001")
    state = ledger.finding_state(run, "F001")
    assert (state["state"], state["first_approver"], state["final_approver"]) == ("approved", "Rashmi", "Anu")


def test_a_second_approval_needs_a_first_one(ledger, run):
    with pytest.raises(ApprovalError, match="first approval"):
        ledger.approve_final("Anu", run, "F001")


def test_rejection_needs_a_note_and_overrides_earlier_approval(ledger, run):
    ledger.approve_first("Rashmi", run, "F001")
    with pytest.raises(ApprovalError, match="note"):
        ledger.reject("Mahsa", run, "F001", " ")
    ledger.reject("Mahsa", run, "F001", "does not fit")
    state = ledger.finding_state(run, "F001")
    assert (state["state"], state["rejected_by"]) == ("rejected", "Mahsa")


def test_reopening_returns_a_decision_to_pending(ledger, run):
    ledger.approve_first("Rashmi", run, "F001")
    ledger.record_event("Anu", "reopened", run, "F001", "approver", {"note": "wording too vague"})
    state = ledger.finding_state(run, "F001")
    assert (state["state"], state["first_approver"]) == ("pending", None)
    with pytest.raises(ApprovalError, match="first approval"):
        ledger.approve_final("Anu", run, "F001")             # nothing to confirm any more
    ledger.approve_first("Mahsa", run, "F001")
    ledger.approve_final("Anu", run, "F001")
    assert ledger.finding_state(run, "F001")["state"] == "approved"


def test_a_refused_approval_leaves_no_trace_in_the_trail(ledger, run):
    before = len(ledger.events(run_id=run))
    with pytest.raises(ApprovalError):
        ledger.approve_final("Anu", run, "F001")
    assert len(ledger.events(run_id=run)) == before


def test_unknown_findings_are_refused(ledger, run):
    with pytest.raises(ApprovalError, match="unknown finding"):
        ledger.approve_first("Rashmi", run, "F999")


# ---- versions and memory ----

def _two_versions(ledger):
    v1 = ledger.register_document(NAME, "first")
    run1 = ledger.record_run(v1["id"], [_finding("F001", "a1", "Section 3.5"), _finding("F002", "b1", "Section 3.2"),
                                        _finding("F003", "c1", "Section 4")])
    v2 = ledger.register_document(NAME, "second")
    run2 = ledger.record_run(v2["id"], [_finding("F001", "a2", "Section 3.5"), _finding("F002", "b1", "Section 3.2"),
                                        _finding("F003", "d1", "Section 5")])
    return run1, run2


def test_comparing_versions_sorts_sections_into_four_groups(ledger):
    _two_versions(ledger)
    assert ledger.compare_versions(NAME, 1, 2) == {
        "unchanged": ["Section 3.2"], "changed": ["Section 3.5"], "added": ["Section 5"], "removed": ["Section 4"]}


def test_earlier_decisions_are_remembered_for_unchanged_sections_only(ledger):
    run1, run2 = _two_versions(ledger)
    ledger.approve_first("Rashmi", run1, "F002")
    ledger.approve_final("Anu", run1, "F002")
    ledger.approve_first("Rashmi", run1, "F001")
    remembered = ledger.prior_decisions(NAME, ["b1", "a2"], before_run_id=run2)
    assert list(remembered) == ["b1"]                      # a2 is new text, so nothing carries over
    assert remembered["b1"][0]["state"] == "approved" and remembered["b1"][0]["final_approver"] == "Anu"


def test_comparing_needs_both_versions_analysed(ledger):
    ledger.register_document(NAME, "only one")
    with pytest.raises(ValueError):
        ledger.compare_versions(NAME, 1, 2)


# ---- history and the readable note ----

def test_history_can_be_filtered_by_document(ledger, run):
    other = ledger.register_document("Other.pdf", "x")
    ledger.record_run(other["id"], [_finding("F001", "o1", "Section 1")])
    assert {e["document"] for e in ledger.events(document_name=NAME)} == {NAME}
    assert len(ledger.events()) == 2


def test_the_memory_note_summarises_versions_counts_and_open_items(ledger, run, tmp_path):
    ledger.approve_first("Rashmi", run, "F001")
    note = ledger.memory_note(NAME)
    assert "v1" in note and "2 findings" in note and "1 awaiting_second_approval" in note and "1 pending" in note
    assert "## Still open" in note and "F002" in note
    path = ledger.write_memory_note(NAME, tmp_path / "notes")
    assert path.read_text(encoding="utf-8") == note


def test_the_memory_note_for_an_unknown_document_says_so(ledger):
    assert "Nothing recorded" in ledger.memory_note("nope.pdf")
