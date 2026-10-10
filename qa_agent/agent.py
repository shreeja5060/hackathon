"""
Q&A agent: Claude answers questions about the policies, NIST and the
synthetic environment evidence, citing only passages it actually retrieved.

Claude searches Phase 1's index with a search_documents tool. Each passage it
sees gets a reference (S1, S2, ...) that it cites inline as [S1]. Before the
answer is returned, citations are checked against what was retrieved: a
reference that wasn't retrieved is removed, and the rest are renumbered [1],
[2], ... in the order of the returned citations list.

When the caller passes the review's findings, Claude sees a summary of each
(ID, status, coverage, recommendation, reviewer note), can fetch one
finding's policy and NIST text with get_finding_evidence, and can suggest new
recommendation wording with propose_recommendation. Suggestions are returned
separately and never change a finding: the reviewer decides.

Entry points:
- ask(question, findings=None, history=None)
  The dashboard's Q&A contract (COPILOT_QA_MODULE="qa_agent.agent:ask"):
  {"answer": str, "citations": [search() results], "suggestions": [...]}.
  findings and history are optional, so ask(question) still works.
- answer(question, finding=None, history=None, findings=None) -> dict
  The same, plus finding-focused mode: one finding's evidence is preloaded.

Every exchange is logged to data/processed/chat_log.sqlite3.
"""

import dataclasses
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

import anthropic
from dotenv import load_dotenv

from qa_agent import retrieval

load_dotenv(retrieval.REPO_ROOT / ".env")

# Same switch as the Phase 2 agents: CLAUDE_BACKEND=anthropic (API key) or
# vertex (Claude on Vertex AI in the GCP project, no key).
sys.path.append(str(retrieval.REPO_ROOT / "shared"))
import claude_client  # noqa: E402

# Haiku while developing; set CHAT_MODEL (e.g. claude-sonnet-5-5) for the demo.
# On Vertex AI, model IDs carry a version suffix and must be enabled in the
# project, so the chat uses the agents' model there unless CHAT_MODEL is set.
MODEL = os.getenv("CHAT_MODEL") or (
    claude_client.MODEL if claude_client.BACKEND == "vertex" else "claude-haiku-4-5")
MAX_TOOL_ROUNDS = 6
MAX_PASSAGE_CHARS = 2500
MAX_FINDINGS_IN_CONTEXT = 80
MAX_HISTORY_MESSAGES = 10
LOG_DB = retrieval.PROCESSED / "chat_log.sqlite3"

SYSTEM_PROMPT = """You are the Compliance Copilot assistant. You help a compliance reviewer \
understand an organization's security policies, how they map to NIST SP 800-53 Rev. 5 and \
NIST CSF 2.0, and any synthetic environment evidence (configurations, asset inventory, \
audit logs).

Ground every factual statement in passages returned by your tools, or in passages provided \
in the conversation. Cite them inline with their reference in square brackets, e.g. [S2]. \
Only cite references you were actually given. If the documents don't answer the question, \
say that the answer was not found in the documents rather than answering from general \
knowledge.

Search before answering unless the provided passages already answer the question. Use \
doc_type "internal" for the organization's policies, "framework" for NIST, and "evidence" \
for configuration, inventory and log evidence.

You may be given the findings in the reviewer's current gap-analysis review, with their IDs \
and review status. Refer to findings by ID. Use get_finding_evidence to read a finding's \
policy text and NIST control before judging or rewriting it. It also returns what the \
organization's other reviewed policies say about the same control; when they differ, cover \
the gap, or repeat it, say so and cite them.

When the reviewer asks you to improve, rewrite or suggest a recommendation, call \
propose_recommendation once per finding with one or two concrete, actionable sentences \
grounded in that finding's policy text and NIST control. Also state each suggestion in your \
reply, with its finding ID, and briefly say why. The reviewer decides whether to use it.

Earlier turns in the conversation show only their final text, not the tool calls behind \
them. Their citations and any suggestions were already checked and recorded when they were \
given, so don't second-guess or retract them. Their [n] markers refer to that turn's own \
sources, not to the references in the current turn.

Keep answers short: a few plain sentences. Don't use Markdown formatting (no bold, \
headings or bullet lists); the dashboard shows answers as plain text."""

SEARCH_TOOL = {
    "name": "search_documents",
    "description": (
        "Search the compliance knowledge base: the organization's security policies, "
        "NIST SP 800-53 Rev. 5 controls, NIST CSF 2.0 outcomes, and synthetic environment "
        "evidence (configurations, inventory, audit logs). Returns the most relevant passages, "
        "each with a reference to cite. A bare control ID such as AC-2 or PR.AA-06 returns "
        "that exact control."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for, in plain words, or a control ID."},
            "doc_type": {
                "type": "string",
                "enum": ["internal", "framework", "evidence", "any"],
                "description": "internal = policies, framework = NIST, evidence = configs/inventory/logs, any = all.",
            },
        },
        "required": ["query", "doc_type"],
        "additionalProperties": False,
    },
    "strict": True,
}


def _evidence_tool(ids: list[str]) -> dict:
    return {
        "name": "get_finding_evidence",
        "description": (
            "Get one finding's evidence: the policy text it cites and the text of its NIST "
            "control, each with a reference to cite."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"finding_id": {"type": "string", "enum": ids}},
            "required": ["finding_id"],
            "additionalProperties": False,
        },
        "strict": True,
    }


def _propose_tool(ids: list[str]) -> dict:
    return {
        "name": "propose_recommendation",
        "description": (
            "Suggest new wording for one finding's recommendation. Only use this when the "
            "reviewer asks for a better or different recommendation. Call once per finding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string", "enum": ids},
                "recommendation": {"type": "string", "description": "The suggested recommendation text."},
            },
            "required": ["finding_id", "recommendation"],
            "additionalProperties": False,
        },
        "strict": True,
    }


_client = None


def _get_client():
    global _client
    if _client is None:
        _client = claude_client.make_client()  # Anthropic or AnthropicVertex, same messages API
    return _client


MAX_RELATED_POLICIES = 4
RELATED_LABELS = {
    "conflict": "Sets a different value for the same thing",
    "covered_elsewhere": "Covers this control fully",
    "overlap": "Also addresses this control",
}


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


# ---------------------------------------------------------------- findings

def _as_dict(value) -> dict:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return dict(value) if isinstance(value, dict) else {}


def _normalize_findings(findings) -> dict[str, dict]:
    """
    Accept the dashboard's Finding objects or plain gap-report dicts.
    Returns {finding_id: finding dict} with the fields the agent uses.
    """
    out = {}
    for position, raw in enumerate(findings or [], start=1):
        f = _as_dict(raw)
        if not f:
            continue
        extras = f.get("extras") or {}
        edited = f.get("recommendation_edited")
        fid = str(f.get("finding_id") or f.get("id") or f"F-{position}")
        out[fid] = {
            "id": fid,
            "requirement": f.get("requirement"),
            "requirement_text": f.get("requirement_text"),
            "coverage": f.get("coverage"),
            "finding": f.get("finding"),
            "recommendation": edited if edited is not None else f.get("recommendation"),
            "framework_control": f.get("framework_control"),
            "status": f.get("status"),
            "reviewer_note": f.get("reviewer_note") or None,
            "plain_language": f.get("plain_language") or extras.get("plain_language"),
            "clarifying_questions": f.get("clarifying_questions") or extras.get("clarifying_questions"),
            "citation": _as_dict(f.get("citation")),
            "cited_text": f.get("cited_text"),
            "related_policies": [r for r in (f.get("related_policies") or []) if isinstance(r, dict)],
        }
    return out


def _review_summary(findings: dict[str, dict]) -> str:
    rows = []
    for f in list(findings.values())[:MAX_FINDINGS_IN_CONTEXT]:
        row = {k: f[k] for k in ("id", "status", "requirement", "coverage", "finding",
                                 "recommendation", "framework_control", "reviewer_note")
               if f.get(k) not in (None, "")}
        rows.append(json.dumps(row, ensure_ascii=False))
    more = len(findings) - len(rows)
    tail = f"\n({more} more findings not shown.)" if more > 0 else ""
    return "Findings in the reviewer's current review (one per line):\n" + "\n".join(rows) + tail


def _finding_evidence(finding: dict, sources: _Sources) -> str:
    parts = []
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
    related = []
    for item in (finding.get("related_policies") or [])[:MAX_RELATED_POLICIES]:
        if not item.get("requirement_text") or not item.get("policy"):
            continue
        label = RELATED_LABELS.get(item.get("kind"), "also addresses this control")
        related.append(f"{label} ({item.get('detail') or ''}; rated {item.get('coverage') or 'unknown'} there):\n"
                       + sources.render({
                           "chunk_id": f"related:{item.get('run_id')}:{item.get('finding_id')}",
                           "text": item["requirement_text"], "source": item["policy"], "page": None,
                           "locator": item.get("locator"), "type": "internal", "doc_kind": "policy", "score": 1.0}))
    if related:
        parts.append("What the organization's other reviewed policies say about the same control:\n"
                     + "\n".join(related))
    return "\n\n".join(parts) or "No evidence text is available for this finding."


def _run_search(args: dict, sources: _Sources) -> str:
    doc_type = None if args.get("doc_type") in (None, "any") else args["doc_type"]
    try:
        hits = retrieval.search(args.get("query", ""), type=doc_type, top_k=5)
    except Exception as e:
        return f"Search failed: {e}"
    if not hits:
        return "No matching passages."
    return "\n\n---\n\n".join(sources.render(h) for h in hits)


# --------------------------------------------------------------- citations

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


# ------------------------------------------------------------------ agent

def _history_text(message: dict) -> str:
    """An earlier turn's text; an assistant turn also lists the sources its [n] markers point to."""
    text = str(message["content"])
    if message["role"] == "assistant" and message.get("citations"):
        listed = "; ".join(
            f"[{i}] " + ", ".join(str(x) for x in (c.get("source"), c.get("locator")) if x)
            for i, c in enumerate(message["citations"], start=1) if isinstance(c, dict))
        text += f"\n(Sources this answer cited, already checked: {listed})"
    return text


def _history_messages(history) -> list[dict]:
    """Earlier turns as API messages: text only, skipping error replies; starts with a user turn."""
    messages = [{"role": m["role"], "content": _history_text(m)}
                for m in (history or [])
                if isinstance(m, dict) and m.get("role") in ("user", "assistant")
                and m.get("content") and not m.get("error")][-MAX_HISTORY_MESSAGES:]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages


def answer(question: str, finding: dict | None = None, history: list[dict] | None = None,
           findings=None) -> dict:
    """
    Answer one question. Returns {"answer", "citations" (search() results, in
    [1], [2] order), "rejected_citations", "suggestions" ([{"finding_id",
    "recommendation"}]), and, in finding-focused mode, "proposed_recommendation"}.
    """
    started = time.time()
    sources = _Sources()
    review = _normalize_findings(findings)
    focus = None
    if finding is not None:
        focus = next(iter(_normalize_findings([finding]).values()), None)
        if focus:
            review.setdefault(focus["id"], focus)

    context = []
    if review:
        context.append(_review_summary(review))
    if focus:
        context.append(f"The reviewer is asking about finding {focus['id']}. Its evidence:\n"
                       + _finding_evidence(focus, sources))
    user_content = "\n\n".join(context + [f"Reviewer's question: {question}"]) if context else question
    messages = _history_messages(history) + [{"role": "user", "content": user_content}]

    ids = list(review)
    tools = [SEARCH_TOOL] + ([_evidence_tool(ids), _propose_tool(ids)] if ids else [])
    suggestions, usage, text = {}, {"input": 0, "output": 0}, ""
    try:
        for _ in range(MAX_TOOL_ROUNDS + 1):
            response = _get_client().messages.create(
                model=MODEL, max_tokens=3000, system=SYSTEM_PROMPT, tools=tools, messages=messages)
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
                args = block.input or {}
                if block.name == "search_documents":
                    content = _run_search(args, sources)
                elif block.name == "get_finding_evidence" and args.get("finding_id") in review:
                    content = _finding_evidence(review[args["finding_id"]], sources)
                elif block.name == "propose_recommendation" and args.get("finding_id") in review:
                    proposal = (args.get("recommendation") or "").strip()
                    if proposal:
                        suggestions[args["finding_id"]] = proposal
                    content = "Suggestion recorded. The reviewer will decide whether to use it."
                else:
                    content = f"Unknown tool or finding: {block.name} {json.dumps(args)}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
            messages.append({"role": "user", "content": results})
        else:
            text = "I couldn't finish answering within the tool limit. Try a more specific question."
    except anthropic.AuthenticationError:
        text = "The Q&A agent can't reach Claude: the Anthropic API key was rejected."
    except anthropic.RateLimitError:
        text = "Claude is rate-limiting requests right now. Please try again in a moment."
    except anthropic.APIStatusError as e:
        text = f"Claude returned an error ({e.status_code}). Please try again."
    except anthropic.APIConnectionError:
        text = "Couldn't connect to Claude. Check the internet connection."

    text, citations, rejected = _check_citations(text, sources)
    result = {
        "answer": text.strip(),
        "citations": citations,
        "rejected_citations": rejected,
        "suggestions": [{"finding_id": k, "recommendation": v} for k, v in suggestions.items()],
    }
    if focus and focus["id"] in suggestions:
        result["proposed_recommendation"] = suggestions[focus["id"]]
    _log(question, focus, review, sources, result, usage, time.time() - started)
    return result


def ask(question: str, findings=None, history=None) -> dict:
    """
    Dashboard Q&A contract: {"answer": str, "citations": [search() results],
    "suggestions": [{"finding_id", "recommendation"}]}. findings (the review's
    Finding objects or dicts) and history (earlier {"role", "content"} chat
    messages) are optional.
    """
    result = answer(question, history=history, findings=findings)
    return {"answer": _plain(result["answer"]), "citations": result["citations"],
            "suggestions": result["suggestions"]}


def _plain(text: str) -> str:
    """Drop leftover Markdown markers; the dashboard shows answers as one line of plain text."""
    text = re.sub(r"(\*\*|__|`)", "", text)
    text = re.sub(r"^\s*(#{1,6}\s+|[-*]\s+)", "", text, flags=re.M)
    return re.sub(r"\s*\n\s*", " ", text).strip()


_LOG_COLUMNS = {
    "ts": "TEXT", "model": "TEXT", "finding": "TEXT", "question": "TEXT",
    "retrieved_chunk_ids": "TEXT", "cited_chunk_ids": "TEXT", "rejected_refs": "TEXT",
    "answer": "TEXT", "proposed_recommendation": "TEXT",
    "input_tokens": "INTEGER", "output_tokens": "INTEGER", "seconds": "REAL",
}


def _log(question, focus, review, sources, result, usage, seconds) -> None:
    try:
        LOG_DB.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(LOG_DB) as db:
            db.execute("CREATE TABLE IF NOT EXISTS chat_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       + ", ".join(f"{name} {kind}" for name, kind in _LOG_COLUMNS.items()) + ")")
            # A log made by an older version may lack newer columns: add them.
            existing = {row[1] for row in db.execute("PRAGMA table_info(chat_log)")}
            for name, kind in _LOG_COLUMNS.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE chat_log ADD COLUMN {name} {kind}")
            db.execute(
                "INSERT INTO chat_log (ts, model, finding, question, retrieved_chunk_ids, cited_chunk_ids,"
                " rejected_refs, answer, proposed_recommendation, input_tokens, output_tokens, seconds)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), MODEL,
                 focus["id"] if focus else (f"{len(review)} findings in context" if review else None),
                 question, json.dumps(list(sources.by_chunk)),
                 json.dumps([c["chunk_id"] for c in result["citations"]]),
                 json.dumps(result["rejected_citations"]), result["answer"],
                 json.dumps(result["suggestions"]) if result["suggestions"] else None,
                 usage["input"], usage["output"], round(seconds, 2)))
    except sqlite3.Error as e:
        # Logging must never break the chat, but a failure shouldn't go unnoticed.
        print(f"[qa_agent] chat log not written ({LOG_DB}): {e}", file=sys.stderr)
