"""
Placeholder for Phase 1's real retriever.

Once Maryam's real index (Chroma + embeddings over the policy PDFs and NIST
OSCAL data) is ready, this file gets replaced by phase1_ingestion/retriever.py,
which exposes the same search() signature. Everyone else (Phase 2 agents, the
chat box, the dashboard) should code against this interface now so nothing
has to change when the swap happens.

Schema (agreed with the team, confirmed against Maryam's real retriever
2026-10-02): type is one of "internal" | "framework" | "evidence" | None.
    {
        "chunk_id": str,
        "text": str,
        "source": str,
        "type": str,       # "internal" | "framework" | "evidence"
        "page": int | None,
        "locator": str | None,
    }
"""

from typing import Optional

_FAKE_CHUNKS = [
    {
        "chunk_id": "policy-security-001",
        "text": (
            "Users must comply with current organizational authentication "
            "requirements, including multifactor authentication where required."
        ),
        "source": "Public_Release_Computer_Security_Policy.pdf",
        "type": "internal",
        "page": 2,
        "locator": "Section 3.5 Authentication",
    },
    {
        "chunk_id": "policy-security-002",
        "text": (
            "Accounts are assigned to individuals and must not be shared unless "
            "an approved technical or business process explicitly permits it."
        ),
        "source": "Public_Release_Computer_Acceptable_Use_Policy.pdf",
        "type": "internal",
        "page": 1,
        "locator": "Section 3.2 Accounts and security controls",
    },
    {
        "chunk_id": "policy-security-003",
        "text": (
            "Organizational data must be stored in approved locations that "
            "provide appropriate access control, resilience, backup, and recovery."
        ),
        "source": "Public_Release_Computer_Security_Policy.pdf",
        "type": "internal",
        "page": 1,
        "locator": "Section 3.3 Data storage, transfer, and backup",
    },
    {
        "chunk_id": "framework-nist-ac2",
        "text": (
            "AC-2 Account Management: The organization manages information "
            "system accounts, including establishing, activating, modifying, "
            "reviewing, disabling, and removing accounts."
        ),
        "source": "NIST_SP_800-53_rev5.json",
        "type": "framework",
        "page": None,
        "locator": "AC-2",
    },
    {
        "chunk_id": "framework-nist-ia2",
        "text": (
            "IA-2 Identification and Authentication: The information system "
            "uniquely identifies and authenticates organizational users, "
            "including multifactor authentication for privileged accounts."
        ),
        "source": "NIST_SP_800-53_rev5.json",
        "type": "framework",
        "page": None,
        "locator": "IA-2",
    },
]


def search(query: str, type: Optional[str] = None, top_k: int = 5) -> list[dict]:
    """
    Placeholder retriever, matching Maryam's real search() signature:
    search(query, type=None, top_k=5). type must be "internal", "framework",
    "evidence", or None.
    """
    results = _FAKE_CHUNKS
    if type is not None:
        results = [c for c in results if c["type"] == type]

    query_lower = query.lower()
    keyword_hits = [c for c in results if any(w in c["text"].lower() for w in query_lower.split())]
    ranked = keyword_hits if keyword_hits else results

    return ranked[:top_k]


def get_original_chunk(chunk_id: str) -> Optional[dict]:
    """
    Fetch one exact chunk by its chunk_id, no search involved. This is what
    the Auditor uses to retrieve the EXACT control chunk the Mapper matched,
    rather than searching again (which could theoretically return something
    different). Returns None if no chunk with that id exists.
    """
    for chunk in _FAKE_CHUNKS:
        if chunk["chunk_id"] == chunk_id:
            return chunk
    return None


if __name__ == "__main__":
    import json
    print(json.dumps(search("multifactor authentication"), indent=2))
