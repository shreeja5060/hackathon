import json
from functools import lru_cache
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "chroma_db"
MANIFEST_PATH = DB_PATH / "index_manifest.json"
PROCESSED = ROOT / "data" / "processed"


@lru_cache(maxsize=1)
def _open_index(manifest_text):
    """Reuse the collection until the active manifest changes."""
    manifest = json.loads(manifest_text)

    if manifest["distance_metric"] != "cosine":
        raise ValueError("This retriever expects a cosine-distance index.")

    client = chromadb.PersistentClient(path=str(DB_PATH))
    collection = client.get_collection(
        name=manifest["collection_name"],
        embedding_function=None,
    )

    if collection.count() != manifest["indexed_chunk_count"]:
        raise RuntimeError("Index count does not match its manifest.")

    return manifest, collection


def _index():
    if not MANIFEST_PATH.is_file():
        raise FileNotFoundError(
            "No index manifest found. Run chunk_and_embed.py first."
        )

    return _open_index(MANIFEST_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _load_model(name, revision):
    """Load the same model used when building the index."""
    return SentenceTransformer(
        name,
        revision=revision,
        device="cpu",
        local_files_only=True,
        trust_remote_code=False,
    )


def _chunk_record(chunk_id, text, metadata):
    return {
        "chunk_id": chunk_id,
        "text": text,
        "source": metadata["source"],
        "page": metadata.get("page"),
        "type": metadata["type"],
        "doc_kind": metadata["doc_kind"],
        "locator": metadata.get("locator"),
    }


def search(query, type=None, top_k=5):
    """
    Return relevant chunks with citations and a similarity score.

    type: None, "internal", "framework", or "evidence".
    score: cosine similarity; higher means a closer match.
           It is not a confidence or compliance percentage.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string.")

    if type not in (None, "internal", "framework", "evidence"):
        raise ValueError(
            "type must be None, internal, framework, or evidence."
        )

    if (
        isinstance(top_k, bool)
        or not isinstance(top_k, int)
        or top_k < 1
    ):
        raise ValueError("top_k must be a positive integer.")

    manifest, collection = _index()

    if manifest["indexed_chunk_count"] == 0:
        return []

    model = _load_model(
        manifest["model_name"],
        manifest["model_revision"],
    )

    # BGE uses an instruction for queries, but not for documents.
    query_text = manifest["query_prefix"] + query.strip()

    tokens = model.tokenizer(
        query_text,
        add_special_tokens=True,
        truncation=False,
        verbose=False,
    )["input_ids"]

    if model.max_seq_length and len(tokens) > model.max_seq_length:
        raise ValueError(
            "Query is too long for the embedding model. "
            "Use a shorter question."
        )

    query_embedding = model.encode(
        [query_text],
        prompt="",
        normalize_embeddings=manifest["normalize_embeddings"],
        convert_to_numpy=True,
        show_progress_bar=False,
    )

    if query_embedding.shape[1] != manifest["embedding_dimension"]:
        raise RuntimeError("Query and index embedding dimensions differ.")

    raw = collection.query(
        query_embeddings=query_embedding.tolist(),
        n_results=min(top_k, manifest["indexed_chunk_count"]),
        where={"type": type} if type is not None else None,
        include=["documents", "metadatas", "distances"],
    )

    results = []

    for chunk_id, text, metadata, distance in zip(
        raw["ids"][0],
        raw["documents"][0],
        raw["metadatas"][0],
        raw["distances"][0],
        strict=True,
    ):
        if text is None or metadata is None:
            raise RuntimeError(f"Incomplete indexed record: {chunk_id}")

        record = _chunk_record(chunk_id, text, metadata)

        # Cosine distance = 1 - cosine similarity.
        # Clamp only to handle tiny floating-point rounding errors.
        record["score"] = max(
            -1.0, min(1.0, 1.0 - float(distance))
        )
        results.append(record)

    return results


def _original_chunks():
    originals = {}

    for filename in ("policy_chunks.json", "framework_chunks.json"):
        records = json.loads(
            (PROCESSED / filename).read_text(encoding="utf-8")
        )
        for record in records:
            chunk_id = record["chunk_id"]
            if chunk_id in originals:
                raise ValueError(f"Duplicate original ID: {chunk_id}")
            originals[chunk_id] = record

    return originals


def _resolve_original(chunk_id, metadata, originals):
    original_id = metadata.get("parent_chunk_id", chunk_id)

    if original_id not in originals:
        raise RuntimeError(
            f"Cannot find original entry for {chunk_id}. "
            "Check parent_chunk_id and the original chunk files."
        )

    original = originals[original_id]

    for field in ("source", "page", "locator", "type", "doc_kind"):
        if original.get(field) != metadata.get(field):
            raise RuntimeError(
                f"Original-entry metadata mismatch: {chunk_id}, {field}"
            )

    return dict(original)


def get_original_chunk(chunk_id):
    """Return the complete original entry for an indexed chunk ID."""
    _, collection = _index()

    result = collection.get(
        ids=[chunk_id],
        include=["metadatas"],
    )

    if not result["ids"]:
        raise KeyError(f"Chunk not found in the active index: {chunk_id}")

    return _resolve_original(
        result["ids"][0],
        result["metadatas"][0],
        _original_chunks(),
    )


def main():
    manifest, collection = _index()

    # Verify that persisted chunks still link to their original entries.
    originals = _original_chunks()
    stored = collection.get(
        limit=manifest["indexed_chunk_count"],
        include=["metadatas"],
    )

    split_count = 0

    for chunk_id, metadata in zip(
        stored["ids"], stored["metadatas"], strict=True
    ):
        _resolve_original(chunk_id, metadata, originals)
        if "parent_chunk_id" in metadata:
            split_count += 1

    print(
        f"Verified original-entry links for {len(stored['ids'])} chunks."
    )
    print(f"{split_count} fragments have parent_chunk_id.")

    examples = [
        (
            "Can employees share passwords or authentication tokens?",
            "internal",
        ),
        (
            "What requirements call for multifactor authentication?",
            "framework",
        ),
    ]

    for question, role in examples:
        print(f"\nQuestion: {question}")
        print(f"Filter: {role}")

        results = search(question, type=role, top_k=3)

        if not results:
            raise RuntimeError(f"No results for the {role} example.")

        for position, result in enumerate(results, start=1):
            if result["type"] != role:
                raise RuntimeError("Search returned the wrong evidence type.")

            print(
                f"\n{position}. Score: {result['score']:.3f}"
                f"\n   Source: {result['source']}"
                f"\n   Page: {result['page']}"
                f"\n   Locator: {result['locator']}"
            )

            # Shorten terminal previews only; search returns full text.
            preview = result["text"].replace("\n", " ")[:400]
            print(f"   Preview: {preview}")

    print("\nChecks finished. Review the passages for relevance.")


if __name__ == "__main__":
    main()