"""Overlaps and conflicts between an organization's policies."""

import pytest

from phase3_dashboard.core import analysis, memory, overlaps, review
from policy_writer.catalog import starter_controls


@pytest.mark.parametrize("text, expected", [
    ("Workstations must lock automatically after 15 minutes of inactivity.", {"time": {15.0}}),
    ("Report incidents within 1 hour.", {"time": {60.0}}),
    ("Passwords must be at least eight characters.", {"length": {8.0}}),
    ("Lock the account after 10 failed sign-in attempts.", {"attempts": {10.0}}),
    ("Accounts are reviewed quarterly.", {"time": {129600.0}}),
    ("Backups run every day.", {"time": {1440.0}}),
    ("Keep a one-page plan.", {}),
], ids=["minutes", "hour", "words", "attempts", "quarterly", "every-day", "none"])
def test_values_are_read_with_their_units(text, expected):
    assert {k: set(v) for k, v in overlaps.quantities(text).items()} == expected


def test_conflicts_compare_like_with_like():
    assert overlaps.value_conflict("lock after 15 minutes", "lock after 5 minutes") == \
        ("time", ["15 minutes"], ["5 minutes"])
    assert overlaps.value_conflict("report within 1 hour", "report within 60 minutes") is None  # same value
    assert overlaps.value_conflict("at least 14 characters", "lock after 15 minutes") is None  # different things
    assert overlaps.value_conflict("15 minutes", "15 minutes, logs reviewed daily") is None  # one says more
    assert overlaps.value_conflict("within 30 days of hire and annually", "within 14 days and every year") == \
        ("time", ["30 days"], ["14 days"])


def statement(policy, control, coverage, text, locator="Section 1", finding="F-001", run=1):
    return {"policy": policy, "control": control, "coverage": coverage, "requirement_text": text,
            "locator": locator, "finding_id": finding, "run_id": run}


def test_relations_are_ranked_conflict_first():
    focus = statement("Computer Security Policy", "IR-6", "Partial", "Report incidents promptly.")
    others = [
        statement("Remote Work Policy", "IR-6", "Full", "Report lost laptops within 1 hour.", run=2),
        statement("Email Policy", "IR-6", "Partial", "Report phishing within 4 hours.", run=3),
        statement("Email Policy", "AC-2", "Full", "Unrelated control.", run=3),
        statement("Computer Security Policy", "IR-6", "Partial", "Same section restated.", finding="F-009"),
    ]
    # the focus text states no value, so nothing conflicts; the Full statement elsewhere closes the gap
    kinds = [(item["policy"], item["kind"]) for item in overlaps.related(focus, others)]
    assert kinds == [("Remote Work Policy", "covered_elsewhere"), ("Email Policy", "overlap")]

    timed = dict(focus, requirement_text="Report incidents within 24 hours.")
    first = overlaps.related(timed, others)[0]
    assert first["kind"] == "conflict" and "24 hours here" in first["detail"]


def test_the_sample_policies_overlap_like_real_ones(sim_backend, tmp_path):
    """Analyzing two sample policies finds the 15- vs 5-minute screen lock and the incident deadline."""
    mem = memory.Memory(tmp_path / "l.sqlite3", tmp_path / "docs")
    for name in ("SAMPLE_Computer_Security_Policy.pdf", "SAMPLE_Remote_Work_Policy_injection_demo.pdf"):
        document = sim_backend.load_policy(name)
        info = mem.register(document, None, "Mahsa")
        session = analysis.analyze_policy(sim_backend, document, [c["chunk_id"] for c in document.chunks])
        mem.record_analysis(session, info, "Mahsa")
    rows = {row["control"]: row for row in overlaps.coverage_map(mem.statements())}
    lock = rows["AC-11"]
    assert lock["overlap"] and lock["conflicts"]
    assert sorted(lock["conflicts"][0]["values"][1] + lock["conflicts"][0]["values"][2]) == ["15 minutes", "5 minutes"]
    incidents = rows["IR-6"]
    assert set(incidents["policies"].values()) == {"Partial", "Full"} and not incidents["conflicts"]
    totals = overlaps.summary(list(rows.values()))
    assert totals["conflicts"] == 1 and totals["overlaps"] >= 3

    missing = overlaps.unaddressed(list(rows.values()), starter_controls())
    assert "IR-8" in missing and "AC-11" not in missing and "CP-9" in missing


def test_rejected_findings_are_not_compared(sim_backend, tmp_path):
    mem = memory.Memory(tmp_path / "l.sqlite3", tmp_path / "docs")
    document = sim_backend.load_policy("SAMPLE_Remote_Work_Policy_injection_demo.pdf")
    info = mem.register(document, None, "Mahsa")
    session = analysis.analyze_policy(sim_backend, document, [c["chunk_id"] for c in document.chunks])
    mem.record_analysis(session, info, "Mahsa")
    lock = next(f for f in session.findings if f.framework_control == "AC-11")
    review.reject(session, lock.finding_id, "Mahsa", "The 5 minutes was a typo")
    mem.sync(session)
    assert "AC-11" not in {s["control"] for s in mem.statements()}
