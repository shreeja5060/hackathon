# Phase 2 — Agents

**Owner:** Shreeja

Three agents that turn policy text into a compliance gap report, plus a
LangGraph pipeline with a human-approval checkpoint.

| Agent | Input | Output |
|---|---|---|
| **Extractor** (`extractor.py`) | one policy chunk | list of discrete requirements, each with a citation (source, section, chunk id) |
| **Mapper** (`mapper.py`) | one requirement | best-matching NIST 800-53 control, with reasoning and the exact control chunk id |
| **Auditor** (`auditor.py`) | one mapped requirement | coverage verdict (`Full` / `Partial` / `Missing` / `Not observable`), finding, recommendation, plain-language explanation, clarifying questions |

`pipeline.py` chains them with LangGraph and pauses at a human-review
`interrupt()` before any finding is final. `run_full_pipeline.py` runs every
policy chunk through all three agents with per-stage error isolation and
writes `full_gap_report.json`.

## Dashboard integration (human-approval checkpoint)

`pipeline.py` exposes the review step as two calls, so the dashboard's
Approve/Reject buttons drive a genuinely suspended pipeline rather than
editing a JSON field:

```python
from phase2_agents.pipeline import start_review, submit_decisions

run = start_review(chunks)              # Extractor -> Mapper -> Auditor, then PAUSES
run["findings"]                         # pending findings, each with a finding_id
final = submit_decisions(run["run_id"], {
    "F001": {"decision": "approved"},
    "F002": {"decision": "rejected"},
    "F003": {"decision": "approved", "recommendation": "edited text"},
})
final["approved"], final["rejected"], final["pending"]
```

Undecided findings stay `pending`; nothing is marked approved by default.
`get_pending(run_id)` returns a paused run's findings (e.g. after a page refresh).

## Finding format

Every finding has the same shape (this is the contract the dashboard reads):

```json
{
  "requirement": "Comply with authentication requirements",
  "coverage": "Partial",
  "finding": "MFA is required 'where required' but the scope is never defined.",
  "recommendation": "Define which systems and user roles require MFA.",
  "plain_language": "The rule says MFA is needed but not where.",
  "clarifying_questions": ["Which systems count as privileged?"],
  "citation": {"chunk_id": "policy-...", "source": "Public_Release_Computer_Security_Policy.pdf", "locator": "Section 3.5 Authentication"},
  "framework_control": "IA-2",
  "status": "pending"
}
```

## Retrieval backend

The agents import `search()` and `get_original_chunk()` from Phase 1's real
retriever (`phase1_ingestion/retriever.py`) when its dependencies are
installed. On machines that cannot install them (Intel Macs: no `torch`/
`onnxruntime` builds), they fall back to `shared/fake_search.py`, which has
the same interface and a few sample chunks. Every script prints which backend
it is using:

```
Retrieval backend: real (phase1_ingestion/retriever.py)
```

Phase 1 requirements install on Linux and Apple Silicon. For a Linux
environment without installing anything, use Cloud Shell on the GCP project.

## Run

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY
python phase2_agents/run_full_pipeline.py        # all policy chunks -> full_gap_report.json
python phase2_agents/pipeline.py                 # one chunk through the LangGraph graph with human-approval pause
```

## Verify

```bash
pytest tests/ -v                                                  # 19 unit tests, no API calls (also run in CI on every push)
python phase2_agents/verify_citations.py full_gap_report.json     # every citation resolves to a real chunk, source, and section
python phase2_agents/evaluate.py                                  # accuracy vs. the human-labeled gold set in eval/
```

`tests/` exercises the agents with a fake Claude client: JSON parsing, citation
carry-through, the no-control path, the `get_original_chunk` integration,
the `max_tokens` regression, and error isolation in the full run.

`eval/gold_set.json` holds 15 verbatim policy requirements with human-labeled
expected control and coverage; `evaluate.py` reports Mapper accuracy, Auditor
agreement, and citation validity against them. See `eval/README.md`.

## Integration status

- Phase 1 + Phase 2 verified end to end on Linux (Cloud Shell, Python 3.12):
  all 28 policy sections across the three sample PDFs, zero code errors.
- Bugs found and fixed by the full run and the tests: Auditor output
  truncated at `max_tokens=300` (raised to 1000); no-control findings
  missing the `plain_language` / `clarifying_questions` fields.
- Mapper candidate count raised from 5 to 15 after a real control (IA-5) was
  missed in the top 5.

## Also here

`shared/mcp_server.py` + `mcp_client.py` expose `search()` as an MCP tool and
prove the round trip. `shared/llama_retriever.py` is a LlamaIndex-based
alternative retrieval layer, configured to use Claude (not OpenAI). Neither is
on the critical path for the demo.
