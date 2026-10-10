"""
Benchmark for the three agents on real policy sections.

    python phase2_agents/benchmark.py                  # 6 sections, spread evenly across the policies
    python phase2_agents/benchmark.py --sections 12    # more sections: slower and costs more
    python phase2_agents/benchmark.py --repeat 2       # run twice to measure run-to-run agreement
    python phase2_agents/benchmark.py --dry-run        # list the sections it would use; no AI calls

It writes eval/BENCHMARKS.md (a table you can paste into the README) and
eval/benchmark_results.json (the raw numbers).

What it measures, all objective, with no AI judge:
  speed      seconds per section and per finding, split into AI time and the rest
  cost       tokens in and out, and an estimated cost (the prices are assumptions,
             see PRICE_IN_PER_M / PRICE_OUT_PER_M below)
  integrity  every citation resolves to a real chunk in the stated section; every
             cited control exists in the index; controls from another NIST book;
             requirements with no control
  reliability errors, and (with --repeat 2) how often two runs pick the same controls

What it does NOT measure: accuracy against an expert. That needs the answer key in
eval/gold_set.json to be filled in by a person who knows the controls (see
phase2_agents/evaluate.py). The report says so in its own table.

It must run on the real index and a real model, so run it in Cloud Shell. It refuses
to run on the placeholder search, whose numbers would mean nothing.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(_HERE)
sys.path.append(os.path.join(_HERE, "..", "shared"))
sys.path.append(os.path.join(_HERE, "..", "phase1_ingestion"))

import claude_client
import extractor
import mapper
import auditor
import verify_citations

OUT_DIR = os.path.join(_HERE, "..", "eval")
CONTROL_ID = re.compile(r"^[A-Z]{2}-\d+(\(\d+\))?$")      # an SP 800-53 control id, e.g. AC-2 or IA-5(1)

# Assumed prices in dollars per million tokens (Sonnet-class). They are not read from
# the API. Change them if your model is priced differently.
PRICE_IN_PER_M = float(os.getenv("PRICE_IN_PER_M", "3"))
PRICE_OUT_PER_M = float(os.getenv("PRICE_OUT_PER_M", "15"))


# ---- measuring ---------------------------------------------------------------

class Meter:
    """Wraps each agent's model call to record how long it took and how many tokens it used."""

    def __init__(self):
        self.calls = []
        self._restore = []

    def wrap(self, module, stage):
        messages = module.client.messages
        original = messages.create

        def timed(**kwargs):
            started = time.perf_counter()
            response = original(**kwargs)
            usage = getattr(response, "usage", None)
            self.calls.append({
                "stage": stage,
                "seconds": time.perf_counter() - started,
                "tokens_in": getattr(usage, "input_tokens", 0) or 0,
                "tokens_out": getattr(usage, "output_tokens", 0) or 0,
            })
            return response

        messages.create = timed
        self._restore.append(lambda: setattr(messages, "create", original))

    def restore(self):
        for undo in self._restore:
            undo()
        self._restore = []


def pick_sections(chunks, n):
    """n sections spread evenly across the sorted list, so the sample is the same every run."""
    ordered = sorted(chunks, key=lambda c: (c.get("source", ""), c.get("page") or 0, c.get("locator", "")))
    if n >= len(ordered):
        return ordered
    if n <= 1:
        return [ordered[len(ordered) // 2]]
    return [ordered[round(i * (len(ordered) - 1) / (n - 1))] for i in range(n)]


def run_section(chunk, meter):
    """One section through Extractor, Mapper and Auditor, timed. A failure is recorded, never fatal."""
    first_call = len(meter.calls)
    started = time.perf_counter()
    findings, errors = [], []
    try:
        requirements = extractor.extract_requirements(chunk)
    except Exception as exc:  # noqa: BLE001 - one bad section must not stop the benchmark
        requirements = []
        errors.append({"stage": "extractor", "error": str(exc)[:200]})
    for requirement in requirements:
        try:
            findings.append(auditor.audit_requirement(mapper.map_requirement(requirement)))
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "mapper/auditor", "error": str(exc)[:200]})
    seconds = time.perf_counter() - started
    calls = meter.calls[first_call:]
    return {
        "section": f"{chunk.get('source', '?')} | {chunk.get('locator', '?')}",
        "chunk_id": chunk.get("chunk_id"),
        "seconds": seconds,
        "model_seconds": sum(c["seconds"] for c in calls),
        "model_calls": len(calls),
        "tokens_in": sum(c["tokens_in"] for c in calls),
        "tokens_out": sum(c["tokens_out"] for c in calls),
        "findings": findings,
        "errors": errors,
    }


def run_benchmark(chunks, repeat=1):
    """Run the chosen sections `repeat` times. Returns one list of section results per run."""
    meter = Meter()
    for module, stage in ((extractor, "extractor"), (mapper, "mapper"), (auditor, "auditor")):
        meter.wrap(module, stage)
    try:
        return [[run_section(chunk, meter) for chunk in chunks] for _ in range(repeat)]
    finally:
        meter.restore()


# ---- summarising -------------------------------------------------------------

def percentile(values, p):
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))]


def control_sets(run):
    return {r["section"]: {f["framework_control"] for f in r["findings"] if f.get("framework_control")} for r in run}


def agreement(run_a, run_b):
    """Average overlap (Jaccard) of the controls two runs picked for the same section."""
    a, b = control_sets(run_a), control_sets(run_b)
    scores, identical = [], 0
    for section in a:
        if section not in b or not (a[section] | b[section]):
            continue
        scores.append(len(a[section] & b[section]) / len(a[section] | b[section]))
        identical += a[section] == b[section]
    return (sum(scores) / len(scores), identical, len(scores)) if scores else None


def summarise(runs, report_path):
    first = runs[0]
    findings = [f for r in first for f in r["findings"]]
    seconds = [r["seconds"] for r in first]
    tokens_in = sum(r["tokens_in"] for r in first)
    tokens_out = sum(r["tokens_out"] for r in first)
    cost = tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M

    with open(report_path, "w", encoding="utf-8") as fh:        # the file verify_citations reads
        json.dump(findings, fh)
    integrity = verify_citations.verify(report_path) if findings else None

    coverage = {}
    for f in findings:
        coverage[f.get("coverage")] = coverage.get(f.get("coverage"), 0) + 1
    controls = [f["framework_control"] for f in findings if f.get("framework_control")]
    return {
        "sections": len(first),
        "sections_without_requirements": sum(1 for r in first if not r["findings"] and not r["errors"]),
        "findings": len(findings),
        "errors": sum(len(r["errors"]) for r in first),
        "seconds_total": sum(seconds),
        "seconds_avg_section": sum(seconds) / len(seconds) if seconds else None,
        "seconds_fastest": min(seconds) if seconds else None,
        "seconds_slowest": max(seconds) if seconds else None,
        "seconds_p50": percentile(seconds, 0.5),
        "seconds_p90": percentile(seconds, 0.9),
        "seconds_per_finding": sum(seconds) / len(findings) if findings else None,
        "model_seconds_share": sum(r["model_seconds"] for r in first) / sum(seconds) if sum(seconds) else None,
        "model_calls": sum(r["model_calls"] for r in first),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_total": cost,
        "cost_per_finding": cost / len(findings) if findings else None,
        "coverage": coverage,
        "unmapped": sum(1 for f in findings if not f.get("framework_control")),
        "other_book_controls": sum(1 for c in controls if not CONTROL_ID.match(c)),
        "integrity": integrity,
        "agreement": agreement(runs[0], runs[1]) if len(runs) > 1 else None,
    }


# ---- the report --------------------------------------------------------------

def fmt(value, unit="", digits=1):
    return "n/a" if value is None else f"{value:.{digits}f}{unit}"


def render_markdown(runs, summary, meta):
    s, first = summary, runs[0]
    integ = s["integrity"] or {}
    n = s["findings"]
    rows = [
        ("Sections tested", f"{s['sections']}, spread evenly across the 3 sample policies. Sections where no requirement was found: {s['sections_without_requirements']}"),
        ("Findings produced", f"{n}"),
        ("Errors", f"{s['errors']}"),
        ("Average time per section", fmt(s["seconds_avg_section"], " s")),
        ("Fastest / slowest section", f"{fmt(s['seconds_fastest'], ' s')} / {fmt(s['seconds_slowest'], ' s')}"),
        ("Median / 90th percentile section", f"{fmt(s['seconds_p50'], ' s')} / {fmt(s['seconds_p90'], ' s')}"),
        ("Average time per finding", fmt(s["seconds_per_finding"], " s")),
        ("Share of time spent waiting on the AI", fmt(None if s["model_seconds_share"] is None else s["model_seconds_share"] * 100, " %", 0)),
        ("AI calls", f"{s['model_calls']}"),
        ("Tokens in / out", f"{s['tokens_in']:,} / {s['tokens_out']:,}"),
        ("Estimated cost per finding", "n/a" if s["cost_per_finding"] is None else f"${s['cost_per_finding']:.3f}"),
        ("Estimated cost of the whole benchmark", f"${s['cost_total']:.2f}"),
        ("Citations that resolve to a real chunk, in the stated document and section",
         "n/a" if not integ else f"{min(integ['citation_resolves'], integ['source_matches'], integ['locator_matches'])}/{integ['findings']}"),
        ("Cited controls that exist in the index", "n/a" if not integ else f"{integ['control_resolves']}/{integ['controls_cited']}"),
        ("Controls from a different NIST book", f"{s['other_book_controls']}"),
        ("Requirements with no matching control (Not observable)", f"{s['unmapped']}" + (f" ({100 * s['unmapped'] / n:.0f} %)" if n else "")),
        ("Coverage verdicts", ", ".join(f"{k} {v}" for k, v in sorted(s["coverage"].items(), key=lambda kv: -kv[1])) or "n/a"),
    ]
    if s["agreement"]:
        mean, identical, total = s["agreement"]
        rows.append(("Run-to-run agreement on the controls picked", f"{mean:.2f} average overlap; identical in {identical} of {total} sections"))
    rows += [
        ("Evaluation method", "Objective checks against the index. No AI judge."),
        ("Accuracy against an expert answer key", "**Not measured yet.** The answer key (eval/gold_set.json) still needs a security reviewer."),
        ("Backend and model", f"{meta['backend']}; retrieval: {meta['retrieval']}"),
        ("Run on", meta["when"]),
    ]
    lines = ["## Benchmarks", "",
             f"Measured by `phase2_agents/benchmark.py` on the real library, one section at a time. "
             f"Cost is an estimate at ${PRICE_IN_PER_M:g} in / ${PRICE_OUT_PER_M:g} out per million tokens.", "",
             "| Metric | Result |", "| --- | --- |"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    lines += ["", "| Section | Findings | Seconds |", "| --- | --- | --- |"]
    # A "|" inside a table cell would be read as a column break, so escape it.
    lines += [f"| {r['section'].replace('|', chr(92) + '|')} | {len(r['findings'])}{' (error)' if r['errors'] else ''} | {r['seconds']:.1f} |" for r in first]
    lines += ["", "> Speed includes retrieval and three AI calls per requirement (Extractor, Mapper, Auditor). "
              "The first request after a restart is slower while the search model loads. "
              "Agreement and the verdict mix describe consistency, not correctness."]
    return "\n".join(lines) + "\n"


# ---- command line --------------------------------------------------------------

def load_sections():
    return extractor.search("security policy requirements users must", type="internal", top_k=200)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Benchmark the Extractor, Mapper and Auditor on real policy sections.")
    parser.add_argument("--sections", type=int, default=6, help="how many sections to run (default 6)")
    parser.add_argument("--repeat", type=int, default=1, help="run the sections this many times (2 measures agreement)")
    parser.add_argument("--dry-run", action="store_true", help="list the sections and stop; makes no AI calls")
    parser.add_argument("--allow-placeholder", action="store_true", help="run on the placeholder search (numbers are not meaningful)")
    args = parser.parse_args(argv)

    retrieval = extractor.RETRIEVER_BACKEND
    if retrieval.startswith("placeholder") and not args.allow_placeholder:
        sys.exit("The real search index is not available here, so the numbers would be meaningless.\n"
                 "Run this in Cloud Shell, or pass --allow-placeholder for a test run.")

    chunks = pick_sections(load_sections(), args.sections)
    print(f"Backend: {claude_client.DESCRIPTION} | retrieval: {retrieval}")
    print(f"Sections ({len(chunks)}):")
    for c in chunks:
        print(f"  - {c.get('source')} | {c.get('locator')} (page {c.get('page')})")
    if args.dry_run:
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    started = time.perf_counter()
    runs = run_benchmark(chunks, repeat=max(1, args.repeat))
    summary = summarise(runs, os.path.join(OUT_DIR, "benchmark_findings.json"))
    meta = {"backend": claude_client.DESCRIPTION, "retrieval": retrieval,
            "when": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    markdown = render_markdown(runs, summary, meta)

    with open(os.path.join(OUT_DIR, "BENCHMARKS.md"), "w", encoding="utf-8") as fh:
        fh.write(markdown)
    with open(os.path.join(OUT_DIR, "benchmark_results.json"), "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "summary": summary,
                   "runs": [[{k: v for k, v in r.items() if k != "findings"} | {"findings": len(r["findings"])} for r in run] for run in runs]},
                  fh, indent=2, default=str)
    print()
    print(markdown)
    print(f"(took {time.perf_counter() - started:.0f} s; saved to eval/BENCHMARKS.md and eval/benchmark_results.json)")


if __name__ == "__main__":
    main()
