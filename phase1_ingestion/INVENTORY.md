# Synthetic asset inventory extension

This adds six inventory entries for the same resources as the configuration demo.
Alex is declared privileged, Sam non-privileged, and Jordan's owner and privilege
are unknown. Other resources use not-applicable account privilege. These are new,
explicit synthetic fixture assumptions, not discovered real permissions.

Each entry carries owner, purpose, lifecycle status, environment and snapshot
time, with a link to its configuration source and JSON Pointer. Identity is
matched using resource type plus resource ID within the same environment. The
parser rejects mismatched snapshots, duplicate and unlinked resources. This MVP
requires identical timestamps; a real inventory integration would need an
explicit freshness policy. The indexer checks that the generated inventory and
configuration bundles refer to the same configuration file hash.

Run with the virtual environment active from the repository root:

```bash
python -m phase1_ingestion.load_environment
python -m phase1_ingestion.load_inventory
python -m phase1_ingestion.check_inventory
python phase1_ingestion/chunk_and_embed.py
python -m phase1_ingestion.check_inventory --index
python -m phase1_ingestion.check_environment --index
python -m phase1_ingestion.evaluate_retrieval
```

The updated environment check counts configuration records by source, allowing
other evidence types to coexist. Inventory uses type=evidence and
doc_kind=asset_inventory; the eight-field search interface is unchanged. Exact
original lookup works through the index's saved original records. No new package
or Claude API key is needed. Six new chunks should bring the existing demo index
from 1,247 to 1,253 if all other input files are unchanged.

Generated `data/processed/inventory_bundle.json` is ignored by Git. To exclude
inventory, remove only that generated bundle and rebuild the index.

Inventory provides declared context, not proof of effective permissions,
configuration or control applicability. A privileged label does not prove which
organizational NIST requirements apply. The separate phase2_integration prototype
has not been updated to consume this inventory; its existing applicability caveat
remains valid. Security-log ingestion is documented separately in [LOGS.md](LOGS.md).

Local validation completed: parser/source-link checks, preservation of unknown
values, malformed fixture rejection, and Python syntax. Live Chroma retrieval
needs to be checked with the user's local index using the commands above. The six
new retrieval queries intentionally search all evidence, including configuration,
to check competition between evidence types. Report misses for review.
