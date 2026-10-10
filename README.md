# 🛡️ Compliance Copilot

**AI agents that check an organization's security policies against NIST SP 800-53, show their evidence for every finding, and keep a tamper-evident record of who approved what.**

![Python](https://img.shields.io/badge/Python-3.12-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-agent%20pipeline-purple)
![Claude](https://img.shields.io/badge/Claude-Anthropic%20API-orange)
![Streamlit](https://img.shields.io/badge/Streamlit-dashboard-red)
![Docker](https://img.shields.io/badge/Docker-Cloud%20Run-2496ED)
[![Phase 2 tests](https://github.com/shreeja5060/hackathon/actions/workflows/phase2-tests.yml/badge.svg)](https://github.com/shreeja5060/hackathon/actions)
[![Phase 3 tests](https://github.com/shreeja5060/hackathon/actions/workflows/phase3-tests.yml/badge.svg)](https://github.com/shreeja5060/hackathon/actions)

Built for the **UpTempo AI Hackathon 2026, Use Case 2: Cybersecurity Governance & Compliance Intelligence.**
Team: Maryam Arief, Shreeja Singh, Anu Olawale and Mahsa.

---

## 🚀 Live demo

- **Demo video (5 minutes):** _add the link here_
- **Hosted app:** the link is in the submission form. It is kept out of this file because the demo runs on a team member's own API key.

> ⚠️ The first request after the app has been idle takes about a minute while the search model loads.

---

## 🚀 What this project does

This is not a chatbot that "reads" a policy. It is a pipeline of specialized agents that produce **checkable** findings, with a person in charge of every decision.

You upload a policy PDF. The system pulls out each requirement, finds the closest NIST control, rates how well the policy covers it, and shows the **exact policy sentence and the control text side by side**. The run then pauses. A named reviewer approves, edits or rejects each finding, a *different* person confirms it, and every step goes into a hash-chained ledger.

**Who it is for:** compliance and security teams that answer customer, audit and regulator requests; auditors who must verify someone else's work; and small suppliers with no security policy at all.

---

## ✨ Features

| Feature | Description |
|---|---|
| **Three-agent pipeline** | Extractor, Mapper and Auditor, each with one job and a validated output |
| **Evidence on every finding** | Policy quote, source document, section and page, control text, plain-language explanation, a suggested fix and questions for the reviewer |
| **Real human checkpoint** | A LangGraph pipeline that is genuinely suspended until a reviewer decides. Nothing is approved by default |
| **Two-person rule** | A different person must confirm each decision before the review can be signed off |
| **Tamper-evident ledger** | Every upload, analysis and decision is stored in a hash-chained record, so editing or deleting an old entry is detected |
| **Library with memory** | Every policy, version and review is kept, and survives a closed browser or a restarted server |
| **Cross-policy comparison** | Findings from different policies are compared by NIST control |
| **Assistant with verified citations** | Answers only from retrieved passages, numbers its sources, and has every citation checked in code |
| **Policy writer** | A few plain questions produce a starter policy where every rule is tied to a NIST control |
| **Swappable model backend** | One setting switches between the Anthropic API, Vertex AI and Gemini |
| **Simulator mode** | The whole dashboard runs on sample data with no AI calls, so every demo step is repeatable |
| **Defenses against bad input** | Policy text that tries to instruct the AI raises a warning; uploads are checked (PDF only, 10 MB) |
| **Cloud Run deployment** | One image builds the search index and runs the app; the ledger is streamed to Cloud Storage |
| **Tests and CI** | Automated tests run on every push using a fake AI, so they need no key |

---

## 🏗️ Architecture

```
  Policy PDF                      NIST SP 800-53 Rev. 5 (pinned OSCAL catalog)
      │                                          │
      └─────────────────┬────────────────────────┘
                        ▼
          ┌───────────────────────────┐
          │   Phase 1: Library        │   parse → chunk by section and page
          │   (Maryam)                │   → embed locally (BGE-small) → Chroma
          └─────────────┬─────────────┘
                        ▼
          ┌───────────────────────────┐
          │        Extractor          │   one section → each checkable requirement,
          │                           │   with the exact quote and its citation
          └─────────────┬─────────────┘
                        ▼
          ┌───────────────────────────┐
          │          Mapper           │   searches the library, shortlists 15 NIST
          │                           │   controls, picks the closest one
          └─────────────┬─────────────┘
                        ▼
          ┌───────────────────────────┐
          │          Auditor          │   reads the quote beside the full control text:
          │                           │   Full / Partial / Missing / Not observable,
          │                           │   plain words, a fix, questions for the reviewer
          └─────────────┬─────────────┘
                        ▼
          ┌───────────────────────────┐
          │   Human checkpoint        │   LangGraph pause: a named reviewer approves,
          │                           │   edits or rejects each finding
          └─────────────┬─────────────┘
                        ▼
          ┌───────────────────────────┐
          │   Second reviewer         │   a different person confirms each decision
          └─────────────┬─────────────┘
                        ▼
      Signed report (Markdown / JSON / CSV)

  Every step above is recorded in the ledger: a hash-chained SQLite record of who did what and when.
  The assistant searches the same library and answers with numbered, verified sources.
```

---

## 📊 Benchmarks

Measured on the three public-release sample policies. Each result says how it was measured and what it does **not** show.

**Speed, cost and integrity** (`phase2_agents/benchmark.py`, real library, Claude Sonnet 4.5, 6 sections)

| Metric | Result |
|---|---|
| Sections tested | 6 (2 had no checkable requirements) |
| Findings produced | 13 |
| Errors | 0 |
| Average time per finding | 11.2 seconds |
| Average time per section | 24.4 seconds |
| Share of time waiting on the AI | 97% |
| Estimated cost per finding | about $0.02 (at an assumed $3 / $15 per million tokens) |
| Citations that resolve to a real passage in the stated section | 13 / 13 |
| Cited controls that exist in the index | 11 / 11 |
| Controls from a different NIST book | 0 |
| Evaluation method | Objective checks against the index. No AI judge |

**Full run on all three policies** (`verify_citations.py`): 28 sections, 66 findings, **66 / 66 citations** and **64 / 64 cited controls** verified against the library.

**Reviewer spot-check** (`eval/spot_check_results.csv`): of 18 control matches, **5 were clearly the right topic, 12 were related but a different aspect, and 1 was off-topic.** One non-specialist reviewer. This is a topic-match check, **not expert accuracy**.

| Not yet measured | Why |
|---|---|
| Accuracy against an expert answer key | `eval/gold_set.json` is ready; it needs a security reviewer to fill it in |

Raw numbers are in `eval/BENCHMARKS.md`.

---

## 🧠 Design Decisions

**Why three agents instead of one big prompt?**
Each agent has one job and a validated output, so each can be tested and fixed on its own. A failure on one requirement is logged and the run continues: in one run, a single section out of 28 came back as broken JSON from Gemini, and the other 27 still finished.

**Why does the Mapper choose from a shortlist of 15 instead of naming any control?**
NIST 800-53 has more than a thousand controls and enhancements. The library retrieves the 15 closest by meaning, and the AI chooses among them. If it names something else, we look it up in the index by exact ID and refuse anything that is not a real control. That is why every control cited in our runs exists.

**Why only 800-53 controls in the shortlist?**
The library also holds NIST CSF 2.0. Early runs sometimes cited a CSF outcome such as `ID.RA-07` as if it were an 800-53 control (7 of 59 findings in one run). Filtering the shortlist to 800-53 brought that to 0.

**Why search small pieces but judge with the full text?**
Small fragments search well, but a reviewer needs the whole control. Each fragment links back to its original entry, and the Auditor reads that original with `get_original_chunk`.

**Why LangGraph, and why a real pause?**
The human step has to be a checkpoint inside the pipeline, not a button in the interface. `interrupt()` suspends the run until `submit_decisions` arrives. Undecided findings stay pending, so nothing is approved by default.

**Why a two-person rule and a hash-chained ledger?**
Because the question an auditor asks is "who approved this, and what did they see?" Each ledger entry carries a hash of the one before it, so a changed or deleted entry shows up when the trail is checked.

**Why no AI judge for quality?**
A model grading output like its own tends to score everything high and tells you very little. We measured what code can check: speed, cost, and whether every citation and control is real. Accuracy against an expert is listed as not measured, and the spot-check was done by a person.

**Why local embeddings?**
BGE-small runs locally with a pinned model revision, so search needs no API key, costs nothing, and gives the same results for everyone.

**Why a swappable model backend?**
Our Google project's Claude quota on Vertex AI was zero, so `shared/claude_client.py` switches between Anthropic, Vertex and Gemini with one setting. Each agent builds its own client: when they briefly shared one, our tests caught test doubles overwriting each other.

---

## 🤖 Components

**Extractor** (`phase2_agents/extractor.py`): reads one policy section and lists each checkable requirement, quoting the exact sentence. It ignores purpose statements and definitions.

**Mapper** (`phase2_agents/mapper.py`): searches the library for the closest 800-53 controls, removes repeats, and picks one. A pick that was not on the shortlist is verified by exact lookup or rejected.

**Auditor** (`phase2_agents/auditor.py`): reads the policy sentence beside the full control text and returns the coverage rating, a finding, a recommendation, a plain-language explanation and questions for the reviewer. If no control was found, it says "Not observable" instead of guessing.

**Pipeline** (`phase2_agents/pipeline.py`): the LangGraph graph with the human checkpoint. `start_review()` runs the agents and pauses; `submit_decisions()` resumes with the reviewer's choices.

**Ledger** (`shared/ledger.py`): the permanent record of documents, versions, findings and decisions, with the two-person rule and a tamper check.

**Assistant** (`qa_agent/`): a tool-using agent that searches the library and reads the current review. Any citation it did not actually retrieve is removed before the answer is shown.

**Dashboard** (`phase3_dashboard/`): Library, Analyze, Review, Sign-off, Assistant and Policy writer pages, plus the simulator.

**Library** (`phase1_ingestion/`): splits policies by section and page, loads NIST 800-53 Rev. 5 and CSF 2.0 from pinned official files checked by SHA-256, and serves `search()` and `get_original_chunk()`. It also indexes synthetic configuration, inventory and CloudTrail log evidence.

---

## 📁 Project Structure

```
hackathon/
├── phase1_ingestion/      # Library: parse_pdfs, chunk_policies, load_frameworks, chunk_and_embed, retriever
├── phase2_agents/         # extractor, mapper, auditor, pipeline, run_full_pipeline,
│                          # verify_citations, evaluate, benchmark
├── phase3_dashboard/      # Streamlit app: app.py, core/, backends/, ui/, tests/
├── qa_agent/              # the assistant: agent.py, retrieval.py
├── policy_writer/         # starter-policy drafting
├── shared/                # claude_client, gemini_client, ledger, fake_search, mcp demos
├── eval/                  # gold_set.json, BENCHMARKS.md, spot_check_results.csv
├── deploy/                # start.sh, litestream.yml, README.md (Cloud Run + Cloud Storage)
├── tests/                 # agents, ledger, citation checker, backends, benchmark
├── .github/workflows/     # CI for Phase 2 and Phase 3
├── Dockerfile             # builds the index and runs the dashboard
├── requirements.txt
└── .env.example           # copy to .env, never commit .env
```

---

## ⚡ Quick Start

Requires **Python 3.12**. Phase 1's pinned packages install on Linux and Apple Silicon (not Intel Macs); Cloud Shell works.

**Option 1: try the dashboard with no keys (simulator, sample data)**

```bash
git clone https://github.com/shreeja5060/hackathon.git
cd hackathon
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cd phase3_dashboard
streamlit run app.py
```

**Option 2: run it for real**

```bash
cp .env.example .env            # add ANTHROPIC_API_KEY

# build the library once (about 10 minutes; downloads the pinned NIST catalogs)
python phase1_ingestion/parse_pdfs.py
python phase1_ingestion/chunk_policies.py
python phase1_ingestion/download_frameworks.py
python phase1_ingestion/load_frameworks.py
python phase1_ingestion/chunk_and_embed.py
python -m phase1_ingestion.check_retriever      # should end with "All handoff checks passed."

cd phase3_dashboard
COPILOT_BACKEND=live streamlit run app.py       # or pick "Live pipeline" in the sidebar
```

**Option 3: Docker.** The image builds the search index, so the build takes several minutes.

```bash
docker build -t compliance-copilot .
docker run -p 8080:8080 -e ANTHROPIC_API_KEY=your_key compliance-copilot
```

Then open http://localhost:8080

**Run the agents from the command line**

```bash
python phase2_agents/run_full_pipeline.py                       # all sections -> full_gap_report.json
python phase2_agents/verify_citations.py full_gap_report.json   # is every citation and control real?
python phase2_agents/benchmark.py                               # speed, tokens, cost, integrity (about 50 cents)
```

### Model backends

Set `CLAUDE_BACKEND` in `.env` (see `shared/claude_client.py`):

| Value | Uses | Notes |
|---|---|---|
| `anthropic` (default) | Claude through the Anthropic API | Needs `ANTHROPIC_API_KEY` |
| `vertex` | Claude through Google Vertex AI | Built; our project's quota was zero, so we did not use it |
| `gemini` | Gemini on Vertex AI | Needs `GEMINI_MODEL`. Covers the three agents, not the assistant (it needs Claude's tool use) |

---

## 🧪 Testing

```bash
pytest tests/ -q                      # agents, citation checker, ledger, backends, benchmark
pytest phase3_dashboard/tests -q      # dashboard (run separately from the line above)
```

Both suites run on every push through GitHub Actions. They use a fake AI, so they need no key and cost nothing. They have caught real bugs: AI replies cut off mid-sentence, agents sharing one client, and tests that only passed on a machine without the real index.

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| LLM | Claude Sonnet 4.5 (Anthropic API); Gemini on Vertex AI as an option |
| Agent orchestration | LangGraph (human checkpoint with `interrupt()`) |
| Retrieval | Chroma, `BAAI/bge-small-en-v1.5` embeddings (local, pinned) |
| Frameworks | NIST SP 800-53 Rev. 5 and CSF 2.0 (official OSCAL, SHA-256 checked) |
| Dashboard | Streamlit |
| Memory | SQLite, hash-chained; streamed to Cloud Storage with Litestream |
| Deployment | Docker, Google Cloud Run, Cloud Storage, Secret Manager |
| CI | GitHub Actions |
| Testing | pytest |
| Also in the repo | an MCP server and client, and a LlamaIndex retriever (side demos, not in the main flow) |

---

## ⚠️ Honest limits

- The output is a **draft for human judgment**, not a certification or legal advice.
- **Accuracy against an expert has not been measured.** The spot-check above is a non-specialist topic check.
- The agents work from the policy outward, so a control that no policy mentions cannot appear as Missing. The Library lists baseline controls that no stored policy addresses.
- Reviewer names are **typed in**. Real sign-in needs Identity-Aware Proxy, which is supported but was not switched on for the demo.
- Environment evidence (configuration, inventory, logs) is searchable and citable but does not yet produce a compliance verdict.
- The AI varies between runs, so the same policy can map a requirement to a different control.
- Speed is limited by three AI calls in a row per requirement (97% of the time is waiting on the AI).
- The demo ran on a team member's personal API key.

**Verified:** after a deliberate restart of the Cloud Run service, the saved review and its audit trail came back intact from Cloud Storage.

---

## 🛣️ Roadmap

- [x] Three-agent pipeline with citations
- [x] Human checkpoint (LangGraph) and two-person rule
- [x] Hash-chained ledger, versions and permanent memory
- [x] Assistant with verified citations
- [x] Policy writer
- [x] Swappable backend (Anthropic, Vertex, Gemini)
- [x] Cloud Run deployment with the ledger kept in Cloud Storage
- [x] Tests and CI
- [x] Benchmark and reviewer spot-check
- [ ] Accuracy against an expert answer key
- [ ] Run requirements in parallel (speed)
- [ ] Reuse earlier results for unchanged sections (cost)
- [ ] Three-way check: framework, policy and live configuration
- [ ] Sign-in with Identity-Aware Proxy and per-company workspaces
- [ ] Cloud SQL ledger and Kubernetes

---

## 👥 Team

| Name | Area |
|---|---|
| Maryam Arief | Phase 1: library and retrieval |
| Shreeja Singh | Phase 2: agents, pipeline, ledger core, evaluation |
| Anu Olawale | Assistant and deployment |
| Mahsa | Phase 3: dashboard, memory, policy writer, demo story |
