# Phase 1 — Policy and NIST retrieval

**Owner:** Maryam Arief. This module prepares the evidence used by Shreeja's
agents and Anu's Q&A agent.

## Current scope

- Three public-release policy PDFs in `data/policies/`.
- NIST SP 800-53 Revision 5, Release 5.2.0, in OSCAL JSON.
- NIST Cybersecurity Framework (CSF) 2.0, in OSCAL JSON.

Both catalogs are included. Configurations, logs, and asset inventory are
stretch goals; their ingestion is not implemented in this MVP. The current
scripts assign document kinds from the known inputs; AI classification at
upload time is not part of this module.

The framework downloader pins its files to NIST OSCAL Content commit
[`78650f02ad9321bb7b817846f8fbd4f2bcd620de`](https://github.com/usnistgov/oscal-content/tree/78650f02ad9321bb7b817846f8fbd4f2bcd620de)
and validates their SHA-256 hashes. Framework versions and OSCAL/catalog
serialization versions are recorded separately in the generated source records.

## Build on another machine

The commands below are for macOS/Linux, from the repository root, on the
branch containing the Phase 1 scripts. Python 3.12.6, Chroma 1.5.9, and
Sentence Transformers 6.1.0 were used for the successful local run. Direct
Phase 1 dependency versions are recorded in the root `requirements.txt`.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

python phase1_ingestion/parse_pdfs.py
python phase1_ingestion/chunk_policies.py
python phase1_ingestion/download_frameworks.py
python phase1_ingestion/load_frameworks.py
python phase1_ingestion/chunk_and_embed.py
python -m phase1_ingestion.check_retriever
```

The first build needs internet to download the pinned NIST catalogs and the
embedding model. Embeddings are computed locally; no Claude API key is needed
for Phase 1. The retriever loads the model from the local cache established by
the indexing step.

To print sample policy and framework search results:

```bash
python -m phase1_ingestion.retriever
```

Generated catalog downloads, processed JSON, and Chroma files are rebuilt
locally, rather than included in the pull request. Keep the original chunk
JSON files after indexing: the full-entry lookup uses them.

## Pipeline and generated files

| Script | Main outputs |
| --- | --- |
| `parse_pdfs.py` | `data/processed/policy_pages.json`, with one-based PDF pages |
| `chunk_policies.py` | `data/processed/policy_chunks.json` and `policy_sources.json` |
| `download_frameworks.py` | Pinned catalogs and `framework_sources.json` in `data/frameworks/` |
| `load_frameworks.py` | `data/processed/framework_chunks.json` and `framework_parse_report.json` |
| `chunk_and_embed.py` | `data/processed/indexed_chunks.json`, Chroma collection, and `chroma_db/index_manifest.json` |

Policy chunks follow numbered sections and page boundaries when that split
keeps the document's text. Other layouts fall back to one chunk per page,
with a `Page N` locator and the complete extracted page text. Framework entries retain
control/outcome identifiers and distinguish requirements from explanatory
discussion or implementation examples. Withdrawn framework entries are
excluded, and unresolved organizational choices remain explicit placeholders.

The index uses `BAAI/bge-small-en-v1.5`, pinned to model commit
`5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`. Long entries are split using the
model's tokenizer, with a 480-token limit including context and section labels.
The splitter uses overlapping words while retaining the original source and
locator. Queries use the model instruction saved in the index manifest.

The collection name depends on the prepared records and index settings.
Rerunning with the same inputs upserts the same IDs; changed inputs/settings
select a different collection. The manifest switches to the completed
collection after all records are stored and the count matches. Older
collections remain on disk. Retrieval follows the active manifest.

## Unfamiliar policy layouts

The chunker accepts unnumbered headings, Roman headings and numbered lists
of requirements using a page-based fallback. It also uses this fallback when
introductory text would otherwise be left out, or a numbered heading contains
text that section splitting would discard. It preserves the extracted text
and actual page numbers instead of guessing section names.

The three supplied templates keep their 28 section chunks and existing IDs.
Only their unchanged publication preambles and empty parent headings remain
metadata-only; the preambles are retained in `source_context`. New or modified
introductions remain in the chunks. Source records include
`chunking_strategy`, either `numbered_sections` or `pages`.

Run the offline checks without changing processed files or the Chroma index:

```bash
python -m phase1_ingestion.check_policy_ingestion
```

The checks generate temporary synthetic PDFs, verify full fallback text and
page citations, test blank/scanned-page and password-protected-file rejection,
and check the original templates. They require PyMuPDF, but no embedding model
or Claude API key. They do not measure agent or compliance accuracy.

To inspect another policy PDF without adding it to the index:

```bash
python -m phase1_ingestion.check_policy_ingestion --pdf "/path/to/another_policy.pdf"
```

These inputs are treated as policies because they were selected as policies;
document classification and OCR are not implemented. A page with no extracted
text raises an error rather than producing a partial document. Review reading
order manually for complex layouts such as columns and tables.

Mahsa's live dashboard upload backend calls this same `chunk_pages()` function.
Its existing `test_uploads_are_split_by_phase1s_chunker` test should now expect
an unnumbered PDF to return a `Page 1` chunk, instead of expecting rejection.
Uploaded files still need indexing before they are available to document search;
this change does not add an upload-to-index step.

## Search interface

Run callers from the repository root so the package import resolves:

```python
from phase1_ingestion.retriever import search, get_original_chunk

policy_hits = search(
    "Can employees share passwords or authentication tokens?",
    type="internal",
    top_k=3,
)

framework_hits = search(
    "What requirements call for multifactor authentication?",
    type="framework",
    top_k=5,
)

if framework_hits:
    # Use the chosen candidate's chunk_id after the Mapper selects it.
    example_full_entry = get_original_chunk(framework_hits[0]["chunk_id"])
```

`search(query, type=None, top_k=5)` returns up to `top_k` results in descending
similarity order. `type=None` searches all indexed roles. The roles are
`internal`, `framework`, and `evidence`; the current index has no `evidence`
records, so that filter returns an empty list. Invalid roles, empty queries,
invalid result counts, and queries exceeding the model's input limit raise
errors instead of silently changing the request.

Every search result has exactly these eight fields:

| Field | Value |
| --- | --- |
| `chunk_id` | String identifying the exact indexed passage |
| `text` | Complete text of that passage |
| `source` | Original filename, linking to its source record |
| `page` | One-based PDF page number, or `None` |
| `type` | `internal`, `framework`, or `evidence` |
| `doc_kind` | Descriptive string; currently `policy`, `control_catalog`, or `cybersecurity_framework` |
| `locator` | Section heading, fallback page label, control identifier, or `None` |
| `score` | Cosine similarity to this query, from -1 to 1; higher is closer |

`None` serializes as JSON `null`; `page` and `locator` are never omitted from
search results. `score` is computed for each query, not stored as a permanent
chunk attribute. It is not a confidence or compliance percentage. A fixed
relevance cutoff has not been calibrated. The example CLI shortens previews
to 400 characters; the function returns full passage text.

## Original entries and source provenance

A split fragment has an internal `parent_chunk_id` pointing to its complete
entry in `policy_chunks.json` or `framework_chunks.json`. Unsplit entries keep
their original IDs and need no parent field.

`get_original_chunk(indexed_chunk_id)` looks up that exact indexed record and
resolves its parent, if present. It returns the original seven-field chunk
record, without a search score. The returned ID is the original entry's ID.
The original entry is a policy section/page chunk or a framework control or
outcome, not the entire PDF or catalog. Unknown IDs and missing/mismatched
original records raise errors. Rebuild the pipeline after changing inputs;
do not independently replace the processed originals behind an active index.

The eight-field search response does not expose `parent_chunk_id`.

Versions, publication dates, download provenance, and policy publication
context are stored once per source in `index_manifest.json` under `sources`.
Use `sources[hit["source"]]` to obtain that source's record. Policy dates
omitted from the public templates remain unknown; do not infer a revision date
from the file timestamp.

## Agent integration

- Replace the placeholder import with `from phase1_ingestion.retriever import search`.
- Replace old `type="policy"` calls with `type="internal"`. A policy's
  specific kind is `doc_kind="policy"`. All current internal inputs are policies.
- The Mapper should keep the exact framework candidate's `chunk_id` alongside
  its control identifier and validate that its selection belongs to the
  retrieved candidates. The Auditor can call `get_original_chunk()` with
  that ID to obtain the complete chosen entry. Searching a control identifier
  again and taking the first similarity result does not guarantee that same
  control.
- For extraction across a whole policy, iterate that policy's original records
  in `data/processed/policy_chunks.json`. A single top-k search only supplies a
  selection of passages.

## Verified local results

Maryam's local run on 2026-09-30 produced:

| Input | Original entries | Indexed passages |
| --- | ---: | ---: |
| Three policy PDFs | 28 | 28 |
| NIST SP 800-53 | 1,014 | 1,107 |
| NIST CSF 2.0 | 106 | 106 |
| **Total** | **1,148** | **1,241** |

All 1,241 original-entry links were checked; 153 indexed fragments have a
parent ID. The largest indexed passage was 480 tokens.

The password-sharing query ranked Computer Security Policy, Section 3.5,
page 2 first. The MFA query returned IA-2(2), IA-2(1), and IA-2(6). The
handoff check passed for both catalogs, the eight-field response, unfiltered
and role-filtered searches, the empty evidence filter, and exact original-entry
lookup for a split fragment.

These are interface, provenance, and sample retrieval checks. They do not
establish overall retrieval accuracy or validate the agents' compliance
findings. Top-k results can contain weak matches, and absence from search
results is not proof that a policy lacks a requirement. The policy PDFs are
sanitized public templates; preserve their publication context when assessing
missing detail or outdated content.
