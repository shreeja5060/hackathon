# Phase 3: Compliance Copilot dashboard

**Owner:** Mahsa

The dashboard is where a person meets the agents' work: upload a policy, run
the Extractor, Mapper and Auditor, decide on every finding, and export a gap
report that contains only the findings a named reviewer approved. It also has
a chat page for questions about the policies and NIST controls.

It runs in two modes. **Live** uses the real Phase 1 and Phase 2 code on
main: Shreeja's agents and LangGraph pipeline, Maryam's chunker and index, and
Anu's Q&A agent once it's merged. **Simulator** produces the same data format
from local rules, with no API key, for demos and offline work.

## Run it

Python 3.12 (Phase 1 needs it), from the repo root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # the team's root file covers every phase

cd phase3_dashboard
streamlit run app.py
```

Then:

1. Type your name in the sidebar. Decisions are logged under it.
2. Pick **Simulator** or **Live pipeline** in the sidebar (see below for Live).
3. On **Analyze policy**, upload a PDF or choose a loaded policy, then **Run analysis**.
4. On **Review findings**, approve, edit or reject each finding.
5. Select **Finalize review**, then download the report from **Export report**.

In the simulator, the three sample policies cover every state: all four
coverage values, an unmapped requirement, sections with no requirements, and
one section with prompt-injection text. **Simulate a failing section** shows
how a bad model reply is handled.

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
  live.py            the real Phase 1 + 2 code (start_review / submit_decisions)
  sample_data.py     invented sample policies and simulated control summaries
ui/components.py     badges, finding cards, table, chat messages
tests/               73 tests, including live end-to-end runs
.streamlit/          theme and server settings
```

The UI only talks to a backend through one interface, and both backends feed
the same validation code (`contracts.normalize_pipeline_state`). Switching
modes changes where the data comes from, not how it's checked or shown.

## What the simulator mirrors

| Piece | Mirrors | Same as the original |
| --- | --- | --- |
| Policy chunks | `phase1_ingestion/chunk_policies.py` | fields, `policy-<sha256>` IDs, `Section …` locators |
| `search()` | `phase1_ingestion/retriever.py` | 8 result fields, argument checks (rejects `type="policy"` too) |
| Extractor | `phase2_agents/extractor.py` | `requirement`, `requirement_text`, `source`, `chunk_id`, `locator` |
| Mapper | `phase2_agents/mapper.py` | adds `mapped_control`, `mapping_reasoning` to the same dicts |
| Auditor | `phase2_agents/auditor.py` | the gap-report entry with `plain_language` and `clarifying_questions`; its exact no-control entry |
| Graph | `phase2_agents/pipeline.py` | `F001`-style IDs; failures in `errors`; pauses at review; resumes with `{finding_id: {"decision"}}` |

Keyword rules stand in for Claude's judgment, so simulated findings are
plausible but aren't real results. The simulated NIST texts are short
paraphrases, labeled as such. Every simulated report says so at the top.

## Running the live pipeline

1. Use Python 3.12 and `pip install -r requirements.txt` from the repo root.
2. Put `ANTHROPIC_API_KEY` in `.env` (`cp .env.example .env`). The root `.gitignore` already ignores it.
3. Build Phase 1's index (see `phase1_ingestion/README.md`) so `data/processed/policy_chunks.json`
   and `chroma_db/` exist. Without it, "Choose a loaded policy" is empty; uploads still work.
4. Start the app and choose **Live pipeline** in the sidebar, or start it with `COPILOT_BACKEND=live`.

The sidebar shows which retriever Phase 2 is really using. `pipeline.py`
quietly falls back to `shared/fake_search.py` when Phase 1's retriever can't
load (for example on Intel Macs, which have no `torch` builds). The dashboard
warns about it, so placeholder matches are never mistaken for real ones.

If something is missing, the sidebar says what. It never falls back to
simulated data while claiming to be live. Live mode calls Claude for every
requirement, so analyze a few sections first.

### Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `COPILOT_BACKEND` | `simulated` | `simulated` or `live` at startup |
| `COPILOT_SIM_DELAY` | `0.3` | Seconds per section in the simulator |
| `COPILOT_QA_MODULE` | `qa_agent.agent:ask` if present | Which Q&A function the chat uses. Without one, live chat shows search results only |
| `ANTHROPIC_API_KEY` | none | Used by the Phase 2 agents and the Q&A agent, never by the dashboard itself |

## The contract with Phase 2

The dashboard calls `pipeline.py`'s review API, once per section:

```python
run = start_review([chunk], run_id=thread_id)   # Extractor -> Mapper -> Auditor, then pauses
submit_decisions(thread_id, {"F001": {"decision": "approved", "recommendation": "edited text"},
                             "F002": {"decision": "rejected"}})
```

**In.** Each finding is what the Auditor returns plus the `finding_id` the
pipeline adds:

```json
{
  "finding_id": "F001",
  "requirement": "MFA for privileged accounts",
  "coverage": "Partial",
  "finding": "…",
  "recommendation": "…",
  "plain_language": "The rule says MFA is needed but not where.",
  "clarifying_questions": ["Which systems count as privileged?"],
  "citation": {"chunk_id": "policy-…", "source": "…", "locator": "Section 3.3 …"},
  "framework_control": "IA-2",
  "status": "pending"
}
```

The dashboard also recovers `requirement_text` and `mapping_reasoning` from the
paused run's mapper output, and the page from the analyzed chunk. Per-item
failures in the run's `errors` list are shown on the Review page and in the
report. Fields the agents add later show up under **Evidence → Other fields**.

**Out.** On finalize, every decision goes back, rejections included, with the
reviewer's edited recommendation when there is one. The dashboard then checks
that the pipeline recorded exactly those decisions. Runs whose sections had no
findings are closed with an empty decision set. Reviewer names, notes and
timestamps stay in the dashboard's audit trail and the exported report.

One run per section means progress shows section by section, and one bad
section can't hold up the rest.

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

## Notes for the team

Fixed on main since the first version of this list: the `type="policy"`
calls, the Auditor re-searching controls (it now uses `get_original_chunk`),
the crash on a null mapping, the missing finding IDs, and whole-document runs.
Still open:

1. **Requirement-driven gaps only.** A control the policy never mentions can't come out as "Missing." A pass over a chosen baseline of controls would catch those.
2. **Two frameworks in one search.** `type="framework"` covers NIST CSF 2.0 as well as SP 800-53, so the Mapper may pick CSF outcomes. A framework filter would let the dashboard's framework selector drive it.
3. **Silent placeholder search.** `pipeline.py` falls back to `fake_search` without failing. The dashboard warns, but a demo machine should have the real index.
4. **Reviewer details.** `submit_decisions()` records approve/reject and edits, but not who decided or why. The dashboard keeps that in its audit trail; the pipeline could store it too.
5. **Two dashboards.** `phase3-anu` has its own `phase3_dashboard/app.py`, which would conflict with this one in main. Anu's Q&A agent (`chat-anu`) already plugs into this dashboard's chat once merged.

## Adding features

- **A new page:** add its name to `PAGES` in `app.py` and a `page_…` function.
- **A new table column or card detail:** `ui/components.py`.
- **A new report section:** `core/report.py` (Markdown, JSON and CSV builders).
- **A new field from the agents:** shows automatically under Other fields. To give it a proper place, add it to `Finding` in `core/contracts.py`.
- **Theme and upload limits:** `.streamlit/config.toml`.

## Deploying later

The app is a single Streamlit process, so it can run in a container on
Cloud Run in the team's Google Cloud project when it's time to host the demo.
The API key would go in Secret Manager, not in the image.
