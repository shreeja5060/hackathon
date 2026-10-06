"""Offline log-ingestion checks; --prepare-demo writes ignored test inputs.

Add --index after rebuilding to check real Chroma lookup and retrieval.
Offline index-wiring tests use doubles for the embedding model and Chroma.
No test connects to AWS or calls Claude.
"""

import argparse
import contextlib
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from . import load_logs


ROOT = Path(__file__).resolve().parents[1]
DEMO_INPUT = ROOT / "data/processed/synthetic_security_logs.json"
CHUNK_FIELDS = {"chunk_id", "text", "source", "page", "type", "doc_kind", "locator"}
RAW_LABEL = "Complete supplied CloudTrail record:\n"


def demo_records():
    """Small parser fixtures, not the team's research/sample-log deliverable."""
    records = []
    for index, (name, result, mfa) in enumerate([
        ("demo-user-alex", "Success", "No"),
        ("demo-user-sam", "Success", "Yes"),
        ("demo-user-jordan", "Failure", None),
    ]):
        record = {
            "eventVersion": "1.08", "eventID": f"demo-login-{index}",
            "eventTime": f"2026-09-30T17:5{index}:00Z",
            "eventSource": "signin.amazonaws.com", "eventName": "ConsoleLogin",
            "userIdentity": {"type": "IAMUser", "userName": name,
                             "arn": f"arn:aws:iam::000000000000:user/{name}"},
            "responseElements": {"ConsoleLogin": result},
            "additionalEventData": {} if mfa is None else {"MFAUsed": mfa},
        }
        if result == "Failure":
            record["errorMessage"] = "Synthetic failed authentication"
        records.append(record)
    records.append({
        "eventVersion": "1.08", "eventID": "demo-trail-change",
        "eventTime": "2026-09-30T17:53:00Z",
        "eventSource": "cloudtrail.amazonaws.com", "eventName": "UpdateTrail",
        "userIdentity": {"type": "IAMUser", "userName": "demo-user-alex"},
        "resources": [{"ARN": "arn:aws:cloudtrail:us-east-1:000000000000:trail/demo-trail",
                       "type": "AWS::CloudTrail::Trail"}],
        "requestParameters": {"name": "demo-trail", "isMultiRegionTrail": False},
        "responseElements": None,
    })
    return {"Records": records}


class LogIngestionChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "fixture.json"

    def parse(self, data=None):
        self.path.write_text(json.dumps(demo_records() if data is None else data), encoding="utf-8")
        return load_logs.parse_logs(self.path, environment="hackathon-demo", synthetic=True)

    def test_exact_record_pointers_hash_schema_and_time_scope(self):
        data = demo_records()
        chunks, sources = self.parse(data)
        self.assertEqual(len(chunks), 4)
        self.assertEqual(len({chunk["chunk_id"] for chunk in chunks}), 4)
        for index, chunk in enumerate(chunks):
            self.assertEqual(set(chunk), CHUNK_FIELDS)
            self.assertEqual(chunk["source"], self.path.name)
            self.assertEqual(chunk["locator"], f"/Records/{index}")
            self.assertEqual(chunk["type"], "evidence")
            self.assertEqual(chunk["doc_kind"], "security_log")
            self.assertIsNone(chunk["page"])
            self.assertIn("SYNTHETIC DEMO SECURITY LOG", chunk["text"])
            raw_record = json.loads(chunk["text"].split(RAW_LABEL, 1)[1])
            self.assertEqual(raw_record, data["Records"][index])
        source = sources[self.path.name]
        self.assertEqual(source["sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(source["event_count"], 4)
        self.assertEqual(source["observed_first_event"], "2026-09-30T17:50:00+00:00")
        self.assertEqual(source["observed_last_event"], "2026-09-30T17:53:00+00:00")
        self.assertEqual(source["collection_coverage"], "unknown")

    def test_success_failure_missing_mfa_and_unspecified_api_result(self):
        chunks, _ = self.parse()
        for chunk, result, mfa in zip(
            chunks, ["success", "success", "failure", load_logs.UNKNOWN],
            ["false", "true", load_logs.UNKNOWN, load_logs.UNKNOWN], strict=True,
        ):
            self.assertIn(f"Reported result: {result}\n", chunk["text"])
            self.assertIn(f"Reported MFAUsed for this event: {mfa}\n", chunk["text"])
        self.assertIn('"isMultiRegionTrail": false', chunks[3]["text"])
        self.assertIn("demo-trail", chunks[3]["text"])

    def test_conflicting_results_and_unrecognized_mfa_stay_unknown(self):
        data = demo_records()
        data["Records"][0]["errorCode"] = "SyntheticError"
        data["Records"][0]["additionalEventData"]["MFAUsed"] = "not-recorded"
        chunks, _ = self.parse(data)
        self.assertIn("Reported result: unknown / conflicting", chunks[0]["text"])
        self.assertIn(f"Reported MFAUsed for this event: {load_logs.UNKNOWN}", chunks[0]["text"])
        self.assertIn("not-recorded", chunks[0]["text"])

    def test_missing_actor_target_and_mfa_are_not_inferred(self):
        data = demo_records()
        data["Records"][0]["userIdentity"] = {
            "userName": "   ", "sessionContext": {"attributes": {"mfaAuthenticated": "true"}}
        }
        data["Records"][0]["additionalEventData"] = {}
        chunks, _ = self.parse(data)
        self.assertIn(f"Recorded userName: {load_logs.UNKNOWN}", chunks[0]["text"])
        self.assertIn(f"Recorded resource targets: {load_logs.UNKNOWN}", chunks[0]["text"])
        self.assertIn(f"Reported MFAUsed for this event: {load_logs.UNKNOWN}", chunks[0]["text"])
        self.assertIn('"mfaAuthenticated": "true"', chunks[0]["text"])

    def test_gzip_and_canonical_ids_preserve_all_fields(self):
        chunks, _ = self.parse()
        self.path.write_text(json.dumps(demo_records(), sort_keys=True), encoding="utf-8")
        reordered, _ = load_logs.parse_logs(self.path, environment="hackathon-demo", synthetic=True)
        self.assertEqual(reordered, chunks)
        compressed = self.root / "fixture.json.gz"
        compressed.write_bytes(gzip.compress(self.path.read_bytes()))
        gz_chunks, sources = load_logs.parse_logs(compressed, environment="hackathon-demo", synthetic=True)
        self.assertEqual(len(gz_chunks), 4)
        self.assertEqual(sources[compressed.name]["sha256"], hashlib.sha256(compressed.read_bytes()).hexdigest())
        for index, chunk in enumerate(gz_chunks):
            self.assertEqual(json.loads(chunk["text"].split(RAW_LABEL, 1)[1]), demo_records()["Records"][index])

    def test_duplicate_malformed_and_unsupported_events_are_rejected(self):
        bad_inputs = [{"Records": []}, [], {"Records": [None]}]
        duplicate = demo_records()
        duplicate["Records"].append(copy.deepcopy(duplicate["Records"][0]))
        bad_inputs.append(duplicate)
        for field, value in [
            ("eventID", ""), ("eventTime", "2026-09-30T17:50:00"),
            ("eventTime", "not-a-time"), ("eventVersion", "2.0"),
            ("userIdentity", "not-an-object"), ("resources", ["not-an-object"]),
            ("responseElements", []), ("errorCode", False),
        ]:
            item = demo_records()
            item["Records"][0][field] = value
            bad_inputs.append(item)
        for data in bad_inputs:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    self.parse(data)
        for raw in ('{"Records": [], "Records": []}', '{"Records": NaN}'):
            self.path.write_text(raw, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_logs.parse_logs(self.path, environment="hackathon-demo", synthetic=True)

    def test_timezone_offsets_and_new_minor_fields_are_preserved(self):
        data = demo_records()
        data["Records"][0].update(eventTime="2026-09-30T11:50:00-06:00", eventVersion="1.99")
        data["Records"][0]["newMinorField"] = {"recorded": "kept exactly"}
        chunks, sources = self.parse(data)
        self.assertEqual(sources[self.path.name]["observed_first_event"], "2026-09-30T17:50:00+00:00")
        self.assertIn("kept exactly", chunks[0]["text"])
        self.assertIn("11:50:00-06:00", chunks[0]["text"])

    def test_failed_cli_load_keeps_previous_bundle(self):
        output = self.root / "logs_bundle.json"
        output.write_text("previous bundle", encoding="utf-8")
        data = demo_records()
        data["Records"][3]["eventTime"] = "invalid"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        arguments = ["load_logs", "--input", str(self.path), "--environment", "hackathon-demo", "--synthetic"]
        with patch.object(load_logs, "BUNDLE_PATH", output), patch.object(sys, "argv", arguments):
            with self.assertRaises(ValueError):
                load_logs.main()
        self.assertEqual(output.read_text(encoding="utf-8"), "previous bundle")

    def test_index_bundle_wiring_and_fragment_original_lookup(self):
        # Exercise the actual indexer and retriever with isolated test doubles.
        # These check wiring and provenance, not BGE embeddings or ranking.
        class Array:
            def __init__(self, rows):
                self.rows, self.shape = rows, (len(rows), 2)

            def __getitem__(self, selection):
                return Array(self.rows[selection])

            def tolist(self):
                return self.rows

        class Model:
            max_seq_length = 512

            def __init__(self, *args, **kwargs):
                self.tokenizer = lambda text, **kwargs: {"input_ids": [0] * (len(text.split()) + 2)}

            def encode(self, texts, **kwargs):
                return Array([[1.0, 0.0] for _ in texts])

        class Collection:
            def __init__(self):
                self.records = {}

            def upsert(self, ids, documents, metadatas, embeddings):
                self.records.update({key: (text, meta) for key, text, meta in zip(ids, documents, metadatas, strict=True)})

            def count(self):
                return len(self.records)

            def get(self, ids=None, include=None):
                selected = list(self.records) if ids is None else [key for key in ids if key in self.records]
                return {"ids": selected, "documents": [self.records[key][0] for key in selected],
                        "metadatas": [self.records[key][1] for key in selected]}

        collections = {}

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def get_or_create_collection(self, name, **kwargs):
                return collections.setdefault(name, Collection())

            def get_collection(self, name, **kwargs):
                return collections[name]

            def get_max_batch_size(self):
                return 128

        def module_from_file(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        doubles = {"chromadb": types.SimpleNamespace(PersistentClient=Client),
                   "sentence_transformers": types.SimpleNamespace(SentenceTransformer=Model)}
        with patch.dict(sys.modules, doubles):
            indexer = module_from_file("log_index_check", ROOT / "phase1_ingestion/chunk_and_embed.py")
            retriever = module_from_file("log_retriever_check", ROOT / "phase1_ingestion/retriever.py")
        indexer.ROOT, indexer.PROCESSED, indexer.DB_PATH = self.root, self.root / "data/processed", self.root / "chroma_db"
        retriever.DB_PATH, retriever.MANIFEST_PATH = indexer.DB_PATH, indexer.DB_PATH / "index_manifest.json"
        from .load_environment import DEFAULT_INPUT, parse_environment
        from .load_inventory import parse_inventory
        configuration, config_sources = parse_environment(DEFAULT_INPUT)
        inventory, inventory_sources = parse_inventory()
        policy = {"chunk_id": "policy-test", "text": "1 Access\nStaff must use MFA.", "source": "Test Policy.pdf",
                  "page": 1, "type": "internal", "doc_kind": "policy", "locator": "Section 1 Access"}
        framework = {**policy, "chunk_id": "framework-test", "source": "Test Catalog.json",
                     "page": None, "type": "framework", "doc_kind": "control_catalog", "locator": "IA-2"}
        data = demo_records()
        data["Records"][3]["testLongField"] = "recorded detail " * 900
        logs, log_sources = self.parse(data)
        for name, value in [
            ("policy_chunks.json", [policy]), ("framework_chunks.json", [framework]),
            ("policy_sources.json", {policy["source"]: {}}),
            ("environment_bundle.json", {"chunks": configuration, "sources": config_sources}),
            ("inventory_bundle.json", {"chunks": inventory, "sources": inventory_sources}),
            ("logs_bundle.json", {"chunks": logs, "sources": log_sources}),
        ]:
            indexer.save_json(indexer.PROCESSED / name, value)
        indexer.save_json(self.root / "data/frameworks/framework_sources.json", {framework["source"]: {}})
        with contextlib.redirect_stdout(io.StringIO()):
            indexer.main()
        manifest, collection = retriever._index()
        expected = [policy, framework] + configuration + inventory + logs
        self.assertEqual(manifest["input_chunk_count"], len(expected))
        self.assertEqual(manifest["sources"][self.path.name], log_sources[self.path.name])
        lookup = {record["chunk_id"]: record for record in expected}
        log_fragments = []
        for key, (_, metadata) in collection.records.items():
            original_id = metadata.get("parent_chunk_id", key)
            self.assertEqual(retriever.get_original_chunk(key), lookup[original_id])
            if metadata["doc_kind"] == "security_log" and "parent_chunk_id" in metadata:
                log_fragments.append(key)
        self.assertTrue(log_fragments, "Long log record was not split")
        indexer.save_json(indexer.PROCESSED / "logs_bundle.json", {"chunks": [], "sources": {}})
        self.assertEqual(retriever.get_original_chunk(log_fragments[0]), logs[3])
        with contextlib.redirect_stdout(io.StringIO()):
            indexer.main()
        new_manifest, _ = retriever._index()
        self.assertEqual(new_manifest["input_chunk_count"], len(expected) - len(logs))
        self.assertNotIn(self.path.name, new_manifest["sources"])
        with self.assertRaises(KeyError):
            retriever.get_original_chunk(log_fragments[0])


def prepare_demo():
    DEMO_INPUT.parent.mkdir(parents=True, exist_ok=True)
    DEMO_INPUT.write_text(json.dumps(demo_records(), indent=2) + "\n", encoding="utf-8")
    chunks, sources = load_logs.parse_logs(DEMO_INPUT, environment="hackathon-demo", synthetic=True)
    load_logs.write_bundle(chunks, sources)
    print(f"Prepared {len(chunks)} synthetic test events: {DEMO_INPUT}")
    print(f"Prepared log bundle: {load_logs.BUNDLE_PATH}")


def check_index():
    from .retriever import _index, get_original_chunk, search
    bundle = json.loads(load_logs.BUNDLE_PATH.read_text(encoding="utf-8"))
    manifest, collection = _index()
    for source, metadata in bundle["sources"].items():
        if manifest["sources"].get(source) != metadata:
            raise AssertionError("Log source differs from the active index. Rebuild first.")
    records = collection.get(where={"doc_kind": "security_log"}, include=["metadatas"])
    originals = {record["chunk_id"]: record for record in bundle["chunks"]}
    found = set()
    for key in records["ids"]:
        original = get_original_chunk(key)
        if original["chunk_id"] not in originals or original != originals[original["chunk_id"]]:
            raise AssertionError("Indexed log has the wrong original")
        found.add(original["chunk_id"])
    if found != set(originals):
        raise AssertionError("Some log originals are missing from the index")
    print("PASS: indexed log sources and every exact original lookup")
    misses = []
    for original in bundle["chunks"]:
        raw = json.loads(original["text"].split(RAW_LABEL, 1)[1])
        actor = (raw.get("userIdentity") or {}).get("userName", "")
        query = f"Security log event {raw['eventName']} {raw['eventID']} {actor} {raw['eventTime']}"
        hits = search(query, type="evidence", top_k=3)
        if any(set(hit) != CHUNK_FIELDS | {"score"} or hit["type"] != "evidence" for hit in hits):
            raise AssertionError("Search contract changed")
        rank = next((i for i, hit in enumerate(hits, 1) if (
            hit["source"] == original["source"] and hit["locator"] == original["locator"]
            and hit["doc_kind"] == "security_log"
        )), None)
        print(f"{'HIT' if rank else 'MISS'} {raw['eventID']}: rank {rank}")
        if rank is None:
            misses.append(raw["eventID"])
    if misses:
        raise AssertionError("Log retrieval misses need review: " + ", ".join(misses))
    print("PASS: log top-three retrieval checks (development fixtures, not compliance accuracy)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-demo", action="store_true", help="Write four synthetic test events and their bundle")
    parser.add_argument("--index", action="store_true", help="Check the real index after rebuilding")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LogIngestionChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    if args.prepare_demo:
        prepare_demo()
    if args.index:
        check_index()


if __name__ == "__main__":
    main()
