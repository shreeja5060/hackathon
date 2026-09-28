"""
Placeholder for Phase 1's real retriever.

Once Maryam's real index (Chroma + embeddings over the policy PDFs and NIST
OSCAL data) is ready, this file gets replaced by phase1_ingestion/retriever.py,
which exposes the same search() signature. Everyone else (Phase 2 agents, the
chat box, the dashboard) should code against this interface now so nothing
has to change when the swap happens.

Schema (agreed 2026-09-25, credit Maryam):
    {
        "chunk_id": str,   # unique id for this chunk, used to cite exact evidence
        "text": str,        # the chunk's text content
        "source": str,      # source file name
        "type": str,        # "policy" | "framework" | "config" | "log" | "inventory"
        "page": int | None, # page number, for PDFs
        "locator": str | None,  # finer reference: policy section, NIST control ID,
                                 # JSON path, or log event reference
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
        "type": "policy",
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
        "type": "policy",
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
        "type": "policy",
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
    Placeholder retriever. Ignores the actual query text and does a naive
    keyword-ish filter so callers get *something* plausible to build against.
    Replace with the real Chroma-backed retriever from phase1_ingestion/.

    Args:
        query: the search text (not semantically used here, just a stand-in)
        type: optional filter, one of "policy" | "framework" | "config" | "log" | "inventory"
        top_k: max number of results

    Returns:
        A list of chunk dicts matching the agreed schema above.
    """
    results = _FAKE_CHUNKS
    if type is not None:
        results = [c for c in results if c["type"] == type]

    query_lower = query.lower()
    keyword_hits = [c for c in results if any(w in c["text"].lower() for w in query_lower.split())]
    ranked = keyword_hits if keyword_hits else results

    return ranked[:top_k]


if __name__ == "__main__":
    import json

    print(json.dumps(search("multifactor authentication"), indent=2))
