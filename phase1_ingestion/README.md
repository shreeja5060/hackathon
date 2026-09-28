# Phase 1 — Ingestion & RAG index

**Owner:** Maryam (+ Shreeja pairing on RAG design)

**Goal:** parse the policy PDFs and NIST framework files, chunk them, embed them,
and store them in a searchable vector index (Chroma). Everything downstream
(Phase 2 agents, the Phase 3 chat box) reads from what's built here.

**Done when:** a search for e.g. "password rules" returns the right chunk from
the right document, with metadata (source file, artifact type: policy /
framework / config / log) attached.

## Suggested files
- `parse_pdfs.py` — extract text from `data/policies/`
- `load_frameworks.py` — load NIST OSCAL JSON from `data/frameworks/`
- `chunk_and_embed.py` — split text, generate embeddings, write to Chroma
- `retriever.py` — a simple `search(query, filters=...)` function other phases import
