"""
Q&A agent: Claude answers questions about the policies, NIST and the
synthetic environment evidence, citing only passages it actually retrieved.

Claude searches Phase 1's index with a search_documents tool. Each passage it
sees gets a reference (S1, S2, ...) that it cites inline as [S1]. Before the
answer is returned, citations are checked against what was retrieved: a
reference that wasn't retrieved is removed, and the rest are renumbered [1],
[2], ... in the order of the returned citations list.

Two entry points:
- ask(question) -> {"answer": str, "citations": [search() results]}
  The dashboard's Q&A contract (COPILOT_QA_MODULE="qa_agent.agent:ask").
- answer(question, finding=None, history=None) -> dict
  Adds conversation history and finding-focused mode: the finding, its policy
  text and its NIST control text are preloaded, and Claude can call
  propose_recommendation. A proposal is only a suggestion for the reviewer.

Every exchange is logged to data/processed/chat_log.sqlite3.
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

import anthropic
from dotenv import load_dotenv

from qa_agent import retrieval

load_dotenv(retrieval.REPO_ROOT / ".env")

# Haiku while developing; set CHAT_MODEL (e.g. claude-sonnet-5-5) for the demo.
MODEL = os.getenv("CHAT_MODEL", "claude-haiku-4-5")
MAX_TOOL_ROUNDS = 4
MAX_PASSAGE_CHARS = 2500
LOG_DB = retrieval.PROCESSED / "chat_log.sqlite3"

SYSTEM_PROMPT = """You are the Compliance Copilot assistant. You help a compliance reviewer \
understand an organization's security policies, how they map to NIST SP 800-53 Rev. 5 and \
NIST CSF 2.0, and any synthetic environment evidence (configurations, asset inventory).

Ground every factual statement in passages returned by the search_documents tool, or in \
passages provided in the conversation. Cite them inline with their reference in square \
brackets, e.g. [S2]. Only cite references you were actually given. If the documents don't \
answer the question, say that the answer was not found in the documents rather than \
answering from general knowledge.

Search before answering unless the provided passages already answer the question. Use \
doc_type "internal" for the organization's policies, "framework" for NIST, and "evidence" \
for configuration and inventory evidence.

When the reviewer asks you to improve, rewrite or suggest a recommendation for the finding \
under review, call propose_recommendation with one or two concrete, actionable sentences \
grounded in the policy text and the NIST control, then briefly explain the change in your \
reply. The reviewer decides whether to apply it.

Keep answers short: a few plain sentences. Don't use Markdown formatting (no bold, \
headings or bullet lists); the dashboard shows answers as plain text."""

SEARCH_TOOL = {
    "name": "search_documents",
    "description": (
        "Search the compliance knowledge base: the organization's security policies, "
        "NIST SP 800-53 Rev. 5 controls, NIST CSF 2.0 outcomes, and synthetic environment "
        "evidence. Returns the most relevant passages, each with a reference to cite. "
        "A bare control ID such as AC-2 or PR.AA-06 returns that exact control."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for, in plain words, or a control ID."},
            "doc_type": {
                "type": "string",
                "enum": ["internal", "framework", "evidence", "any"],
                "description": "internal = policies, framework = NIST, evidence = configs/inventory, any = all.",
            },
        },
        "required": ["query", "doc_type"],
        "additionalProperties": False,
    },
    "strict": True,
}
PROPOSE_TOOL = {
    "name": "propose_recommendation",
    "description": (
        "Propose new wording for the recommendation of the finding under review. "
        "Only use this when the reviewer asks for a better or different recommendation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "recommendation": {"type": "string", "description": "The proposed recommendation text."},
        },
        "required": ["recommendation"],
        "additionalProperties": False,
    },
    "strict": True,
}

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


class _Sources:
    """Numbers every passage shown to Claude in one answer as S1, S2, ..."""

    def __init__(self):
        self.by_ref = {}
        self.by_chunk = {}

    def render(self, chunk: dict) -> str:
        cid = chunk["chunk_id"]
        if cid not in self.by_chunk:
            ref = f"S{len(self.by_ref) + 1}"
            self.by_chunk[cid] = ref
            self.by_ref[ref] = chunk
        ref = self.by_chunk[cid]
        where = ", ".join(str(x) for x in (chunk.get("source"), chunk.get("locator")) if x)
        return f"[{ref}] {where}\n{(chunk.get('text') or '')[:MAX_PASSAGE_CHARS]}"


def _finding_context(finding: dict, sources: _Sources) -> str:
    parts = [
        "The reviewer is looking at this finding:",
        json.dumps({k: finding.get(k) for k in
                    ("requirement", "coverage", "finding", "recommendation", "framework_control",
                     "plain_language", "clarifying_questions") if finding.get(k) is not None},
                   indent=2),
    ]
    citation = finding.get("citation") or {}
    cid = citation.get("chunk_id")
    if cid:
        text = finding.get("cited_text") or retrieval.original_text(cid)
        if text:
            parts.append("Policy text the finding cites:\n" + sources.render({
                "chunk_id": cid, "text": text, "source": citation.get("source"),
                "locator": citation.get("locator"), "page": citation.get("page"),
                "type": "internal", "doc_kind": "policy", "score": 1.0}))
    control = finding.get("framework_control")
    if control:
        hits = retrieval.search(control, type="framework", top_k=1)
        if hits:
            parts.append("NIST control text:\n" + sources.render(hits[0]))
    return "\n\n".join(parts)


def _run_search(args: dict, sources: _Sources) -> str:
    doc_type = None if args.get("doc_type") in (None, "any") else args["doc_type"]
    try:
        hits = retrieval.search(args.get("query", ""), type=doc_type, top_k=5)
    except Exception as e:
        return f"Search failed: {e}"
    if not hits:
        return "No matching passages."
    return "\n\n---\n\n".join(sources.render(h) for h in hits)


# One bracket can hold several references: [S1], [S1, S2], [S1; S2].
_CITE_GROUP = re.compile(r"\[\s*(S\d+(?:\s*[,;]\s*S\d+)*)\s*\]")
_REF = re.compile(r"S\d+")


def _check_citations(text: str, sources: _Sources) -> tuple[str, list[dict], list[str]]:
    """
    Keep citations to retrieved passages, renumbered [1], [2], ... in order of
    first use; strip references that weren't retrieved and report them.
    """
    refs = [ref for group in _CITE_GROUP.findall(text) for ref in _REF.findall(group)]
    used, rejected = [], []
    for ref in dict.fromkeys(refs):
        (used if ref in sources.by_ref else rejected).append(ref)
    number = {ref: i for i, ref in enumerate(used, start=1)}

    def renumber(match):
        kept = [f"[{number[ref]}]" for ref in _REF.findall(match.group(1)) if ref in number]
        return "".join(dict.fromkeys(kept))

    text = _CITE_GROUP.sub(renumber, text)
    text = re.sub(r"[ \t]+([.,;:])", r"\1", text)  # tidy spaces left by removed references
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text, [sources.by_ref[ref] for ref in used], rejected


def answer(question: str, finding: dict | None = None, history: list[dict] | None = None) -> dict:
    """
    Answer one question. history holds earlier turns as {"role", "content"}.
    Returns {"answer", "citations" (search() results, in [1], [2] order),
    "rejected_citations", and "proposed_recommendation" when one was proposed}.
    """
    started = time.time()
    sources = _Sources()
    messages = [{"role": m["role"], "content": m["content"]}
                for m in (history or []) if m.get("role") in ("user", "assistant") and m.get("content")]

    user_content = question
    if finding:
        user_content = _finding_context(finding, sources) + "\n\nReviewer's question: " + question
    messages.append({"role": "user", "content": user_content})

    tools = [SEARCH_TOOL, PROPOSE_TOOL] if finding else [SEARCH_TOOL]
    proposal, usage, text = None, {"input": 0, "output": 0}, ""
    try:
        for _ in range(MAX_TOOL_ROUNDS + 1):
            response = _get_client().messages.create(
                model=MODEL, max_tokens=2000, system=SYSTEM_PROMPT, tools=tools, messages=messages)
            usage["input"] += response.usage.input_tokens
            usage["output"] += response.usage.output_tokens

            if response.stop_reason == "refusal":
                text = "I can't help with that request."
                break
            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text")
                break

            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if block.name == "search_documents":
                    content = _run_search(block.input, sources)
                elif block.name == "propose_recommendation":
                    proposal = (block.input.get("recommendation") or "").strip() or None
                    content = "Proposal recorded. The reviewer will decide whether to apply it."
                else:
                    content = f"Unknown tool {block.name}."
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
            messages.append({"role": "user", "content": results})
        else:
            text = "I couldn't finish answering within the search limit. Try a more specific question."
    except anthropic.AuthenticationError:
        text = "The Q&A agent can't reach Claude: the Anthropic API key was rejected."
    except anthropic.RateLimitError:
        text = "Claude is rate-limiting requests right now. Please try again in a moment."
    except anthropic.APIStatusError as e:
        text = f"Claude returned an error ({e.status_code}). Please try again."
    except anthropic.APIConnectionError:
        text = "Couldn't connect to Claude. Check the internet connection."

    text, citations, rejected = _check_citations(text, sources)
    result = {"answer": text.strip(), "citations": citations, "rejected_citations": rejected}
    if proposal:
        result["proposed_recommendation"] = proposal
    _log(question, finding, sources, result, usage, time.time() - started)
    return result


def ask(question: str) -> dict:
    """Dashboard Q&A contract: {"answer": str, "citations": [search() results]}."""
    result = answer(question)
    return {"answer": _plain(result["answer"]), "citations": result["citations"]}


def _plain(text: str) -> str:
    """Drop leftover Markdown markers; the dashboard shows answers as one line of plain text."""
    text = re.sub(r"(\*\*|__|`)", "", text)
    text = re.sub(r"^\s*(#{1,6}\s+|[-*]\s+)", "", text, flags=re.M)
    return re.sub(r"\s*\n\s*", " ", text).strip()


def _log(question, finding, sources, result, usage, seconds) -> None:
    try:
        LOG_DB.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(LOG_DB) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS chat_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, model TEXT, finding TEXT,
                question TEXT, retrieved_chunk_ids TEXT, cited_chunk_ids TEXT,
                rejected_refs TEXT, answer TEXT, proposed_recommendation TEXT,
                input_tokens INTEGER, output_tokens INTEGER, seconds REAL)""")
            db.execute(
                "INSERT INTO chat_log (ts, model, finding, question, retrieved_chunk_ids, cited_chunk_ids,"
                " rejected_refs, answer, proposed_recommendation, input_tokens, output_tokens, seconds)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), MODEL,
                 finding.get("requirement") if finding else None, question,
                 json.dumps(list(sources.by_chunk)), json.dumps([c["chunk_id"] for c in result["citations"]]),
                 json.dumps(result["rejected_citations"]), result["answer"],
                 result.get("proposed_recommendation"), usage["input"], usage["output"], round(seconds, 2)))
    except sqlite3.Error:
        pass  # logging must never break the chat
