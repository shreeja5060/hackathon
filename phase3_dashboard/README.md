# Phase 3: Compliance Copilot dashboard

**Owner:** Mahsa

The dashboard is where a person meets the agents' work: upload a policy, run
the Extractor, Mapper and Auditor, review every finding next to its evidence,
and sign off a report that contains only the findings a named reviewer
approved and a second reviewer confirmed. Everything is kept in a permanent,
tamper-evident record (Shreeja's ledger), so reviews survive the browser and
the server, and each finding shows what the organization's other policies say
about the same control. Anu's Q&A assistant is built in: it answers questions
about the policies, NIST and the current review, and can suggest stronger
wording for a finding, which the reviewer still has to accept and approve.
For an organization with no policy yet, the Policy writer drafts a starter
policy from NIST controls, which then goes through the same review.

It runs in two modes. **Live** uses the real code on main: Maryam's index and
chunker, Shreeja's agents and LangGraph pipeline, and Anu's `qa_agent`.
**Simulator** produces the same data formats from local rules, with no AI
calls, for offline work and demos without credentials.

## The user flow

```
 Library ──► Analyze ──► Review ──► (second reviewer confirms) ──► Sign-off
    ▲            ▲          │ Assistant: questions, sources, suggested wording
    │            │          ▼
    └── every step is kept: versions, findings, decisions, trail
                 │
 Policy writer ──┘  (no policy yet: draft one, approve it, check it like any other)
```

| Page | What happens |
| --- | --- |
| **Library** | Every policy the organization has analyzed: versions, reviews and their state, what changed between versions, a memory note. **Across policies** shows values that differ between policies (a 15- vs 5-minute screen lock), a coverage map, and the starter controls no policy addresses yet. **Activity trail** lists every action and checks the hash chain. Returning users land here. |
| **Analyze** | Upload a PDF, pick an indexed policy, or use a starter draft; choose sections; run the agents. The page says whether the library already has this exact file, or which version this is. Text that looks like prompt injection is flagged. |
| **Review** | A coverage bar and decision progress on top; the review queue on the left; the open finding on the right with the policy quote, plain-language summary, finding, recommendation, reviewer questions, evidence, **what your other policies say about this control**, and what was decided on the same text in an earlier version. Approve, edit or reject; under the two-person rule a different reviewer then confirms or sends back each decision. **Suggest stronger wording** asks the assistant for a rewrite that waits on the finding until **Use this wording**. |
| **Sign-off** | Locked until every decision is made and confirmed. Then a table of approved findings with who decided, who confirmed and where each recommendation's wording came from, the audit trail, and Markdown, JSON and CSV downloads. |
| **Assistant** | Anu's Q&A agent with numbered sources, question shortcuts (including "Do our other policies cover or contradict these findings?"), and cards for every suggested rewrite that open the finding in review. |
| **Policy writer** | A few questions about the organization, topics to cover, then a draft whose every rule is tied to a NIST SP 800-53 control (SP 800-53B low baseline plus a few moderate ones). Live mode lets the model adapt each section from NIST's text, validated; edit, approve, download, and **check this draft** with the gap analysis. |

### Memory and the two-person rule

- Every upload, analysis and review action is written to the ledger as it happens
  (`core/memory.py` over `shared/ledger.py`). Uploaded PDFs and approved drafts are
  saved by their SHA-256. Any later session, on any machine sharing the ledger, can
  reopen a review from the Library.
- With the two-person rule (on by default), a reviewer's approvals and rejections are
  confirmed by a different person, who can also send a decision back with a reason.
  Rejections are checked too, because rejecting hides a gap from the report. Only the
  person who decided can undo. The ledger enforces the rule a second time.
- If someone else changed a review since this page loaded, the page says so and won't
  overwrite their decisions; **Reopen the latest version** reloads it.
- Simulator records go to a separate file, so demo data never mixes with real reviews.
- On Cloud Run the ledger is streamed to Cloud Storage; see `deploy/README.md`.

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
the findings, change the reviewer name to a colleague's to confirm them (on a
deployed copy with company sign-in, the colleague signs in instead), and select
**Finalize review** to unlock the signed report.

In the simulator, the three sample policies cover every state: all four
coverage values, an unmapped requirement, sections with no requirements, and
one section with prompt-injection text. They also overlap like a real policy
set: analyze the computer security policy and then the remote work policy to
see a conflicting screen-lock time and an incident deadline covered elsewhere.
The simulated assistant can list findings ("Which findings have Missing
coverage?"), propose wording ("Suggest better wording for the rejected
findings") and answer from the other policies ("Is F-012 covered elsewhere?").
Developers can make a section fail on purpose with `COPILOT_SIM_FAIL_SECTION=1`
to see how a bad model reply is handled; there is no switch for it in the UI.

### Tests

```bash
python -m pytest phase3_dashboard/tests -q      # from the repo root
```

154 tests, run by CI on every push (`.github/workflows/phase3-tests.yml`). They
include headless runs of the whole app and end-to-end runs through the real
Phase 1 chunker, Phase 2 agents and LangGraph pipeline and Anu's `qa_agent`,
in the UI too: upload, analyze, ask for wording, use it, approve, sign off.
Others cover the ledger round trip (a review reopened in a new session, stale
pages refused, tampering detected), the two-person rule, overlap detection and
the policy writer (including AI sections that must keep their NIST controls).
Only Claude's replies and the search index are faked, so no key is needed.
Every test gets its own empty ledger.

## How it's organized

```
app.py               pages: Library, Analyze, Review, Sign-off, Assistant, Policy writer
core/
  contracts.py       checks agent and assistant output and turns it into typed findings
  review.py          the human-review rules (two-person rule included) and the audit log
  memory.py          the permanent record: writes each action to the ledger, reopens reviews
  overlaps.py        what other policies say about the same control: conflicts, overlaps
  analysis.py        runs sections through the pipeline; resumes them on finalize
  report.py          Markdown, JSON and CSV exports (approved findings only)
  identity.py        the reviewer from company sign-in (Identity-Aware Proxy), when configured
  security.py        upload checks, safe rendering, prompt-injection warning
backends/
  simulated.py       fake Phase 1 + 2 and a rule-based assistant, in the same formats
  live.py            the real code: start_review / submit_decisions, qa_agent.ask
  sample_data.py     invented sample policies and simulated control summaries
ui/components.py     header, coverage bar, review queue, finding detail, chat pieces
static/fonts/        Public Sans and IBM Plex Mono (OFL), served locally
tests/               154 tests, including live end-to-end runs with the Q&A agent
.streamlit/          theme, fonts and server settings
../policy_writer/    the starter policy writer: NIST-grounded topics and clauses, AI drafting
../deploy/           Cloud Run start script and Litestream config for the ledger
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
| `LEDGER_PATH` | `data/ledger.sqlite3` | The permanent record; the simulator uses `ledger-simulator.sqlite3` beside it |
| `COPILOT_DOCUMENT_DIR` | `data/documents` | Where uploaded PDFs and approved drafts are kept (simulator: `documents-simulator`) |
| `COPILOT_MEMORY` | `on` | `off` keeps reviews only for the browser session |
| `COPILOT_TWO_PERSON` | `on` | `off` lets one reviewer decide and sign off alone |
| `COPILOT_IDENTITY` | none | `iap`: the reviewer is the account signed in through Identity-Aware Proxy |
| `COPILOT_IAP_AUDIENCE` | none | With IAP, also verify its signed token for this audience (see `deploy/README.md`) |
| `LEDGER_REPLICA_URL` | none | In the Docker image: stream the ledger to Cloud Storage, e.g. `gs://BUCKET/ledger` |
| `COPILOT_SIM_FAIL_SECTION` | none | Developers: make that simulator section fail like a bad model reply |

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
reviewer's edited recommendation when there is one, and the names of who
decided (`reviewer`) and who confirmed (`confirmed_by`). The dashboard then
checks that the pipeline recorded exactly those decisions. Runs whose sections
had no findings are closed with an empty decision set. A review reopened after
a restart skips runs that are no longer paused (the ledger already has its
decisions).

One run per section means progress shows section by section, and one bad
section can't hold up the rest.

## Human review and safety rules

These are enforced in `core/`, not just in the UI.

- Every decision needs a reviewer name. Rejecting needs a reason. Edits to a recommendation are logged with the before and after text.
- Two-person rule: a different person confirms every decision (names compared ignoring case and spacing), sending back needs a reason, and only the decider can undo. The ledger refuses a self-confirmation too.
- Every action lands in the ledger's hash-chained trail; the Library verifies the chain and shows the first broken entry if anyone edits the file.
- A finding that arrives already marked approved is reset to pending: only a person approves.
- Findings that fail validation (unknown coverage, missing citation) can be rejected but not approved.
- Results are only accepted from a pipeline that paused for review.
- Finalizing requires a decision on every finding, then locks the review. The report contains approved findings only, with the full audit trail.
- The assistant's suggested wording never changes a finding. It waits until the reviewer selects **Use this wording** and approves; the audit trail records the suggestion and whether it was accepted as is or edited, and the report marks each recommendation's origin. A suggestion for an already-decided finding needs the reviewer to reopen it first. Suggestions naming findings that aren't in the review are dropped.
- Bulk approval covers only Full-coverage findings with no flags or validation problems; each approval is logged separately and marked as bulk.
- Document and model text is escaped before display, so it can't render links, images, HTML or tables. Nothing uses `unsafe_allow_html`; the only raw HTML is the coverage bar, built from counts and fixed labels.
- Uploads must be real PDFs with selectable text, at most 10 MB and 80 pages, not password-protected. They're checked in memory; when memory is on, the original is kept in the document store under its SHA-256 (and never in the Docker image).
- Starter policies: every AI-drafted section must keep the section's NIST controls and name no others, or it falls back to the baseline clauses and says so. The draft is checked without its control tags.
- Policy text that looks like instructions to an AI triggers a warning for the reviewer.
- CSV cells that would run as spreadsheet formulas are neutralized.
- The browser shows only error types; details go to the terminal.

## Notes for the team

Fixed on main since the first version of this list: the `type="policy"`
calls, the Auditor re-searching controls, the crash on a null mapping, the
missing finding IDs, whole-document runs, and CSF outcomes in the Mapper (#15).

Changes in other people's files in this branch, for review:

- **Shreeja, `shared/ledger.py`:** `reopened` returns a finding to pending in
  `finding_state()`; read-back queries `documents()`, `runs()`, `run_findings()`,
  `last_seq()`; `record_run(detail=...)`. Tests in `tests/test_ledger.py`.
  Rejection confirmations are recorded as `rejection_confirmed` (state stays
  `rejected`). `submit_decisions()` now receives `reviewer` and `confirmed_by`
  with each decision, if you want the pipeline to store them.
- **Anu, `qa_agent/agent.py`:** `get_finding_evidence` also returns what other
  reviewed policies say about the finding's control, as citable passages, and
  the prompt asks the model to point out differences. **`Dockerfile`:**
  Litestream, `deploy/start.sh` as the start command, uid 1000; see
  `deploy/README.md` for the Cloud Run settings (4 GiB fixes the 512 MiB crash).

Still open:

1. **Requirement-driven gaps.** A control no policy mentions can't come out of the agents as "Missing." The Library now lists starter-baseline controls that no stored policy addresses, which covers the obvious cases.
2. **Silent placeholder search.** `pipeline.py` falls back to `fake_search` without failing. The dashboard warns, but a demo machine should have the real index.
3. **Which rule wins in a conflict** is the organization's call. The dashboard shows both values side by side; a policy-precedence setting (stricter wins, or more specific wins) would let it suggest one.
4. **Many reviewers at once.** The ledger is SQLite with one writer (one Cloud Run instance). For a whole company, move it to Cloud SQL (see `deploy/README.md`).

## Adding features

- **A new page:** add its name to `PAGES` and `NAV_LABELS` in `app.py` and a `page_…` function.
- **A new starter-policy topic:** `policy_writer/catalog.py` (topic, controls, clauses), then the simulator's rules in `backends/sample_data.py` if the check should recognize it.
- **A new table column or card detail:** `ui/components.py`.
- **A new report section:** `core/report.py` (Markdown, JSON and CSV builders).
- **A new field from the agents:** shows automatically under Other fields. To give it a proper place, add it to `Finding` in `core/contracts.py`.
- **Theme and upload limits:** `.streamlit/config.toml`.

## Deploying on Cloud Run

The root `Dockerfile` builds the index into the image and starts this
dashboard through `deploy/start.sh` in live mode with the Q&A agent. Follow
`deploy/README.md`: a bucket, `ANTHROPIC_API_KEY` from Secret Manager, 4 GiB of
memory, one instance with CPU always allocated, and
`LEDGER_REPLICA_URL=gs://BUCKET/ledger` so the ledger survives restarts. Add
Identity-Aware Proxy so reviewers sign in with their company accounts. When the
project's Vertex quota is raised, switch with `CLAUDE_BACKEND=vertex`,
`ANTHROPIC_VERTEX_PROJECT_ID=uc2-cyber-policy-compliance` and
`CLOUD_ML_REGION=global` (service account: Vertex AI User role), and unset
`CHAT_MODEL`.

