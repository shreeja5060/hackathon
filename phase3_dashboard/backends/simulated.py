"""Simulator: produces what Phase 1 and Phase 2 produce, without needing them.

Use it to build and demo the dashboard before the phase1 and phase2 branches
are merged. Nothing here calls Claude or reads the real index.

What it mirrors, and from which file:

* policy chunks .... phase1_ingestion/chunk_policies.py (same fields, IDs and locators)
* search() ......... phase1_ingestion/retriever.py (same 8 fields, same argument checks)
* Extractor ........ phase2_agents/extractor.py  -> requirement, requirement_text, source, chunk_id, locator
* Mapper ........... phase2_agents/mapper.py     -> adds mapped_control, mapping_reasoning (in place)
* Auditor .......... phase2_agents/auditor.py    -> the gap-report entry, status "pending"
* the graph ........ phase2_agents/pipeline.py   -> pauses at human_review with {"message", "findings"},
                                                     resumes with {"approved": [...]}

Keyword rules stand in for Claude's judgment, so the findings are plausible
but are not real compliance results.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ..core.security import UploadRejected, check_upload
from . import sample_data as data
from .base import BackendUnavailable, ComplianceBackend, PolicyDocument
from .pdf_text import parse_pdf_pages

POLICY_TYPE = "internal"  # chunk_policies.py's default role for policies


# ======================================================================
# Phase 1 mirrors: chunks and search()
# ======================================================================

_HEADING = re.compile(r"^(\d+(?:\.\d+)*)(?:\.)?\s+([A-Z].*)$")
_FOOTER = "Public template | Organization-specific and security-sensitive details removed"


def _chunk_id(prefix: str, identity: list) -> str:
    encoded = json.dumps(identity, ensure_ascii=False).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()}"


def make_policy_chunk(source: str, page: int, heading: str, body_text: str) -> dict:
    """Same fields and ID scheme as chunk_policies.make_chunk()."""
    locator = f"Section {heading}"
    text = f"{heading}\n\n{body_text}"
    return {
        "chunk_id": _chunk_id("policy", [source, page, locator, text]),
        "text": text,
        "source": source,
        "type": POLICY_TYPE,
        "doc_kind": "policy",
        "page": page,
        "locator": locator,
    }


def make_framework_chunk(control_id: str, control: dict) -> dict:
    """Shaped like load_frameworks.py output, with a clearly labelled paraphrase."""
    family = control_id.split("-")[0]
    text = (
        f"NIST SP 800-53 Release {data.FRAMEWORK_INFO['version']} | {control_id}: {control['title']}\n\n"
        f"Context: {family}: {data.FAMILIES[family]}\n\n"
        f"Control statement:\n[Simulated summary, not NIST's wording] {control['summary']}"
    )
    return {
        "chunk_id": _chunk_id("framework", [data.SP80053_SOURCE, "simulated", control_id, text]),
        "text": text,
        "source": data.SP80053_SOURCE,
        "type": "framework",
        "doc_kind": "control_catalog",
        "page": None,
        "locator": control_id,
    }


def _clean_line(line: str):
    line = re.sub(r"\s+", " ", line).strip()
    if line in {"PUBLIC RELEASE", _FOOTER}:
        return None
    return line.replace("\uf0b7", "•")


def _join_paragraphs(lines) -> str:
    paragraphs, current = [], ""
    for line in lines:
        if not line or line.startswith("•"):
            if current:
                paragraphs.append(current)
            current = line
        elif current:
            current += ("" if current.endswith("-") else " ") + line
        else:
            current = line
    if current:
        paragraphs.append(current)
    return "\n\n".join(paragraphs)


def chunk_pages_like_phase1(pages: list[dict]) -> tuple[list[dict], list[str]]:
    """Port of chunk_policies.chunk_pages() for one uploaded file. Returns (chunks, notes)."""
    chunks: list[dict] = []
    heading = None
    for record in pages:
        body: list[str] = []
        for raw_line in record["text"].splitlines():
            line = _clean_line(raw_line)
            if line is None:
                continue
            match = _HEADING.fullmatch(line)
            if match:
                if heading and (text := _join_paragraphs(body)):
                    chunks.append(make_policy_chunk(record["source"], record["page"], heading, text))
                heading = f"{match.group(1)} {match.group(2)}"
                body = []
            elif heading is not None:
                body.append(line)
        if heading and (text := _join_paragraphs(body)):
            chunks.append(make_policy_chunk(record["source"], record["page"], heading, text))

    if heading is not None:
        return chunks, []

    # Phase 1 rejects PDFs without numbered headings. The simulator is more
    # forgiving so you can still try any file: one section per page.
    fallback = []
    for record in pages:
        text = _join_paragraphs(line for line in map(_clean_line, record["text"].splitlines()) if line is not None)
        if text:
            fallback.append({
                "chunk_id": _chunk_id("policy", [record["source"], record["page"], None, text]),
                "text": text, "source": record["source"], "type": POLICY_TYPE,
                "doc_kind": "policy", "page": record["page"], "locator": None,
            })
    note = ("No numbered section headings were found, so each page became one section. "
            "Phase 1's chunker rejects files like this, so this upload will fail in live mode.")
    return fallback, [note]


_STOPWORDS = frozenset(
    "a about all an and any are as at be by can do does for from has have how i if in into is it its "
    "may must not of on or our say says should tell that the their them there these this those to was "
    "we what when where which who will with you your policy policies".split()
)


def _stem(word: str) -> str:
    for suffix in ("ions", "ion", "ing", "ed", "es", "s", "e"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _terms(text: str) -> Counter:
    return Counter(_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS and len(w) > 1)


def simulated_search(corpus: list[dict], query, type=None, top_k=5) -> list[dict]:  # noqa: A002 - mirrors retriever.search
    """Same signature, checks and 8-field results as retriever.search(); word overlap instead of embeddings."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string.")
    if type not in (None, "internal", "framework", "evidence"):
        raise ValueError("type must be None, internal, framework, or evidence.")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError("top_k must be a positive integer.")

    query_terms = _terms(query)
    if not query_terms:
        return []
    query_norm = math.sqrt(sum(v * v for v in query_terms.values()))
    scored = []
    for chunk in corpus:
        if type is not None and chunk["type"] != type:
            continue
        terms = _terms(chunk["text"])
        if not terms:
            continue
        dot = sum(count * terms.get(term, 0) for term, count in query_terms.items())
        score = dot / (query_norm * math.sqrt(sum(v * v for v in terms.values())))
        locator = chunk.get("locator") or ""
        if chunk["type"] == "framework" and re.search(rf"(?<![\w(-]){re.escape(locator)}(?![\w(])", query):
            score = max(score, 0.9)  # direct control-ID lookups such as "IA-5"
        scored.append((score, chunk))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["chunk_id"]))
    results = []
    for score, chunk in scored[:top_k]:
        record = {key: chunk.get(key) for key in ("chunk_id", "text", "source", "page", "type", "doc_kind", "locator")}
        record["score"] = round(max(-1.0, min(1.0, score)), 4)
        results.append(record)
    return results


# ======================================================================
# Phase 2 mirrors: Extractor, Mapper, Auditor
# ======================================================================

_CUE = re.compile(
    r"\b(?:must|shall|should|(?:is|are) required to|(?:is|are) prohibited|(?:is|are) responsible for)\b", re.I
)
_VAGUE = re.compile(
    r"\b(?:where required|as required|where appropriate|as appropriate|when appropriate|as needed|"
    r"when needed|where necessary|as necessary|periodically|regularly|routinely|in a timely manner|timely|"
    r"promptly|reasonable|adequate|where possible|when possible|strong)\b", re.I
)
_SPECIFIC = re.compile(
    r"\b(?:\d+|annual(?:ly)?|quarterly|monthly|weekly|daily|immediately|at least|within|no later than)\b", re.I
)
_TOPICS = tuple((re.compile(pattern, re.I), name, control) for pattern, name, control in data.TOPIC_RULES)
MAX_REQUIREMENTS_PER_CHUNK = 4


def match_topic(text: str):
    """(requirement name, control id) for the first matching rule, or None."""
    for pattern, name, control in _TOPICS:
        if text and pattern.search(text):
            return name, control
    return None


def _heading_and_body(chunk: dict) -> tuple[str, str]:
    if chunk.get("locator"):
        heading, _, body = chunk["text"].partition("\n\n")
        return heading, body
    return "", chunk["text"]


def _sentences(text: str) -> list[str]:
    sentences = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = " ".join(paragraph.split())
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z•])", paragraph):
            sentence = sentence.strip(" •")
            if sentence:
                sentences.append(sentence)
    return sentences


def _fallback_name(heading: str, sentence: str) -> str:
    """Short name when no rule matches: the section title, else the first few words."""
    title = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", heading).strip()
    if title:
        return title[0].upper() + title[1:].lower()
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", sentence)[:6]
    return " ".join(words).capitalize() or "Policy requirement"


def simulated_extract_requirements(chunk: dict) -> list[dict]:
    """Same output as extractor.extract_requirements(chunk)."""
    heading, body = _heading_and_body(chunk)
    requirements, used = [], Counter()
    for sentence in _sentences(body):
        if len(sentence) < 20 or not _CUE.search(sentence):
            continue
        topic = match_topic(sentence) or match_topic(heading)
        name = topic[0] if topic else _fallback_name(heading, sentence)
        used[name] += 1
        if used[name] > 1:
            name = f"{name} ({used[name]})"
        requirements.append({"requirement": name, "requirement_text": sentence})
        if len(requirements) == MAX_REQUIREMENTS_PER_CHUNK:
            break
    for requirement in requirements:  # extractor.py attaches the citation like this
        requirement["source"] = chunk["source"]
        requirement["chunk_id"] = chunk["chunk_id"]
        requirement["locator"] = chunk.get("locator")
    return requirements


def simulated_map_requirement(requirement: dict) -> dict:
    """Same as mapper.map_requirement(): adds mapped_control and mapping_reasoning in place."""
    base_name = re.sub(r" \(\d+\)$", "", requirement["requirement"])
    topic = match_topic(requirement["requirement_text"]) or match_topic(base_name)
    if topic is None:
        requirement["mapped_control"] = None
        requirement["mapping_reasoning"] = "None of the candidate controls genuinely match this requirement."
    else:
        name, control_id = topic
        title = data.CONTROLS[control_id]["title"]
        requirement["mapped_control"] = control_id
        requirement["mapping_reasoning"] = f"The requirement is about {name.lower()}, which {control_id} ({title}) covers."
    return requirement


def _bucket(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:8], 16) % 100


def simulated_audit_requirement(mapped: dict) -> dict:
    """Same gap-report entry as auditor.audit_requirement()."""
    control_id = mapped["mapped_control"]
    text = mapped["requirement_text"]

    if control_id is None:
        coverage = "Not observable"
        finding = ("This requirement doesn't match a NIST SP 800-53 control, so coverage can't be judged "
                   "from the policy text alone.")
        recommendation = "Confirm which control this supports, or mark it as organization-specific guidance."
    else:
        control = data.CONTROLS[control_id]
        label = f"{control_id} ({control['title']})"
        vague = _VAGUE.search(text)
        if not re.search(control["core"], text, re.I):
            coverage = "Missing"
            finding = (f"The text covers {mapped['requirement'].lower()} but doesn't require "
                       f"{control['core_desc']}, which is the core of {label}.")
            recommendation = control["recommendation"]
        elif vague:
            coverage = "Partial"
            finding = f"Scope unclear: \"{vague.group(0)}\" isn't defined, so it's hard to verify {label}."
            recommendation = control["recommendation"]
        elif _SPECIFIC.search(text):
            coverage = "Full"
            finding = f"States a specific, checkable expectation that meets the intent of {label}."
            recommendation = "None"
        elif _bucket(text, control_id) < 55:
            coverage = "Partial"
            finding = f"Addresses {label} but doesn't say who is responsible or how compliance is checked."
            recommendation = control["recommendation"]
        else:
            coverage = "Full"
            finding = f"Clearly addresses the intent of {label}."
            recommendation = "None"

    return {
        "requirement": mapped["requirement"],
        "coverage": coverage,
        "finding": finding,
        "recommendation": recommendation,
        "citation": {
            "chunk_id": mapped["chunk_id"],
            "source": mapped["source"],
            "locator": mapped["locator"],
        },
        "framework_control": control_id,
        "status": "pending",
    }


# ======================================================================
# Phase 2 mirror: the LangGraph pipeline
# ======================================================================

@dataclass
class SimulatedInterrupt:
    """Stands in for langgraph.types.Interrupt; the dashboard only reads .value."""

    value: Any


@dataclass
class SimulatedCommand:
    """Stands in for langgraph.types.Command(resume=...)."""

    resume: Any = None


class SimulatedPipeline:
    """Behaves like the compiled graph in pipeline.py.

    invoke({"policy_chunk": chunk}, config) runs Extractor -> Mapper -> Auditor
    and pauses at human_review. invoke(SimulatedCommand(resume={"approved": [...]}),
    config) resumes that thread and returns the state with approved_findings.
    """

    def __init__(self, delay: float = 0.0, fail_section_index: int | None = None):
        self.delay = delay
        self.fail_section_index = fail_section_index
        self._threads: dict[str, dict] = {}
        self._lock = threading.Lock()

    def invoke(self, payload, config):
        thread_id = config["configurable"]["thread_id"]
        if isinstance(payload, SimulatedCommand):
            return self._resume(thread_id, payload.resume)

        chunk = payload["policy_chunk"]
        if self.delay:
            time.sleep(self.delay)
        if self.fail_section_index is not None and thread_id.endswith(f"-s{self.fail_section_index:03d}"):
            # What a bad model reply looks like inside extractor.py: json.loads() fails.
            json.loads("Sure! Here are the requirements I found:\n[")

        requirements = simulated_extract_requirements(chunk)
        mapped = [simulated_map_requirement(requirement) for requirement in requirements]
        gap_report = [simulated_audit_requirement(item) for item in mapped]
        state = {"policy_chunk": chunk, "requirements": requirements, "mapped": mapped, "gap_report": gap_report}
        with self._lock:
            self._threads[thread_id] = dict(state)
        pause = SimulatedInterrupt(value={"message": "Please review these findings", "findings": gap_report})
        return {**state, "__interrupt__": [pause]}

    def _resume(self, thread_id: str, decision) -> dict:
        with self._lock:
            state = self._threads.pop(thread_id, None)
        if state is None:
            raise RuntimeError(f"There is no paused run for thread {thread_id}.")
        state["approved_findings"] = decision["approved"]
        return state


def simulated_answer(corpus: list[dict], question: str) -> dict:
    """Stands in for the Q&A agent: {"answer", "citations"} with search() results as citations."""
    hits = simulated_search(corpus, question, top_k=5)
    relevant = [hit for hit in hits if hit["score"] >= 0.15]
    if not relevant:
        return {
            "answer": "Simulated answer: I couldn't find this in the loaded policies or NIST controls. "
                      "Try different words.",
            "citations": [],
        }
    parts = ["Simulated answer (keyword matching, not Claude)."]
    policy = next((hit for hit in relevant if hit["type"] == "internal"), None)
    control = next((hit for hit in relevant if hit["type"] == "framework"), None)
    if policy:
        heading, body = _heading_and_body(policy)
        sentences = _sentences(body)
        quote = next((s for s in sentences if _CUE.search(s)), sentences[0] if sentences else body)
        where = policy["locator"] or f"page {policy['page']}"
        parts.append(f"{policy['source']}, {where}, says: \"{quote}\"")
    if control:
        title = control["text"].split("\n", 1)[0].split(": ", 1)[-1]
        parts.append(f"The closest NIST SP 800-53 control is {control['locator']} ({title}).")
    return {"answer": " ".join(parts), "citations": relevant[:3]}


# ======================================================================
# The backend the dashboard uses
# ======================================================================

class SimulatedBackend(ComplianceBackend):
    name = "Simulator"
    simulated = True

    def __init__(self, delay: float = 0.3, fail_section_index: int | None = None):
        self.pipeline = SimulatedPipeline(delay=delay, fail_section_index=fail_section_index)
        super().__init__(self.pipeline, SimulatedCommand)
        self._samples = {
            source: PolicyDocument(
                source=source,
                chunks=[make_policy_chunk(source, page, heading, body) for page, heading, body in sections],
                origin="sample",
                notes=["Invented sample policy for the simulator, not one of the team's real PDFs."],
            )
            for source, sections in data.SAMPLE_POLICIES.items()
        }
        self._uploads: dict[str, PolicyDocument] = {}
        self._framework_chunks = [make_framework_chunk(cid, control) for cid, control in data.CONTROLS.items()]

    def set_failing_section(self, index: int | None) -> None:
        self.pipeline.fail_section_index = index

    def list_policies(self) -> list[str]:
        return list(self._samples)

    def load_policy(self, source: str) -> PolicyDocument:
        if source not in self._samples:
            raise BackendUnavailable(f"There is no sample policy called {source}.")
        return self._samples[source]

    def parse_upload(self, filename: str, data_bytes: bytes) -> PolicyDocument:
        name = check_upload(filename, data_bytes)
        pages = parse_pdf_pages(name, data_bytes)
        chunks, notes = chunk_pages_like_phase1(pages)
        if not chunks:
            raise UploadRejected("No text sections were found in this PDF.")
        document = PolicyDocument(
            source=name, chunks=chunks, origin="upload",
            notes=notes + ["Simulator: findings for this file come from keyword rules, not Claude."],
        )
        self._uploads[name] = document
        return document

    def framework_info(self) -> dict:
        return dict(data.FRAMEWORK_INFO)

    def corpus(self) -> list[dict]:
        documents = [*self._samples.values(), *self._uploads.values()]
        return [chunk for document in documents for chunk in document.chunks] + self._framework_chunks

    def search(self, query, type=None, top_k=5):  # noqa: A002
        return simulated_search(self.corpus(), query, type=type, top_k=top_k)

    def ask(self, question: str) -> dict:
        return simulated_answer(self.corpus(), question)
