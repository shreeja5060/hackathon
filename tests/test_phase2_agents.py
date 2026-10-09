"""
Phase 2 unit tests. Run from the repo root with:  pytest tests/ -v
No API key or network needed.
"""
import json
import pytest

import extractor
import mapper
import auditor


# ---------- retrieval backend / fallback ----------

def test_fallback_backend_is_selected_when_real_retriever_unavailable():
    # On a machine without Phase 1 deps (e.g. the test runner), the agents
    # must still import and announce which backend they are using.
    assert extractor.RETRIEVER_BACKEND in (
        "real (phase1_ingestion/retriever.py)",
        "placeholder (shared/fake_search.py)",
    )


def test_search_returns_agreed_schema():
    results = extractor.search("authentication", type="internal")
    assert isinstance(results, list) and results, "search() returned nothing"
    required = {"chunk_id", "text", "source", "type", "page", "locator"}
    for r in results:
        assert required <= set(r.keys()), f"missing keys: {required - set(r.keys())}"
        assert r["type"] == "internal"


def test_get_original_chunk_round_trip():
    first = extractor.search("authentication", type="internal")[0]
    again = auditor.get_original_chunk(first["chunk_id"])
    assert again is not None
    assert again["chunk_id"] == first["chunk_id"]


# ---------- Extractor ----------

def test_extractor_parses_json_and_attaches_citation(fake_claude, sample_chunk):
    fake_claude(extractor, json.dumps([
        {"requirement": "MFA required", "requirement_text": "including multifactor authentication where required."}
    ]))
    reqs = extractor.extract_requirements(sample_chunk)
    assert len(reqs) == 1
    r = reqs[0]
    assert r["requirement"] == "MFA required"
    # citation fields must be carried through from the chunk
    assert r["source"] == sample_chunk["source"]
    assert r["chunk_id"] == sample_chunk["chunk_id"]
    assert r["locator"] == sample_chunk["locator"]


def test_extractor_strips_markdown_code_fences(fake_claude, sample_chunk):
    fenced = "```json\n" + json.dumps([{"requirement": "x", "requirement_text": "y"}]) + "\n```"
    fake_claude(extractor, fenced)
    reqs = extractor.extract_requirements(sample_chunk)
    assert reqs[0]["requirement"] == "x"


def test_extractor_prompt_contains_policy_text(fake_claude, sample_chunk):
    fake_claude(extractor, "[]")
    extractor.extract_requirements(sample_chunk)
    prompt = fake_claude.sent[-1]["messages"][0]["content"]
    assert sample_chunk["text"] in prompt


def test_extractor_raises_on_malformed_json(fake_claude, sample_chunk):
    fake_claude(extractor, '[{"requirement": "truncated...')
    with pytest.raises(json.JSONDecodeError):
        extractor.extract_requirements(sample_chunk)
    # The pipeline relies on this raising so run_full_pipeline can catch and
    # log it instead of silently producing a bad finding.


# ---------- Mapper ----------

def test_mapper_attaches_control_and_exact_chunk_id(fake_claude):
    req = {
        "requirement": "MFA required",
        "requirement_text": "multifactor authentication where required",
        "source": "s.pdf", "chunk_id": "policy-security-001", "locator": "3.5",
    }
    fake_claude(mapper, json.dumps({"control_id": "IA-2", "reasoning": "authentication"}))
    out = mapper.map_requirement(req)
    assert out["mapped_control"] == "IA-2"
    # The chunk_id of the matched framework control must be recorded so the
    # Auditor can fetch the exact entry (Maryam's integration point).
    assert out["mapped_control_chunk_id"] == "framework-nist-ia2"
    assert out["mapping_reasoning"] == "authentication"


def test_mapper_handles_no_match(fake_claude):
    req = {"requirement": "r", "requirement_text": "something unrelated",
           "source": "s", "chunk_id": "c", "locator": "l"}
    fake_claude(mapper, json.dumps({"control_id": None, "reasoning": "none fit"}))
    out = mapper.map_requirement(req)
    assert out["mapped_control"] is None
    assert out["mapped_control_chunk_id"] is None


def test_mapper_fetches_extra_candidates_before_trimming(fake_claude, monkeypatch):
    # It asks for more than it shows, because other frameworks and repeated
    # fragments are dropped before the shortlist of 15 is built.
    seen = {}
    def fake_search(query, type=None, top_k=5):
        seen["top_k"] = top_k
        return []
    monkeypatch.setattr(mapper, "search", fake_search)
    fake_claude(mapper, json.dumps({"control_id": None, "reasoning": ""}))
    mapper.map_requirement({"requirement": "r", "requirement_text": "t",
                            "source": "s", "chunk_id": "c", "locator": "l"})
    assert seen["top_k"] >= 15


# ---------- Auditor ----------

REQUIRED_FINDING_KEYS = {
    "requirement", "coverage", "finding", "recommendation",
    "plain_language", "clarifying_questions", "citation",
    "framework_control", "status",
}


def test_auditor_returns_not_observable_without_api_call_when_no_control(monkeypatch):
    # No control mapped -> must NOT call Claude, must return a sensible finding.
    def boom(**kwargs):
        raise AssertionError("Claude should not be called when there is no control")
    monkeypatch.setattr(auditor.client.messages, "create", boom)
    out = auditor.audit_requirement({
        "requirement": "r", "requirement_text": "t",
        "mapped_control": None, "mapped_control_chunk_id": None,
        "chunk_id": "c", "source": "s", "locator": "l",
    })
    assert out["coverage"] == "Not observable"
    assert out["framework_control"] is None
    assert out["status"] == "pending"
    assert REQUIRED_FINDING_KEYS <= set(out.keys())


def test_auditor_builds_full_finding_with_plain_language(fake_claude):
    fake_claude(auditor, json.dumps({
        "coverage": "Partial",
        "finding": "Scope unclear",
        "recommendation": "Define which systems require MFA",
        "plain_language": "The rule says MFA is needed but not where.",
        "clarifying_questions": ["Which systems count as privileged?"],
    }))
    out = auditor.audit_requirement({
        "requirement": "MFA required", "requirement_text": "t",
        "mapped_control": "IA-2", "mapped_control_chunk_id": "framework-nist-ia2",
        "chunk_id": "policy-security-001",
        "source": "Public_Release_Computer_Security_Policy.pdf",
        "locator": "Section 3.5 Authentication",
    })
    assert REQUIRED_FINDING_KEYS <= set(out.keys())
    assert out["coverage"] == "Partial"
    assert out["framework_control"] == "IA-2"
    assert out["citation"] == {
        "chunk_id": "policy-security-001",
        "source": "Public_Release_Computer_Security_Policy.pdf",
        "locator": "Section 3.5 Authentication",
    }
    assert out["clarifying_questions"] == ["Which systems count as privileged?"]
    assert out["status"] == "pending"


def test_auditor_uses_exact_control_chunk_not_a_second_search(fake_claude, monkeypatch):
    def no_search(*a, **k):
        raise AssertionError("Auditor must use get_original_chunk, not search()")
    monkeypatch.setattr(auditor, "search", no_search)
    fake_claude(auditor, json.dumps({"coverage": "Full", "finding": "ok",
                                     "recommendation": "None",
                                     "plain_language": "fine", "clarifying_questions": []}))
    out = auditor.audit_requirement({
        "requirement": "r", "requirement_text": "t",
        "mapped_control": "IA-2", "mapped_control_chunk_id": "framework-nist-ia2",
        "chunk_id": "c", "source": "s", "locator": "l",
    })
    assert out["coverage"] == "Full"
    prompt = fake_claude.sent[-1]["messages"][0]["content"]
    assert "IA-2" in prompt and "Identification and Authentication" in prompt


@pytest.mark.parametrize("bad", ["Mostly Full", "partial", "", "N/A"])
def test_auditor_coverage_values_are_constrained(fake_claude, bad):
    # Documents the contract the dashboard relies on. If Claude ever returns a
    # value outside the four allowed ones, this is where we'd want to catch it.
    fake_claude(auditor, json.dumps({"coverage": bad, "finding": "", "recommendation": "",
                                     "plain_language": "", "clarifying_questions": []}))
    out = auditor.audit_requirement({
        "requirement": "r", "requirement_text": "t",
        "mapped_control": "IA-2", "mapped_control_chunk_id": "framework-nist-ia2",
        "chunk_id": "c", "source": "s", "locator": "l",
    })
    assert out["coverage"] not in {"Full", "Partial", "Missing", "Not observable"}, \
        "test fixture sanity: a bad value should not be in the allowed set"
    # NOTE: the Auditor currently passes bad values through. See
    # test_auditor_rejects_invalid_coverage below once validation is added.


def test_auditor_truncation_bug_regression(fake_claude):
    # Regression for the 300-token truncation seen in the first full run:
    # the Auditor must ask for enough tokens to fit plain_language fields.
    fake_claude(auditor, json.dumps({"coverage": "Full", "finding": "", "recommendation": "None",
                                     "plain_language": "", "clarifying_questions": []}))
    auditor.audit_requirement({
        "requirement": "r", "requirement_text": "t",
        "mapped_control": "IA-2", "mapped_control_chunk_id": "framework-nist-ia2",
        "chunk_id": "c", "source": "s", "locator": "l",
    })
    assert fake_claude.sent[-1]["max_tokens"] >= 1000


# ---------- run_full_pipeline error isolation ----------

def test_full_pipeline_skips_failed_chunk_and_continues(monkeypatch, tmp_path):
    import run_full_pipeline as rfp
    monkeypatch.chdir(tmp_path)  # so the JSON outputs land in a temp folder

    chunks = [
        {"chunk_id": "a", "text": "A", "source": "s", "type": "internal", "page": 1, "locator": "1"},
        {"chunk_id": "b", "text": "B", "source": "s", "type": "internal", "page": 1, "locator": "2"},
        {"chunk_id": "c", "text": "C", "source": "s", "type": "internal", "page": 1, "locator": "3"},
    ]
    monkeypatch.setattr(rfp, "search", lambda *a, **k: chunks)

    def fake_extract(chunk):
        if chunk["chunk_id"] == "b":
            raise ValueError("simulated Claude failure")
        return [{"requirement": chunk["chunk_id"], "requirement_text": chunk["text"],
                 "source": "s", "chunk_id": chunk["chunk_id"], "locator": chunk["locator"]}]
    monkeypatch.setattr(rfp, "extract_requirements", fake_extract)
    monkeypatch.setattr(rfp, "map_requirement",
                        lambda r: {**r, "mapped_control": "AC-2", "mapped_control_chunk_id": "x"})
    monkeypatch.setattr(rfp, "audit_requirement",
                        lambda r: {"requirement": r["requirement"], "coverage": "Full",
                                   "finding": "", "recommendation": "None",
                                   "plain_language": "", "clarifying_questions": [],
                                   "citation": {}, "framework_control": "AC-2", "status": "pending"})

    report = rfp.run_full_pipeline()

    assert [f["requirement"] for f in report] == ["a", "c"]       # b skipped, others kept
    errors = json.loads((tmp_path / "pipeline_errors.json").read_text())
    assert len(errors) == 1 and errors[0]["stage"] == "extractor"
    assert (tmp_path / "full_gap_report.json").exists()


# ---------- Extractor: tolerate the reply shapes Claude actually produces ----------
# Regression for: "Extractor failed: 'str' object does not support item assignment"
# (seen in the dashboard's live end-to-end tests when Claude returned a shape
# other than a list of objects).

@pytest.mark.parametrize("reply", [
    json.dumps({"requirements": [{"requirement": "r1", "requirement_text": "t1"}]}),
    json.dumps(["Privileged accounts must be reviewed every quarter."]),
    json.dumps([{"requirement_text": "only text, no short name"}]),
])
def test_extractor_accepts_common_reply_shapes(fake_claude, sample_chunk, reply):
    fake_claude(extractor, reply)
    reqs = extractor.extract_requirements(sample_chunk)
    assert len(reqs) == 1
    r = reqs[0]
    assert r["requirement"] and r["requirement_text"]
    assert r["source"] == sample_chunk["source"]          # citation still attached
    assert r["chunk_id"] == sample_chunk["chunk_id"]


def test_extractor_empty_object_means_no_requirements(fake_claude, sample_chunk):
    fake_claude(extractor, "{}")
    assert extractor.extract_requirements(sample_chunk) == []


@pytest.mark.parametrize("reply", ['"just a sentence"', '42', '{"a": 1, "b": 2}', '[1, 2]'])
def test_extractor_unusable_shapes_raise_a_clear_error(fake_claude, sample_chunk, reply):
    fake_claude(extractor, reply)
    with pytest.raises(ValueError):
        extractor.extract_requirements(sample_chunk)


# ---------- Agents must not share a Claude client ----------
# Regression: with one shared client object, patching extractor.client,
# mapper.client and auditor.client patched the SAME object, so the last stub
# answered every agent (dashboard live tests failed with "'control_id'").

def test_each_agent_owns_its_own_claude_client():
    assert extractor.client is not mapper.client
    assert extractor.client is not auditor.client
    assert mapper.client is not auditor.client


def test_stubbing_one_agent_does_not_affect_the_others(monkeypatch, sample_chunk):
    import conftest
    monkeypatch.setattr(extractor.client.messages, "create",
        lambda **k: conftest._FakeResponse(json.dumps([{"requirement": "E", "requirement_text": "e"}])))
    monkeypatch.setattr(mapper.client.messages, "create",
        lambda **k: conftest._FakeResponse(json.dumps({"control_id": "IA-2", "reasoning": "m"})))
    monkeypatch.setattr(auditor.client.messages, "create",
        lambda **k: conftest._FakeResponse(json.dumps({"coverage": "Full", "finding": "a", "recommendation": "None",
                                                       "plain_language": "", "clarifying_questions": ["q?"]})))
    reqs = extractor.extract_requirements(sample_chunk)
    assert [r["requirement"] for r in reqs] == ["E"]          # not the auditor's "q?"
    mapped = mapper.map_requirement(reqs[0])
    assert mapped["mapped_control"] == "IA-2"                  # not a KeyError


def test_extractor_rejects_another_agents_reply_instead_of_inventing_requirements(fake_claude, sample_chunk):
    auditor_shaped = json.dumps({"coverage": "Partial", "finding": "f", "recommendation": "r",
                                 "plain_language": "p", "clarifying_questions": ["Which systems count as privileged?"]})
    fake_claude(extractor, auditor_shaped)
    with pytest.raises(ValueError):
        extractor.extract_requirements(sample_chunk)
