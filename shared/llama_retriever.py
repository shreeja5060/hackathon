"""
Phase 2 / shared: a LlamaIndex-based alternative to fake_search.py.

This shows the FRAMEWORK way of doing retrieval, versus the plain-Python way
in fake_search.py. Key new ideas:

  - Document: LlamaIndex wraps each chunk's text + metadata into a Document
    object, rather than a plain dict.
  - Index: a structure built over your Documents that knows how to find
    relevant ones for a query.
  - Query engine / retriever: the object you actually call to search.
  - Metadata filtering: querying only within chunks whose metadata matches
    (e.g. type == "framework") - built into LlamaIndex rather than hand-
    written like our _FAKE_CHUNKS filter.

Note: LlamaIndex's most common retrieval method uses EMBEDDINGS, which by
default calls OpenAI - we don't have a key for that here. This file instead
uses SimpleKeywordTableIndex, a keyword-based index that needs no embedding
API at all, so it runs with zero extra setup. A production version would
swap in a local embedding model (e.g. HuggingFaceEmbedding) for better
semantic matching - noted as a trade-off below.
"""

import os
import sys
from dotenv import load_dotenv
from llama_index.core import Document, SimpleKeywordTableIndex, Settings
from llama_index.llms.anthropic import Anthropic

sys.path.append(os.path.join(os.path.dirname(__file__)))
from fake_search import _FAKE_CHUNKS  # reuse our existing sample data

load_dotenv()

# LlamaIndex defaults to OpenAI for its internal LLM calls (here, keyword
# extraction for the index) unless told otherwise. Since our whole project
# uses Claude, we point LlamaIndex at it too - this is the "model-agnostic"
# part of the framework: swap providers without changing how you call it.
Settings.llm = Anthropic(model="claude-sonnet-4-5", api_key=os.getenv("ANTHROPIC_API_KEY"))


def build_index() -> SimpleKeywordTableIndex:
    """
    Converts our existing chunk dicts into LlamaIndex Documents (text +
    metadata), then builds a keyword index over them. This is the
    "framework way" of what chunk_and_embed.py does with ChromaDB + real
    embeddings.
    """
    documents = []
    for chunk in _FAKE_CHUNKS:
        doc = Document(
            text=chunk["text"],
            metadata={
                "chunk_id": chunk["chunk_id"],
                "source": chunk["source"],
                "type": chunk["type"],
                "locator": chunk["locator"],
            },
        )
        documents.append(doc)

    index = SimpleKeywordTableIndex.from_documents(documents)
    return index


def search_with_llamaindex(query: str, type: str = None, top_k: int = 5) -> list[dict]:
    """
    Same search() shape as fake_search.py and the real retriever, but
    powered by a LlamaIndex retriever under the hood. This demonstrates the
    framework can be swapped in without changing the interface the rest of
    the pipeline depends on.
    """
    index = build_index()
    retriever = index.as_retriever(similarity_top_k=top_k)

    nodes = retriever.retrieve(query)

    results = []
    for node in nodes:
        meta = node.metadata
        # Apply the type filter ourselves here, since SimpleKeywordTableIndex
        # doesn't support metadata filters natively the way a vector index does
        if type is not None and meta.get("type") != type:
            continue
        results.append({
            "chunk_id": meta.get("chunk_id"),
            "text": node.text,
            "source": meta.get("source"),
            "type": meta.get("type"),
            "locator": meta.get("locator"),
        })

    return results


if __name__ == "__main__":
    import json
    print("Searching with LlamaIndex for 'authentication multifactor'...\n")
    results = search_with_llamaindex("authentication multifactor", type="internal")
    print(json.dumps(results, indent=2))
