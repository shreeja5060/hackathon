# Phase 3 — Dashboard

**Owner:** Mahsa

**Goal:** a UI for the whole flow: upload a policy → watch it analyze → see the
gap table → click a finding to see its proof → approve/reject each finding →
export the approved report → ask the chat box a question.

**Default plan:** Streamlit (pure Python, fastest to build, safest for a
2-week hackathon). If a React build proves solid by end of Day 9, it can
replace this — but the checkpoint exists precisely so we're never stuck.

## Suggested files (Streamlit path)
- `app.py` — main Streamlit entrypoint
- `components/gap_table.py`
- `components/chat.py`

## If going React
- `frontend/` — Vite + React app
- Calls a small FastAPI backend (`api.py`, can live here or in `shared/`) that
  wraps Phase 1/2's Python functions as endpoints.
