"""
Tests for the benchmark script (phase2_agents/benchmark.py). No AI calls and no real
index: the three agents' model calls are replaced with canned replies that report token
usage, and the placeholder search stands in for the library. These prove the script
measures, counts and reports correctly; the real numbers come from running it in Cloud Shell.
"""
import json

import pytest

import auditor
import benchmark
import extractor
import mapper


class _Usage:
    def __init__(self, tin, tout):
        self.input_tokens, self.output_tokens = tin, tout


class _Block:
    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text, tin, tout):
        self.content, self.usage = [_Block(text)], _Usage(tin, tout)


AUDIT = json.dumps({"coverage": "Partial", "finding": "f", "recommendation": "r", "plain_language": "p", "clarifying_questions": []})


@pytest.fixture
def fake_agents(monkeypatch):
    """Canned replies with token usage: Extractor 100/20, Mapper 100/20, Auditor 300/80."""
    calls = {"n": 0}

    def reply(text, tin, tout):
        def create(**kwargs):
            calls["n"] += 1
            return _Resp(text, tin, tout)
        return create

    monkeypatch.setattr(extractor.client.messages, "create",
                        reply(json.dumps([{"requirement": "MFA", "requirement_text": "use multifactor authentication"}]), 100, 20))
    monkeypatch.setattr(mapper.client.messages, "create", reply(json.dumps({"control_id": "IA-2", "reasoning": "auth"}), 100, 20))
    monkeypatch.setattr(auditor.client.messages, "create", reply(AUDIT, 300, 80))
    return calls


def _chunk(i):
    return {"chunk_id": f"c{i}", "text": "t", "source": f"P{i // 3}.pdf", "type": "internal", "page": 1, "locator": f"Section {i}"}


def _result(section, controls, seconds=1.0, errors=()):
    findings = [{"requirement": "r", "coverage": "Partial", "framework_control": c,
                 "citation": {"chunk_id": "x", "source": "s", "locator": "l"}} for c in controls]
    return {"section": section, "chunk_id": "x", "seconds": seconds, "model_seconds": seconds / 2, "model_calls": 3,
            "tokens_in": 500, "tokens_out": 120, "findings": findings, "errors": list(errors)}


# ---- choosing sections ----

def test_sections_are_spread_evenly_and_repeatable():
    chunks = [_chunk(i) for i in range(10)]
    first = benchmark.pick_sections(chunks, 4)
    assert [c["locator"] for c in first] == ["Section 0", "Section 3", "Section 6", "Section 9"]
    assert first == benchmark.pick_sections(list(reversed(chunks)), 4)       # input order does not matter


def test_asking_for_more_sections_than_exist_returns_all_and_one_returns_the_middle():
    chunks = [_chunk(i) for i in range(5)]
    assert len(benchmark.pick_sections(chunks, 50)) == 5
    assert [c["locator"] for c in benchmark.pick_sections(chunks, 1)] == ["Section 2"]


# ---- measuring ----

def test_tokens_calls_and_time_are_recorded_per_section_and_the_wrapper_is_removed(fake_agents):
    before = extractor.client.messages.create
    runs = benchmark.run_benchmark([_chunk(0), _chunk(1)])
    assert len(runs) == 1 and len(runs[0]) == 2
    section = runs[0][0]
    assert (section["tokens_in"], section["tokens_out"], section["model_calls"]) == (500, 120, 3)
    assert len(section["findings"]) == 1 and section["errors"] == []
    assert 0 <= section["model_seconds"] <= section["seconds"]
    assert extractor.client.messages.create is before                     # restored afterwards


def test_a_failing_section_is_recorded_and_the_rest_still_run(fake_agents, monkeypatch):
    def broken(chunk):
        if chunk["chunk_id"] == "c1":
            raise ValueError("bad reply")
        return [{"requirement": "r", "requirement_text": "t", "source": "s", "chunk_id": chunk["chunk_id"], "locator": "l"}]
    monkeypatch.setattr(benchmark.extractor, "extract_requirements", broken)
    runs = benchmark.run_benchmark([_chunk(0), _chunk(1), _chunk(2)])
    errors = [len(r["errors"]) for r in runs[0]]
    assert errors == [0, 1, 0]
    assert runs[0][1]["findings"] == []


def test_repeat_gives_one_result_list_per_run(fake_agents):
    assert len(benchmark.run_benchmark([_chunk(0)], repeat=3)) == 3


# ---- summarising ----

def test_summary_counts_findings_tokens_cost_and_checks_the_citations(fake_agents, tmp_path):
    chunks = benchmark.load_sections()[:2]
    runs = benchmark.run_benchmark(chunks)
    summary = benchmark.summarise(runs, str(tmp_path / "f.json"))
    n = summary["findings"]
    assert n == len(chunks) and summary["errors"] == 0
    assert summary["tokens_in"] == 500 * len(chunks) and summary["tokens_out"] == 120 * len(chunks)
    expected = summary["tokens_in"] / 1e6 * benchmark.PRICE_IN_PER_M + summary["tokens_out"] / 1e6 * benchmark.PRICE_OUT_PER_M
    assert summary["cost_total"] == pytest.approx(expected)
    assert summary["cost_per_finding"] == pytest.approx(expected / n)
    assert summary["integrity"]["citation_resolves"] == n                 # placeholder chunks resolve
    assert summary["agreement"] is None                                    # only one run


def test_other_book_controls_and_unmapped_requirements_are_counted(tmp_path):
    runs = [[_result("A | s1", ["AC-2", "ID.RA-07", None, "IA-5(1)"])]]
    summary = benchmark.summarise(runs, str(tmp_path / "f.json"))
    assert summary["other_book_controls"] == 1                            # ID.RA-07 is a CSF outcome
    assert summary["unmapped"] == 1
    assert summary["coverage"] == {"Partial": 4}


def test_price_assumptions_can_be_changed(monkeypatch, tmp_path):
    monkeypatch.setattr(benchmark, "PRICE_IN_PER_M", 10.0)
    monkeypatch.setattr(benchmark, "PRICE_OUT_PER_M", 20.0)
    summary = benchmark.summarise([[_result("A | s1", ["AC-2"])]], str(tmp_path / "f.json"))
    assert summary["cost_total"] == pytest.approx(500 / 1e6 * 10 + 120 / 1e6 * 20)


def test_agreement_is_the_overlap_of_controls_per_section():
    run_a = [_result("S1", ["AC-2", "IA-2"]), _result("S2", ["CM-6"]), _result("S3", [])]
    run_b = [_result("S1", ["AC-2"]), _result("S2", ["CM-6"]), _result("S3", [])]
    mean, identical, total = benchmark.agreement(run_a, run_b)
    assert (total, identical) == (2, 1)                                   # S3 is empty in both runs and is skipped
    assert mean == pytest.approx((0.5 + 1.0) / 2)


def test_percentile_handles_small_and_empty_lists():
    assert benchmark.percentile([], 0.9) is None
    assert benchmark.percentile([5.0], 0.9) == 5.0
    assert benchmark.percentile([1.0, 2.0, 3.0, 4.0, 10.0], 0.5) == 3.0


# ---- the report ----

def test_report_has_the_rows_and_says_accuracy_is_not_measured(tmp_path):
    runs = [[_result("A | s1", ["AC-2"], 12.0), _result("A | s2", ["IA-2"], 30.0)]]
    summary = benchmark.summarise(runs, str(tmp_path / "f.json"))
    text = benchmark.render_markdown(runs, summary, {"backend": "test", "retrieval": "real", "when": "now"})
    for label in ("Average time per section", "Average time per finding", "Tokens in / out", "Estimated cost per finding",
                  "Cited controls that exist", "Controls from a different NIST book", "| Section | Findings | Seconds |"):
        assert label in text
    assert "Not measured yet" in text and "No AI judge" in text
    assert "A \\| s2" in text and "30.0" in text                          # the pipe is escaped so the table keeps its columns
    for line in text.splitlines():
        if line.startswith("| A "):
            assert line.replace("\\|", "").count("|") == 4                  # section, findings, seconds: three cells


def test_report_still_renders_when_nothing_was_found(tmp_path):
    runs = [[_result("A | s1", [])]]
    summary = benchmark.summarise(runs, str(tmp_path / "f.json"))
    text = benchmark.render_markdown(runs, summary, {"backend": "t", "retrieval": "r", "when": "w"})
    assert "n/a" in text and "Findings produced | 0" in text


def test_report_shows_agreement_only_when_run_twice(tmp_path):
    one = [[_result("S1", ["AC-2"])]]
    two = one + [[_result("S1", ["AC-2"])]]
    meta = {"backend": "t", "retrieval": "r", "when": "w"}
    assert "Run-to-run agreement" not in benchmark.render_markdown(one, benchmark.summarise(one, str(tmp_path / "a.json")), meta)
    assert "Run-to-run agreement" in benchmark.render_markdown(two, benchmark.summarise(two, str(tmp_path / "b.json")), meta)


# ---- the command line ----

def test_it_refuses_to_run_on_the_placeholder_search():
    with pytest.raises(SystemExit) as exc:
        benchmark.main([])
    assert "real search index" in str(exc.value)


def test_dry_run_lists_sections_and_makes_no_ai_calls(fake_agents, capsys):
    benchmark.main(["--dry-run", "--allow-placeholder", "--sections", "2"])
    out = capsys.readouterr().out
    assert "Sections (2)" in out and fake_agents["n"] == 0


def test_a_full_run_writes_the_report_files(fake_agents, tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "OUT_DIR", str(tmp_path))
    benchmark.main(["--allow-placeholder", "--sections", "2"])
    assert "## Benchmarks" in (tmp_path / "BENCHMARKS.md").read_text(encoding="utf-8")
    saved = json.loads((tmp_path / "benchmark_results.json").read_text(encoding="utf-8"))
    assert saved["summary"]["findings"] >= 1 and (tmp_path / "benchmark_findings.json").exists()
