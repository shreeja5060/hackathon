"""
Tests for the Mapper's shortlist and for what happens when Claude picks a
control that was not on it. Regression for what a live run showed:
  - CSF 2.0 outcomes (ID.RA-07 ...) appeared as "NIST 800-53 controls"
  - the same control (SI-3) took several shortlist slots
  - a pick that was not on the shortlist ended up as "Unknown control text"
No API calls: Claude and the index are stubbed.
"""
import json
import pytest

import mapper
import auditor

REQ = {"requirement": "r", "requirement_text": "Report incidents promptly.",
       "source": "policy.pdf", "chunk_id": "policy-1", "locator": "Section 4"}


def _hit(locator, chunk_id=None, source="NIST_SP-800-53_rev5_catalog.json", text=None):
    return {"chunk_id": chunk_id or f"fw-{locator}", "text": text or f"{locator}: text of {locator}",
            "source": source, "type": "framework", "page": None, "locator": locator}


@pytest.fixture
def results(monkeypatch):
    """Set what the (stubbed) index returns for the shortlist search."""
    def use(hits):
        monkeypatch.setattr(mapper, "search", lambda q, type=None, top_k=5: hits[:top_k])
    return use


def _prompt(fake_claude):
    return fake_claude.sent[-1]["messages"][0]["content"]


def test_shortlist_excludes_other_frameworks_and_repeats(results):
    results([_hit("IA-2"), _hit("ID.RA-07", source="NIST_CSF_v2.0_catalog.json"),
             _hit("SI-3", chunk_id="a"), _hit("SI-3", chunk_id="b"), _hit("SI-3", chunk_id="c"),
             _hit("AC-2")])
    got = [h["locator"] for h in mapper.shortlist("x")]
    assert got == ["IA-2", "SI-3", "AC-2"]                  # no CSF, SI-3 once, order kept
    assert [h["chunk_id"] for h in mapper.shortlist("x") if h["locator"] == "SI-3"] == ["a"]


def test_shortlist_is_capped_at_15(results):
    results([_hit(f"AC-{i}") for i in range(1, 41)])
    assert len(mapper.shortlist("x")) == 15


def test_prompt_shows_only_800_53_controls(fake_claude, results):
    results([_hit("IA-2"), _hit("ID.RA-07", source="NIST_CSF_v2.0_catalog.json"), _hit("AC-2")])
    fake_claude(mapper, json.dumps({"control_id": "IA-2", "reasoning": "r"}))
    mapper.map_requirement(dict(REQ))
    assert "ID.RA-07" not in _prompt(fake_claude)
    assert "IA-2" in _prompt(fake_claude) and "AC-2" in _prompt(fake_claude)


def test_pick_on_the_shortlist_keeps_its_chunk_id(fake_claude, results):
    results([_hit("IA-2", chunk_id="fw-ia2"), _hit("AC-2")])
    fake_claude(mapper, json.dumps({"control_id": "IA-2", "reasoning": "authentication"}))
    out = mapper.map_requirement(dict(REQ))
    assert out["mapped_control"] == "IA-2"
    assert out["mapped_control_chunk_id"] == "fw-ia2"
    assert out["mapping_reasoning"] == "authentication"      # no note added


def test_pick_off_the_shortlist_is_kept_when_the_index_confirms_it(fake_claude, results, monkeypatch):
    results([_hit("IA-2"), _hit("AC-2")])
    monkeypatch.setattr(mapper, "_lookup_control", lambda cid: {"chunk_id": "fw-ir6"} if cid == "IR-6" else None)
    fake_claude(mapper, json.dumps({"control_id": "IR-6", "reasoning": "incident reporting"}))
    out = mapper.map_requirement(dict(REQ))
    assert out["mapped_control"] == "IR-6"
    assert out["mapped_control_chunk_id"] == "fw-ir6"
    assert "confirmed in the index" in out["mapping_reasoning"]


def test_pick_that_cannot_be_verified_is_left_unmapped(fake_claude, results, monkeypatch):
    results([_hit("IA-2")])
    monkeypatch.setattr(mapper, "_lookup_control", lambda cid: None)
    fake_claude(mapper, json.dumps({"control_id": "ZZ-99", "reasoning": "invented"}))
    out = mapper.map_requirement(dict(REQ))
    assert out["mapped_control"] is None
    assert out["mapped_control_chunk_id"] is None
    assert "ZZ-99" in out["mapping_reasoning"]


def test_csf_outcome_is_not_accepted_as_an_800_53_control(fake_claude, results, monkeypatch):
    results([_hit("IA-2")])
    monkeypatch.setattr(mapper, "_lookup_control", lambda cid: None)   # the index lookup excludes CSF
    fake_claude(mapper, json.dumps({"control_id": "ID.RA-07", "reasoning": "risk"}))
    assert mapper.map_requirement(dict(REQ))["mapped_control"] is None


def test_null_pick_means_no_mapping_and_no_lookup(fake_claude, results, monkeypatch):
    results([_hit("IA-2")])
    monkeypatch.setattr(mapper, "_lookup_control", lambda cid: (_ for _ in ()).throw(AssertionError("no lookup for null")))
    fake_claude(mapper, json.dumps({"control_id": None, "reasoning": "none fit"}))
    out = mapper.map_requirement(dict(REQ))
    assert out["mapped_control"] is None and out["mapped_control_chunk_id"] is None


@pytest.mark.parametrize("reply", [json.dumps({"coverage": "Partial"}), json.dumps(["IA-2"]), '"IA-2"'])
def test_reply_without_a_control_id_is_an_error_not_a_silent_no_match(fake_claude, results, reply):
    results([_hit("IA-2")])
    fake_claude(mapper, reply)
    with pytest.raises(ValueError):
        mapper.map_requirement(dict(REQ))


def test_auditor_does_not_judge_against_missing_control_text(monkeypatch):
    def boom(**kw):
        raise AssertionError("Claude must not be asked to judge against nothing")
    monkeypatch.setattr(auditor.client.messages, "create", boom)
    monkeypatch.setattr(auditor, "get_original_chunk", lambda cid: None)
    out = auditor.audit_requirement({
        "requirement": "r", "requirement_text": "t", "mapped_control": "IR-6",
        "mapped_control_chunk_id": "gone", "chunk_id": "c", "source": "s", "locator": "l"})
    assert out["coverage"] == "Not observable"
    assert out["framework_control"] == "IR-6"
    assert "Unknown control text" not in json.dumps(out)
    assert {"plain_language", "clarifying_questions", "citation", "status"} <= set(out)
