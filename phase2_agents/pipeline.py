"""
Phase 2: the full pipeline as a LangGraph graph, with a real human-review
checkpoint, exposed as two functions the dashboard can call.

    run = start_review(chunks)            # runs Extractor -> Mapper -> Auditor on a whole
                                          # document, then PAUSES. Returns pending findings.
    final = submit_decisions(run["run_id"], decisions)
                                          # the human's approve/reject choices resume the
                                          # paused run; returns the final findings.

Why a graph instead of a loop? The review step is not a UI flag: the pipeline
is genuinely suspended at `human_review` until a person decides. Nothing is
final until `submit_decisions` is called. That is the human-oversight
requirement implemented in the pipeline itself.

State flows through four nodes:
    extract -> map -> audit -> human_review -> END
Each agent node isolates per-item failures (logged in state["errors"]) so one
bad chunk never stops the document, same as run_full_pipeline.py.
"""

import os
import sys
import json
import uuid
from typing import TypedDict, Optional
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver

# Retrieval backend: use Maryam's real retriever (phase1_ingestion) when its
# dependencies are installed (Linux / Apple Silicon / Cloud Shell / Docker);
# fall back to the placeholder fake_search on machines that can't run it
# (e.g. Intel Macs). Same search() / get_original_chunk() interface either way.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(_HERE)
sys.path.append(os.path.join(_HERE, "..", "shared"))
sys.path.append(os.path.join(_HERE, "..", "phase1_ingestion"))
try:
    from retriever import search, get_original_chunk
    RETRIEVER_BACKEND = "real (phase1_ingestion/retriever.py)"
except Exception:
    from fake_search import search, get_original_chunk
    RETRIEVER_BACKEND = "placeholder (shared/fake_search.py)"

from extractor import extract_requirements
from mapper import map_requirement
from auditor import audit_requirement

load_dotenv()

VALID_DECISIONS = {"approved", "rejected"}


class PipelineState(TypedDict):
    chunks: list            # input: the document's policy chunks
    requirements: list      # after extract
    mapped: list            # after map
    findings: list          # after audit (status = "pending")
    final_findings: list    # after human review (status = approved/rejected)
    errors: list            # per-item failures, never fatal


def extract_node(state: PipelineState) -> dict:
    requirements, errors = [], []
    for chunk in state["chunks"]:
        try:
            requirements.extend(extract_requirements(chunk))
        except Exception as e:
            errors.append({"stage": "extractor", "chunk_id": chunk.get("chunk_id"), "error": str(e)})
    return {"requirements": requirements, "errors": state.get("errors", []) + errors}


def map_node(state: PipelineState) -> dict:
    mapped, errors = [], []
    for req in state["requirements"]:
        try:
            mapped.append(map_requirement(req))
        except Exception as e:
            errors.append({"stage": "mapper", "requirement": req.get("requirement"), "error": str(e)})
    return {"mapped": mapped, "errors": state.get("errors", []) + errors}


def audit_node(state: PipelineState) -> dict:
    findings, errors = [], []
    for req in state["mapped"]:
        try:
            findings.append(audit_requirement(req))
        except Exception as e:
            errors.append({"stage": "auditor", "requirement": req.get("requirement"), "error": str(e)})
    # give every finding a stable id the dashboard can refer back to
    for i, f in enumerate(findings):
        f["finding_id"] = f"F{i+1:03d}"
    return {"findings": findings, "errors": state.get("errors", []) + errors}


def human_review_node(state: PipelineState) -> dict:
    """
    The checkpoint. interrupt() suspends the run and hands the pending
    findings out. The run stays suspended until submit_decisions() resumes
    it with the human's choices; only then are statuses set.
    """
    decisions = interrupt({
        "message": "Review these findings. Nothing is final until you decide.",
        "findings": state["findings"],
    })
    # decisions: {finding_id: {"decision": "approved"|"rejected",
    #                          "recommendation": optional edited text}}
    final = []
    for f in state["findings"]:
        d = decisions.get(f["finding_id"], {})
        decision = d.get("decision", "pending")
        if decision not in VALID_DECISIONS:
            decision = "pending"          # undecided items stay pending, not silently approved
        out = dict(f)
        out["status"] = decision
        if d.get("recommendation"):
            out["recommendation"] = d["recommendation"]
            out["recommendation_edited_by_human"] = True
        final.append(out)
    return {"final_findings": final}


def build_graph():
    g = StateGraph(PipelineState)
    g.add_node("extract", extract_node)
    g.add_node("map", map_node)
    g.add_node("audit", audit_node)
    g.add_node("human_review", human_review_node)
    g.set_entry_point("extract")
    g.add_edge("extract", "map")
    g.add_edge("map", "audit")
    g.add_edge("audit", "human_review")
    g.add_edge("human_review", END)
    return g


# One checkpointer for the process: it remembers every paused run by run_id.
# (In-memory is fine for a single Streamlit process; swap for SqliteSaver if
# the app ever needs to survive a restart mid-review.)
_checkpointer = MemorySaver()
app = build_graph().compile(checkpointer=_checkpointer)


# ---------------------------------------------------------------------------
# The two functions the dashboard calls
# ---------------------------------------------------------------------------

def start_review(chunks: list[dict], run_id: Optional[str] = None) -> dict:
    """
    Run a document's chunks through Extractor -> Mapper -> Auditor and pause
    for human review. Returns the pending findings and the run_id needed to
    resume. Nothing is approved yet.
    """
    run_id = run_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": run_id}}
    result = app.invoke({"chunks": chunks, "requirements": [], "mapped": [],
                         "findings": [], "final_findings": [], "errors": []},
                        config=config)
    paused = result.get("__interrupt__")
    findings = paused[0].value["findings"] if paused else result.get("findings", [])
    return {
        "run_id": run_id,
        "status": "awaiting_human_review" if paused else "complete",
        "findings": findings,
        "errors": result.get("errors", []),
        "backend": RETRIEVER_BACKEND,
    }


def submit_decisions(run_id: str, decisions: dict) -> dict:
    """
    Resume a paused run with the human's decisions and return the final
    findings. `decisions` maps finding_id -> {"decision": "approved"|"rejected",
    "recommendation": optional edited text}.
    """
    config = {"configurable": {"thread_id": run_id}}
    result = app.invoke(Command(resume=decisions), config=config)
    final = result.get("final_findings", [])
    return {
        "run_id": run_id,
        "status": "complete",
        "findings": final,
        "approved": [f for f in final if f["status"] == "approved"],
        "rejected": [f for f in final if f["status"] == "rejected"],
        "pending": [f for f in final if f["status"] == "pending"],
        "errors": result.get("errors", []),
    }


def get_pending(run_id: str) -> Optional[dict]:
    """Look up a paused run's pending findings (e.g. after a page refresh)."""
    config = {"configurable": {"thread_id": run_id}}
    state = app.get_state(config)
    if not state or not state.tasks:
        return None
    for task in state.tasks:
        if task.interrupts:
            return task.interrupts[0].value
    return None


if __name__ == "__main__":
    # Demo: one chunk, pause, then simulate a human approving the first finding
    # and rejecting the rest.
    print(f"Retrieval backend: {RETRIEVER_BACKEND}")
    chunks = search("authentication", type="internal", top_k=1)
    run = start_review(chunks)
    print(f"\nPaused ({run['status']}), {len(run['findings'])} finding(s) awaiting review:")
    for f in run["findings"]:
        print(f"  {f['finding_id']}  {f['requirement']}: {f['coverage']}")

    decisions = {f["finding_id"]: {"decision": "approved" if i == 0 else "rejected"}
                 for i, f in enumerate(run["findings"])}
    final = submit_decisions(run["run_id"], decisions)
    print(f"\nResumed. approved={len(final['approved'])} rejected={len(final['rejected'])} "
          f"pending={len(final['pending'])}")
    print(json.dumps(final["approved"], indent=2)[:800])
