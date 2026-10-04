# Phase 3: Compliance Copilot dashboard

**Owner:** Mahsa

The dashboard is where a person meets the agents' work: upload a policy, run
the Extractor, Mapper and Auditor, decide on every finding, and export a gap
report that contains only the findings a named reviewer approved. It also has
a chat page for questions about the policies and NIST controls.

It runs today on a **simulator** that produces exactly what Phase 1 and
Phase 2 produce, so it doesn't wait on the other branches. After the merge,
one setting switches it to the **live** pipeline.

## Run it

From the repo root:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r phase3_dashboard/requirements.txt

cd phase3_dashboard
streamlit run app.py
```

Then:

1. Type your name in the sidebar. Decisions are logged under it.
2. On **Analyze policy**, pick **Choose a loaded policy** (or upload a PDF), then **Run analysis**.
3. On **Review findings**, approve, edit or reject each finding.
4. Select **Finalize review**, then download the report from **Export report**.

The three sample policies cover every state: all four coverage values, an
unmapped requirement, sections with no requirements, and one section with
prompt-injection text. The sidebar's **Simulate a failing section** switch
shows how a bad model reply is handled.

### PyCharm

Run → Edit Configurations → **+** → Python. Switch "script" to **module** and
enter `streamlit`. Set the parameters to `run app.py` and the working directory
to `<repo>/phase3_dashboard`.

### Tests

```bash
python -m pytest phase3_dashboard/tests -q      # from the repo root
```

There are 66 tests. They include a headless run of the whole app and, once
Phase 2 is merged, a round trip through the real LangGraph runtime with
`phase2_agents/pipeline.py` (the agents are swapped for stand-ins, so no
API calls). Before the merge those live tests are skipped.

## How it's organized

```
app.py               pages: Analyze policy, Review findings, Export report, Ask the docs
core/
  contracts.py       checks Phase 2 output and turns it into typed findings
  review.py          the human-review rules and the audit log
  analysis.py        runs sections through the pipeline; resumes them on finalize
  report.py          Markdown, JSON and CSV exports (approved findings only)
  security.py        upload checks, safe rendering, prompt-injection warning
backends/
  simulated.py       fake Phase 1 + 2, in the same formats
  live.py            the real Phase 1 + 2 code, after the merge
  sample_data.py     invented sample policies and simulated control summaries
ui/components.py     badges, finding cards, table, chat messages
tests/               66 tests
.streamlit/          theme and server settings
```

The UI only talks to a backend through one interface. Both backends drive
their graph the same way and feed the same validation code
(`contracts.normalize_pipeline_state`). Going live changes where the data
comes from, not how it's checked or shown.

## What the simulator mirrors

| Piece | Mirrors | Same as the original |
| --- | --- | --- |
| Policy chunks | `phase1_ingestion/chunk_policies.py` | fields, `policy-<sha256>` IDs, `Section …` locators |
| `search()` | `phase1_ingestion/retriever.py` | 8 result fields, argument checks (rejects `type="policy"` too) |
| Extractor | `phase2_agents/extractor.py` | `requirement`, `requirement_text`, `source`, `chunk_id`, `locator` |
| Mapper | `phase2_agents/mapper.py` | adds `mapped_control`, `mapping_reasoning` to the same dicts |
| Auditor | `phase2_agents/auditor.py` | the gap-report entry, `status: "pending"`, `"None"` when Full |
| Graph | `phase2_agents/pipeline.py` | pauses at human review; resumes with `{"approved": [...]}` |

Keyword rules stand in for Claude's judgment, so simulated findings are
plausible but aren't real results. The simulated NIST texts are short
paraphrases, labeled as such. Every simulated report says so at the top.

## Switching to the live pipeline

1. Merge `phase1-maryam` and `phase2-shreeja` into your branch.
2. `pip install -r requirements.txt` (the root one), and put `ANTHROPIC_API_KEY` in `.env`.
   Make sure the root `.gitignore` lists `.env`.
3. Run Phase 1's build scripts (see its README) so `data/processed/policy_chunks.json`
   and `chroma_db/` exist.
4. Start the app and choose **Live pipeline** in the sidebar, or start it with
   `COPILOT_BACKEND=live`.

If something is missing, the sidebar says what. It never falls back to
simulated data while claiming to be live. Live mode calls Claude for every
section, so analyze a few sections first.

### Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `COPILOT_BACKEND` | `simulated` | `simulated` or `live` at startup |
| `COPILOT_SIM_DELAY` | `0.3` | Seconds per section in the simulator |
| `COPILOT_QA_MODULE` | none | Anu's Q&A agent as `module:function`. Without it, live chat shows search results only |
| `ANTHROPIC_API_KEY` | none | Used by the Phase 2 agents, never by the dashboard itself |

## The contract with Phase 2

**In.** `pipeline.py` pauses with `interrupt({"message": ..., "findings": gap_report})`.
Each finding is what `auditor.py` returns:

```json
{
  "requirement": "No credential sharing",
  "coverage": "Partial",
  "finding": "…",
  "recommendation": "…",
  "citation": {"chunk_id": "policy-…", "source": "Computer Security Policy.pdf", "locator": "Section 3.5 …"},
  "framework_control": "IA-5",
  "status": "pending"
}
```

The dashboard also recovers `requirement_text` and `mapping_reasoning` from
the mapper output in the same state, and the page from the analyzed chunk.
Extra fields the agents add later (an ambiguity note, say) show up under
**Evidence → Other fields** without code changes.

**Out.** On finalize, each paused thread is resumed with
`Command(resume={"approved": [...]})`: its approved findings in the auditor's
format, plus `reviewer`, `reviewed_at`, `reviewer_note`, and, when the reviewer
edited the text, `recommendation_ai_original`.

`pipeline.py` analyzes one chunk per run, so the dashboard runs one LangGraph
thread per section and resumes each with its own approved findings.

## Human review and safety rules

These are enforced in `core/`, not just in the UI.

- Every decision needs a reviewer name. Rejecting needs a reason. Edits to a recommendation are logged with the before and after text.
- A finding that arrives already marked approved is reset to pending: only a person approves.
- Findings that fail validation (unknown coverage, missing citation) can be rejected but not approved.
- Results are only accepted from a pipeline that paused for review.
- Finalizing requires a decision on every finding, then locks the review. The report contains approved findings only, with the full audit trail.
- Document and model text is escaped before display, so it can't render links, images, HTML or tables. Nothing uses `unsafe_allow_html`.
- Uploads must be real PDFs with selectable text, at most 10 MB and 80 pages, not password-protected. They're read in memory, never saved.
- Policy text that looks like instructions to an AI triggers a warning for the reviewer.
- CSV cells that would run as spreadsheet formulas are neutralized.
- The browser shows only error types; details go to the terminal.

## Notes for Phase 2

Found while matching the dashboard to the current agent code:

1. `extractor.py` and `pipeline.py` call `search(..., type="policy")`. The real retriever only accepts `internal`, `framework` or `evidence`, so these raise errors after the swap.
2. `auditor.py` looks the control up again with `search(control_id)` and takes the top hit. Phase 1's README warns this can return a different control. Keep the Mapper's chosen `chunk_id` and use `get_original_chunk()`.
3. When the Mapper returns `null`, the auditor calls `search(None)`, which raises with the real retriever. Return "Not observable" directly instead.
4. The auditor's output drops `requirement_text`, `page` and `mapping_reasoning`, and has no finding ID. The dashboard works around this, but adding them upstream is simpler.
5. Gaps are found only for requirements the policy states. A control the policy never mentions can't come out as "Missing." A pass over a chosen baseline of controls would catch those.
6. `type="framework"` searches cover NIST CSF 2.0 as well as SP 800-53, so the Mapper may return CSF outcomes. A framework filter would also let the dashboard's framework selector drive the choice.

## Adding features

- **A new page:** add its name to `PAGES` in `app.py` and a `page_…` function.
- **A new table column or card detail:** `ui/components.py`.
- **A new report section:** `core/report.py` (Markdown, JSON and CSV builders).
- **A new field from the agents:** shows automatically under Other fields. To give it a proper place, add it to `Finding` in `core/contracts.py`.
- **Theme:** `.streamlit/config.toml`.

## Deploying later

The app is a single Streamlit process, so it can run in a container on
Cloud Run in the team's Google Cloud project when it's time to host the demo.
The API key would go in Secret Manager, not in the image.
