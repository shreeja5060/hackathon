# Compliance Copilot 🛡️

An AI-powered agent that reads internal security policy documents, compares them
against official frameworks (starting with NIST), and produces a gap report
what's covered, what's partial, what's missing  with citations and a
human-in-the-loop approval step before anything is finalized.

Built for the UpTempo AI Hackathon 2026 - Use Case 2 (Cybersecurity Governance &
Compliance Intelligence).

## The problem

Checking whether an organization's policies satisfy a security framework like
NIST 800-53 is done manually today: expensive experts read every policy line by
line and cross-reference it against hundreds of controls. It takes weeks and
goes stale the moment a policy changes.

## The solution

Upload a policy → the system extracts its requirements → maps them to the
official framework → flags gaps (Full / Partial / Missing coverage) → drafts a
recommendation for each gap, with a citation back to the source text → a human
reviews and approves before anything is final. A chat interface lets you ask
questions like "what's our MFA policy?" and get answers with sources.

## Architecture

```
Policies + Frameworks + Configs (data/)
        │
        ▼
  Phase 1: Ingestion & RAG index      (phase1_ingestion/)
        │
        ▼
  Phase 2: Agents                     (phase2_agents/)
    Extractor → Mapper → Auditor
        │
        ▼
  Phase 3: Dashboard                  (phase3_dashboard/)
    upload · gap table · approve/reject · chat
        │
        ▼
  Phase 4: Demo & story               (phase4_demo/)
```

See `shared/` for prompts and utilities used across phases.

## Team & ownership

| Phase | Area | Owner |
|---|---|---|
| 1 | Data ingestion & RAG pipeline | Maryam (+ Shreeja pairing) |
| 2 | Agents (Extractor / Mapper / Auditor) | Shreeja |
| — | Chat box / Q&A agent + Tier 2/3 research | Anu |
| 3 | Dashboard | Mahsa |
| 4 | Demo, story, verification | Anu + team |

## Getting started

```bash
git clone <repo-url>
cd compliance-copilot
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then fill in your ANTHROPIC_API_KEY
```

## Branching workflow

- `main` always stays working — it's what we demo from.
- Branch per person/feature: `phase1-maryam`, `phase2-shreeja`, `phase3-mahsa`, `phase4-anu`.
- Commit often, open a PR into `main`, one teammate reviews before merging.
- **Never commit secrets.** Real keys go in `.env` (gitignored); `.env.example` shows the shape only.

## Roadmap tiers

- **Tier 1 (MVP, required):** policy vs. NIST framework gap analysis, with citations and human approval.
- **Tier 2 (stretch):** + sample environment config file → three-way check (NIST ↔ policy ↔ config).
- **Tier 3 (stretch):** + sample log excerpt → shows a real enforcement gap (e.g. policy + config require MFA, logs show a bypass).
