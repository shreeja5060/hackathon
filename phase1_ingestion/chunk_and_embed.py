import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
DB_PATH = ROOT / "chroma_db"

MODEL_NAME = "BAAI/bge-small-en-v1.5"
MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

MAX_TOKENS = 480
OVERLAP_WORDS = 20

# Keep requirements, discussion, and examples clearly labelled.
FRAMEWORK_LABELS = re.compile(
    r"^(?:Control statement|Discussion[^\n]*|CSF outcome|"
    r"Implementation examples[^\n]*):[ \t]*$",
    re.MULTILINE,
)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def fingerprint(value):
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def token_count(text, tokenizer):
    return len(
        tokenizer(
            text,
            add_special_tokens=True,
            truncation=False,
            verbose=False,
        )["input_ids"]
    )


def split_body(prefix, body, tokenizer):
    """Split on word boundaries, preserving the original text."""
    words = list(re.finditer(r"\S+", body))
    start = 0

    while start < len(words):
        low = start + 1
        high = len(words)
        best_end = start

        # Find the largest piece that fits the model's token budget.
        while low <= high:
            middle = (low + high) // 2
            excerpt = body[
                words[start].start():words[middle - 1].end()
            ]
            candidate = prefix + excerpt

            if token_count(candidate, tokenizer) <= MAX_TOKENS:
                best_end = middle
                low = middle + 1
            else:
                high = middle - 1

        if best_end == start:
            raise ValueError(
                "A heading/context plus one word exceeds the token "
                "limit. Review this entry instead of truncating it."
            )

        yield prefix + body[
            words[start].start():words[best_end - 1].end()
        ]

        if best_end == len(words):
            break

        start = max(start + 1, best_end - OVERLAP_WORDS)


def prepare_chunk(record, tokenizer):
    text = record["text"].strip()

    if not text:
        raise ValueError(f"Empty chunk: {record['chunk_id']}")

    if token_count(text, tokenizer) <= MAX_TOKENS:
        return [record]

    labels = (
        list(FRAMEWORK_LABELS.finditer(text))
        if record["type"] == "framework"
        else []
    )

    blocks = []

    if labels:
        context = text[:labels[0].start()].strip()

        for position, label in enumerate(labels):
            end = (
                labels[position + 1].start()
                if position + 1 < len(labels)
                else len(text)
            )
            body = text[label.end():end].strip()
            prefix = context + "\n\n" + label.group().strip() + "\n"
            blocks.append((prefix, body))
    else:
        heading, separator, body = text.partition("\n")
        blocks = (
            [(heading + "\n\n", body.strip())]
            if separator
            else [("", text)]
        )

    pieces = [
        piece
        for prefix, body in blocks
        for piece in split_body(prefix, body, tokenizer)
    ]

    if not pieces:
        raise ValueError(f"No text after splitting: {record['chunk_id']}")

    return [
        {
            **record,
            "parent_chunk_id": record["chunk_id"],
            "chunk_id": "fragment-" + fingerprint(
                [record["chunk_id"], number, piece]
            ),
            "text": piece,
        }
        for number, piece in enumerate(pieces, start=1)
    ]


def main():
    policies = read_json(PROCESSED / "policy_chunks.json")
    frameworks = read_json(PROCESSED / "framework_chunks.json")
    environment_path = PROCESSED / "environment_bundle.json"
    environment = read_json(environment_path) if environment_path.exists() else {"chunks": [], "sources": {}}
    inventory_path = PROCESSED / "inventory_bundle.json"
    inventory = read_json(inventory_path) if inventory_path.exists() else {"chunks": [], "sources": {}}
    logs_path = PROCESSED / "logs_bundle.json"
    logs = read_json(logs_path) if logs_path.exists() else {"chunks": [], "sources": {}}
    evidence = environment["chunks"] + inventory["chunks"] + logs["chunks"]
    original_chunks = policies + frameworks + evidence

    sources = read_json(PROCESSED / "policy_sources.json")
    framework_sources = read_json(
        ROOT / "data" / "frameworks" / "framework_sources.json"
    )

    if sources.keys() & framework_sources.keys():
        raise ValueError("Duplicate filenames in the source records.")

    sources.update(framework_sources)
    if sources.keys() & environment["sources"].keys():
        raise ValueError("Duplicate environment source filename")
    sources.update(environment["sources"])
    if sources.keys() & inventory["sources"].keys():
        raise ValueError("Duplicate inventory source filename")
    for inventory_source in inventory["sources"].values():
        linked_source = environment["sources"].get(inventory_source["linked_configuration"])
        if linked_source is None or linked_source.get("sha256") != inventory_source["linked_configuration_sha256"]:
            raise ValueError("Inventory/configuration snapshots differ. Rerun load_environment and load_inventory.")
    sources.update(inventory["sources"])
    if sources.keys() & logs["sources"].keys():
        raise ValueError("Duplicate security-log source filename")
    sources.update(logs["sources"])

    if not original_chunks:
        raise ValueError("No chunks found.")

    required_fields = {
        "chunk_id", "text", "source", "page",
        "type", "doc_kind", "locator",
    }

    for chunk in original_chunks:
        missing = required_fields - chunk.keys()
        if missing:
            raise ValueError(f"Chunk is missing fields: {sorted(missing)}")
        if chunk["source"] not in sources:
            raise ValueError(f"Missing source record: {chunk['source']}")
        if chunk["type"] not in {"internal", "framework", "evidence"}:
            raise ValueError(f"Unexpected type: {chunk['type']}")

    original_ids = [chunk["chunk_id"] for chunk in original_chunks]
    if len(original_ids) != len(set(original_ids)):
        raise ValueError("Duplicate input chunk IDs.")

    print(
        f"Loaded {len(policies)} policy chunks and "
        f"{len(frameworks)} framework chunks and {len(evidence)} evidence chunks."
    )
    print("Loading embedding model; the first run downloads its files...")

    model = SentenceTransformer(
        MODEL_NAME,
        revision=MODEL_REVISION,
        device="cpu",
        trust_remote_code=False,
    )

    if model.max_seq_length and MAX_TOKENS > model.max_seq_length:
        raise ValueError("Chunk limit exceeds the model's input limit.")

    tokenizer = model.tokenizer
    indexed_chunks = [
        prepared
        for chunk in original_chunks
        for prepared in prepare_chunk(chunk, tokenizer)
    ]

    ids = [chunk["chunk_id"] for chunk in indexed_chunks]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate prepared chunk IDs.")

    largest = max(
        token_count(chunk["text"], tokenizer)
        for chunk in indexed_chunks
    )
    if largest > MAX_TOKENS:
        raise ValueError("A prepared chunk still exceeds the token limit.")

    print(f"Prepared {len(indexed_chunks)} chunks for indexing.")
    print(f"Largest prepared chunk: {largest} tokens.")

    settings = {
        "model_name": MODEL_NAME,
        "model_revision": MODEL_REVISION,
        "query_prefix": QUERY_PREFIX,
        "document_prefix": "",
        "normalize_embeddings": True,
        "distance_metric": "cosine",
        "max_tokens": MAX_TOKENS,
        "overlap_words": OVERLAP_WORDS,
        "splitter_version": 1,
    }

    # Different inputs get a separate collection, avoiding stale chunks.
    snapshot = fingerprint({
        "settings": settings,
        "chunks": indexed_chunks,
        "sources": sources,
    })
    collection_name = "compliance-" + snapshot[:20]

    print("Creating embeddings...")
    embeddings = model.encode(
        [chunk["text"] for chunk in indexed_chunks],
        prompt="",
        batch_size=32,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=True,
    )

    client = chromadb.PersistentClient(path=str(DB_PATH))
    collection = client.get_or_create_collection(
        name=collection_name,
        embedding_function=None,
        configuration={"hnsw": {"space": "cosine"}},
    )

    batch_size = min(128, client.get_max_batch_size())

    for start in range(0, len(indexed_chunks), batch_size):
        batch = indexed_chunks[start:start + batch_size]

        # Chroma stores IDs/text separately from metadata.
        # Missing page/locator values are restored as null by search().
        metadata = [
            {
                key: value
                for key, value in chunk.items()
                if key not in {"chunk_id", "text"} and value is not None
            }
            for chunk in batch
        ]

        collection.upsert(
            ids=[chunk["chunk_id"] for chunk in batch],
            documents=[chunk["text"] for chunk in batch],
            metadatas=metadata,
            embeddings=embeddings[
                start:start + len(batch)
            ].tolist(),
        )

    stored_count = collection.count()
    if stored_count != len(indexed_chunks):
        raise RuntimeError(
            f"Expected {len(indexed_chunks)} chunks, found {stored_count}."
        )

    save_json(PROCESSED / "indexed_chunks.json", indexed_chunks)

    # Keep the exact originals with this index, even if inputs later change.
    originals_file = collection_name + "-originals.json"
    save_json(DB_PATH / originals_file, original_chunks)

    # Publish the active index only after all chunks are stored.
    save_json(
        DB_PATH / "index_manifest.json",
        {
            **settings,
            "collection_name": collection_name,
            "originals_file": originals_file,
            "embedding_dimension": int(embeddings.shape[1]),
            "input_chunk_count": len(original_chunks),
            "indexed_chunk_count": stored_count,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sources": sources,
        },
    )

    print(f"Stored {stored_count} chunks in Chroma.")
    print(f"Database: {DB_PATH}")
    print("Index ready. Next: build and check the search function.")


if __name__ == "__main__":
    main()
