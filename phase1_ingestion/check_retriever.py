"""Check the real index's search contract before handing it to other phases.

Run from the repository root:
    python -m phase1_ingestion.check_retriever

Uses the existing local index and embedding model. No Claude calls.
"""

import math
from collections import Counter

if __package__:
    from .retriever import _index, get_original_chunk, search
else:
    from retriever import _index, get_original_chunk, search


EXPECTED_FIELDS = {
    "chunk_id", "text", "source", "page",
    "type", "doc_kind", "locator", "score",
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    manifest, collection = _index()
    stored = collection.get(
        limit=manifest["indexed_chunk_count"],
        include=["metadatas"],
    )
    metadata = stored["metadatas"]
    by_source = Counter(item["source"] for item in metadata)
    by_type = Counter(item["type"] for item in metadata)

    for source in (
        "NIST_SP-800-53_rev5_catalog.json",
        "NIST_CSF_v2.0_catalog.json",
    ):
        require(by_source[source] > 0, f"Catalog missing: {source}")
        print(f"PASS: {source}: {by_source[source]} indexed chunks")

    for role in (None, "internal", "framework", "evidence"):
        results = search("password authentication", type=role, top_k=3)
        available = len(metadata) if role is None else by_type[role]
        require(
            len(results) == min(3, available),
            f"Unexpected result count for filter {role!r}",
        )
        for result in results:
            require(set(result) == EXPECTED_FIELDS, "Search schema differs")
            require(bool(result["text"].strip()), "Empty result text")
            require(
                result["source"] in manifest["sources"],
                "Search result has no source record",
            )
            require(
                role is None or result["type"] == role,
                "Search returned a different evidence type",
            )
            require(
                math.isfinite(result["score"])
                and -1.0 <= result["score"] <= 1.0,
                "Invalid cosine similarity score",
            )
            if result["type"] == "framework":
                require(result["page"] is None, "Framework page must be null")
            require(
                result["locator"] is None
                or isinstance(result["locator"], str),
                "Locator must be a string or null",
            )

        scores = [result["score"] for result in results]
        require(scores == sorted(scores, reverse=True), "Results out of order")
        print(f"PASS: filter={role!r}, {len(results)} results, correct schema")

    fragment = next(
        (
            (chunk_id, item)
            for chunk_id, item in zip(stored["ids"], metadata, strict=True)
            if "parent_chunk_id" in item
        ),
        None,
    )
    if fragment is not None:
        chunk_id, item = fragment
        original = get_original_chunk(chunk_id)
        require(
            original["chunk_id"] == item["parent_chunk_id"],
            "Exact lookup returned the wrong original entry",
        )
        require(bool(original["text"].strip()), "Original entry has no text")
        print("PASS: get_original_chunk retrieves a fragment's original entry")
    else:
        print("SKIP: parent lookup check; this index has no split fragments")

    print("\nAll handoff checks passed.")
    print("These check the interface and index contents, not compliance accuracy.")


if __name__ == "__main__":
    main()
