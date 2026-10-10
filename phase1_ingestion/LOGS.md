# Phase 1 security-log ingestion

`load_logs.py` reads a supplied AWS CloudTrail JSON file with a nonempty
`Records` array. It also accepts the same JSON compressed as `.json.gz`.
Each record becomes an original evidence chunk with `type="evidence"`,
`doc_kind="security_log"`, `page=null`, and an exact `/Records/N` locator.
The public eight-field `search()` interface is unchanged.

The parser retains the complete supplied event, including extra fields from
new CloudTrail minor versions. It requires a version of `1.x`, an event ID,
event time, event name and service. It rejects duplicate IDs, duplicate JSON
fields, invalid field shapes, missing timezone information and empty inputs.
Parsing must finish before the generated bundle is replaced.

Recorded actors, resources, results and MFA values are evidence from this
file. Missing values stay unknown. A ConsoleLogin `Success`/`Failure` or an
explicit error is rendered as a reported result; API events without an
explicit result stay unknown. Conflicting success/error fields also stay
unknown. `MFAUsed` belongs to the supplied console-login event, not a permanent
account setting or proof of successful access. Other MFA/session fields are
preserved in the complete record without being substituted for `MFAUsed`.

Source metadata contains the original file hash, declared environment,
synthetic label, event count and first/last observed event times. Those times
are not a statement of complete log coverage. No identity link to a
configuration or inventory entry is inferred, and no compliance verdict is
created. The module does not connect to AWS or call Claude.

The format follows AWS's documentation:

- [CloudTrail event record fields](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-event-reference-record-contents.html)
- [Console sign-in events](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-event-reference-aws-console-sign-in-events.html)
- [CloudTrail log file examples](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-log-file-examples.html)

## Load a supplied file

Run from the repository root with the working Python 3.12 environment active:

```bash
python -m phase1_ingestion.load_logs \
  --input "/path/to/supplied_logs.json" \
  --environment hackathon-demo \
  --synthetic

python phase1_ingestion/chunk_and_embed.py
python -m phase1_ingestion.check_logs --index
```

Use `--synthetic` for synthetic samples. The environment is an explicit caller
label, not something discovered from the events. The parser does not verify
the authenticity of a supplied log file. The bundle is written to the ignored
`data/processed/logs_bundle.json`; rerunning replaces that generated bundle.
Indexing includes this bundle alongside existing configuration and inventory
evidence. Without a log bundle the indexer keeps its previous behavior.

The indexer's existing saved-original snapshot preserves every complete log
entry. `get_original_chunk()` resolves a split log passage to that entry.
Rebuild after replacing a bundle; retrieval continues to use the active
index's saved originals until the new index is published.

## Check the ingestion code

```bash
python -m phase1_ingestion.check_logs
```

Nine checks cover JSON pointers, hashes, full-record preservation, unknown
values, duplicate/malformed inputs, gzip, timezone offsets, retained minor
version fields, failed-load preservation and index/original-lookup wiring.
The index-wiring check uses the actual Phase 1 code with model and Chroma test
doubles in a temporary directory. It verifies both coexistence with the
configuration/inventory bundles and removal of old logs from a rebuilt index.
It does not measure embedding or retrieval quality. These offline checks
need only Python's standard library and do not modify the working index.

To check real indexing without waiting for a sample file, generate four small
synthetic parser fixtures in the ignored processed directory:

```bash
python -m phase1_ingestion.check_logs --prepare-demo
python phase1_ingestion/chunk_and_embed.py
python -m phase1_ingestion.check_logs --index
python -m phase1_ingestion.check_environment --index
python -m phase1_ingestion.check_inventory --index
python -m phase1_ingestion.check_retriever
python -m phase1_ingestion.evaluate_retrieval
```

The fixtures contain two successful console logins with different recorded
MFA values, a failed login with missing MFA data, and an UpdateTrail record
with an unspecified result. They are tests of this loader, not the project's
research/sample-log deliverable. The fixture file and bundle are gitignored;
no new sample dataset is committed.

`--index` verifies the active log source metadata, exact lookup for every
indexed log passage, and four top-three development retrieval questions when
the generated fixtures are used. Passage counts can grow by more than four
because long records may split. These checks measure retrieval and provenance,
not an agent's interpretation or compliance accuracy. The same command can
check a supplied file's bundle; it uses one identifying query per event.

To exclude logs again, remove only the generated `logs_bundle.json` and rebuild
the index. The raw source file is retained.
