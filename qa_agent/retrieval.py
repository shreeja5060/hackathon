"""
Q&A agent: thin wrapper over Phase 1's search().

- A bare control ID ("AC-2", "IA-2(1)", "PR.AA-06") is looked up exactly.
  Searching an ID by meaning returns a neighbor (e.g. "PR.AA-06" -> PR.AA-02).
  Exact hits get score 1.0 so they still match the search() result contract.
- type="policy" is accepted as an alias for "internal".
- Chroma's client isn't safe to open from several threads at once, so calls
  into search() are serialized.
"""

import json
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROCESSED = REPO_ROOT / "data" / "processed"

TYPE_ALIASES = {"policy": "internal"}

_lock = threading.Lock()
_framework_by_id = None


def _framework_index() -> dict:
    global _framework_by_id
    if _framework_by_id is None:
        chunks = json.loads((PROCESSED / "framework_chunks.json").read_text(encoding="utf-8"))
        _framework_by_id = {c["locator"]: c for c in chunks if c.get("locator")}
    return _framework_by_id


def search(query: str, type: str | None = None, top_k: int = 5) -> list[dict]:
    from phase1_ingestion.retriever import search as phase1_search

    type = TYPE_ALIASES.get(type, type)
    query = (query or "").strip()
    if not query:
        return []
    if type in ("framework", None):
        hit = _framework_index().get(query)
        if hit:
            return [{**hit, "score": 1.0}]
    with _lock:
        return phase1_search(query, type=type, top_k=top_k)


def original_text(chunk_id: str) -> str:
    """Full text of an indexed passage, or "" if it can't be found."""
    from phase1_ingestion.retriever import get_original_chunk
    try:
        with _lock:
            return get_original_chunk(chunk_id)["text"]
    except Exception:
        return ""
