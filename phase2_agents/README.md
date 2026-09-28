# Phase 2 — Agents

**Owner:** Shreeja

**Goal:** three agents built on top of Phase 1's retriever:
1. **Extractor** — pulls discrete requirements out of a policy, with source citation
2. **Mapper** — matches each requirement to the relevant NIST control(s)
3. **Auditor** — classifies coverage (Full / Partial / Missing / Not observable),
   flags outdated or ambiguous language, and drafts a recommendation

**Done when:** feeding in one policy produces a structured gap report (JSON) the
dashboard can render directly — matching the brief's example table:

| Requirement | Coverage | Finding | Recommendation |

## Suggested files
- `extractor.py`
- `mapper.py`
- `auditor.py`
- `orchestrator.py` — runs the three in sequence (plain Python loop or LangGraph)
- `prompts/` — the actual prompt text for each agent, kept separate from logic
