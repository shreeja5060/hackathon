# Phase 3: Compliance Copilot dashboard

**Owner:** Mahsa

The dashboard is where a person meets the agents' work: upload a policy, run
the Extractor, Mapper and Auditor, review every finding next to its evidence,
and sign off a report that contains only the findings a named reviewer
approved. Anu's Q&A assistant is built in: it answers questions about the
policies, NIST and the current review, and can suggest stronger wording for a
finding, which the reviewer still has to accept and approve.

It runs in two modes. **Live** uses the real code on main: Maryam's index and
chunker, Shreeja's agents and LangGraph pipeline, and Anu's `qa_agent`.
**Simulator** produces the same data formats from local rules, with no AI
calls, for offline work and demos without credentials.

## What a reviewer does

| Page | What happens |
| --- | --- |
| **Analyze policy** | Upload a PDF or pick an indexed policy, choose sections in a table, run the agents. Text that looks like prompt injection is flagged. |
| **Review findings** | A coverage bar and decision progress on top; the review queue on the left; the open finding on the right with the policy quote, plain-language summary, finding, recommendation, reviewer questions and evidence. Approve, edit or reject, and the next open finding opens. Full-coverage findings with no flags can be approved together. **Suggest stronger wording** asks the assistant for a rewrite that waits on the finding until **Use this wording**. |
| **Signed report** | Locked until sign-off. Then a table of approved findings with where each recommendation's wording came from, the audit trail, and Markdown, JSON and CSV downloads. |
| **Assistant** | Anu's Q&A agent with numbered sources, question shortcuts, and cards for every suggested rewrite that open the finding in review. |

## Run it

Python 3.12 (Phase 1 needs it), from the repo root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # the team's root file covers every phase

cd phase3_dashboard                # so .streamlit/config.toml (theme, fonts, limits) loads
streamlit run app.py               # simulator; add COPILOT_BACKEND=live for real data
```

Then type your name as the reviewer in the sidebar, analyze a policy, review
the findings, and select **Finalize review** to unlock the signed report.

In the simulator, the three sample policies cover every state: all four
coverage values, an unmapped requirement, sections with no requirements, and
one section with prompt-injection text. **Simulator options → Simulate a
failing section** shows how a bad model reply is handled. The simulated
assistant can list findings ("Which findings have Missing coverage?") and
propose wording ("Suggest better wording for the rejected findings").

### Tests

```bash
python -m pytest phase3_dashboard/tests -q      # from the repo root
```

91 tests, run by CI on every push (`.github/workflows/phase3-tests.yml`). They
include headless runs of the whole app and end-to-end runs through the real
Phase 1 chunker, Phase 2 agents and LangGraph pipeline and Anu's `qa_agent`,
in the UI too: upload, analyze, ask for wording, use it, approve, sign off.
Only Claude's replies and the search index are faked, so no key is needed.

## How it's organized

```
app.py               pages: Analyze policy, Review findings, Signed report, Assistant
core/
  contracts.py       checks agent and assistant output and turns it into typed findings
  review.py          the human-review rules, assistant suggestions and the audit log
  analysis.py        runs sections through the pipeline; resumes them on finalize
  report.py          Markdown, JSON and CSV exports (approved findings only)
  security.py        upload checks, safe rendering, prompt-injection warning
backends/
  simulated.py       fake Phase 1 + 2 and a rule-based assistant, in the same formats
  live.py            the real code: start_review / submit_decisions, qa_agent.ask
  sample_data.py     invented sample policies and simulated control summaries
ui/components.py     header, coverage bar, review queue, finding detail, chat pieces
static/fonts/        Public Sans and IBM Plex Mono (OFL), served locally
tests/               91 tests, including live end-to-end runs with the Q&A agent
.streamlit/          theme, fonts and server settings
```

The UI only talks to a backend through one interface, and both backends feed
the same validation code (`contracts.normalize_pipeline_state`,
`contracts.normalize_chat_answer`). Switching modes changes where the data
comes from, not how it's checked or shown.

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
2. `cp .env.example .env` and set how the agents reach Claude (the root `.gitignore` ignores `.env`):
   - **Anthropic API, for now**: `CLAUDE_BACKEND=anthropic` and your own `ANTHROPIC_API_KEY=...`.
     No key? Use the **Simulator** instead; it needs none.
   - **Vertex AI, later**: the GCP project's Claude quota is 0 (every call returns 429), so the team
     isn't using it before the demo. Once the quota is raised: `CLAUDE_BACKEND=vertex`,
     `ANTHROPIC_VERTEX_PROJECT_ID=uc2-cyber-policy-compliance`, `CLOUD_ML_REGION=global`, and
     `gcloud auth application-default login` once.

   Check it with `python shared/claude_client.py`, which makes one tiny call.
3. Build Phase 1's index (see `phase1_ingestion/README.md`) so `data/processed/policy_chunks.json`
   and `chroma_db/` exist. Without it, "Choose a loaded policy" is empty; uploads still work.
4. Start the app with `COPILOT_BACKEND=live streamlit run app.py`, or choose **Live pipeline** in the sidebar.

The Phase 2 agents and Anu's Q&A agent both go through `shared/claude_client.py`, so one
`CLAUDE_BACKEND` setting covers the whole app. On Vertex the chat uses the agents' model unless
`CHAT_MODEL` is set to a Vertex model ID enabled in the project.

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
| `CLAUDE_BACKEND` | `anthropic` | `vertex` or `anthropic`, for the agents and the Q&A assistant |
| `CHAT_MODEL` | Haiku 4.5 (Anthropic API), the agents' model (Vertex) | Model for the Q&A assistant |
| `ANTHROPIC_API_KEY` | none | Only with `CLAUDE_BACKEND=anthropic`; the dashboard itself never uses it |

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
- The assistant's suggested wording never changes a finding. It waits until the reviewer selects **Use this wording** and approves; the audit trail records the suggestion and whether it was accepted as is or edited, and the report marks each recommendation's origin. A suggestion for an already-decided finding needs the reviewer to reopen it first. Suggestions naming findings that aren't in the review are dropped.
- Bulk approval covers only Full-coverage findings with no flags or validation problems; each approval is logged separately and marked as bulk.
- Document and model text is escaped before display, so it can't render links, images, HTML or tables. Nothing uses `unsafe_allow_html`; the only raw HTML is the coverage bar, built from counts and fixed labels.
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
5. **Suggestions one finding at a time.** The assistant can propose wording for several findings in one answer, but each is accepted and approved individually, on purpose.

## Adding features

- **A new page:** add its name to `PAGES` in `app.py` and a `page_…` function.
- **A new table column or card detail:** `ui/components.py`.
- **A new report section:** `core/report.py` (Markdown, JSON and CSV builders).
- **A new field from the agents:** shows automatically under Other fields. To give it a proper place, add it to `Finding` in `core/contracts.py`.
- **Theme and upload limits:** `.streamlit/config.toml`.

## Deploying on Cloud Run

The root `Dockerfile` builds the index into the image and starts this
dashboard from its folder (so the theme, fonts and limits load) in live mode
with the Q&A agent. On the Cloud Run service pass `ANTHROPIC_API_KEY` from
Secret Manager (the default `CLAUDE_BACKEND=anthropic`), and give it 4 GiB of
memory, min and max instances 1 and session affinity, since review decisions
live in the running instance's memory. When the project's Vertex quota is
raised, switch with `CLAUDE_BACKEND=vertex`,
`ANTHROPIC_VERTEX_PROJECT_ID=uc2-cyber-policy-compliance` and
`CLOUD_ML_REGION=global` (service account: Vertex AI User role), and unset
`CHAT_MODEL`.

