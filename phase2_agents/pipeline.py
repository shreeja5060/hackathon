"""
Phase 2: The full pipeline, connected with LangGraph.

This wraps extractor.py, mapper.py, and auditor.py (which you already built
and tested separately) into ONE connected workflow: Extractor -> Mapper ->
Auditor -> Human Review. Nothing about those three functions changes - we're
just adding a conductor on top that passes data between them and pauses for
a human at the end.

Key new ideas today:
  - STATE: a shared dictionary that flows through every node, growing as it
    goes (this chunk -> + extracted requirements -> + mappings -> + gap report)
  - NODES: each node is just a Python function that takes the state in and
    returns updates to it
  - EDGES: the connections saying which node runs after which
  - INTERRUPT: a special point where the graph PAUSES and waits for a human
    decision before continuing - this is your required human-review step
"""

import os
import json
from typing import TypedDict
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver

import sys
# Explicitly add this script's own folder AND the shared folder to the
# import path. This avoids relying on how Python happens to resolve the
# "current directory" when the script is launched from somewhere else.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(_THIS_DIR)
sys.path.append(os.path.join(_THIS_DIR, "..", "shared"))

from fake_search import search
from extractor import extract_requirements
from mapper import map_requirement
from auditor import audit_requirement

load_dotenv()


# This TypedDict describes the SHAPE of our state - what data exists as it
# flows through the graph. Every node can read from this and add to it.
class PipelineState(TypedDict):
    policy_chunk: dict          # the input: one chunk of policy text
    requirements: list          # filled in by the Extractor node
    mapped: list                 # filled in by the Mapper node
    gap_report: list             # filled in by the Auditor node
    approved_findings: list      # filled in after human review


def extractor_node(state: PipelineState) -> dict:
    """LangGraph node = a function that reads state, does work, returns updates."""
    print("\n[Extractor] Reading policy chunk...")
    requirements = extract_requirements(state["policy_chunk"])
    print(f"[Extractor] Found {len(requirements)} requirement(s)")
    return {"requirements": requirements}


def mapper_node(state: PipelineState) -> dict:
    print("\n[Mapper] Matching requirements to NIST controls...")
    mapped = [map_requirement(req) for req in state["requirements"]]
    for m in mapped:
        print(f"[Mapper]   {m['requirement']} -> {m['mapped_control']}")
    return {"mapped": mapped}


def auditor_node(state: PipelineState) -> dict:
    print("\n[Auditor] Classifying coverage and drafting recommendations...")
    gap_report = [audit_requirement(req) for req in state["mapped"]]
    for g in gap_report:
        print(f"[Auditor]   {g['requirement']}: {g['coverage']}")
    return {"gap_report": gap_report}


def human_review_node(state: PipelineState) -> dict:
    """
    This is the required MVP step: the graph PAUSES here and hands control
    back to a human. interrupt() stops execution and returns whatever we
    pass it - the pending findings - so a person (or our dashboard, later)
    can look at them before anything is marked final.
    """
    print("\n[Human Review] Pausing for approval...")
    decision = interrupt({
        "message": "Please review these findings",
        "findings": state["gap_report"]
    })
    # Once a human responds (see how we resume below), `decision` holds
    # whatever they sent back - here, which findings they approved.
    return {"approved_findings": decision["approved"]}


# Build the graph: register each node, then wire the edges (the order
# they run in).
graph = StateGraph(PipelineState)
graph.add_node("extractor", extractor_node)
graph.add_node("mapper", mapper_node)
graph.add_node("auditor", auditor_node)
graph.add_node("human_review", human_review_node)

graph.set_entry_point("extractor")
graph.add_edge("extractor", "mapper")
graph.add_edge("mapper", "auditor")
graph.add_edge("auditor", "human_review")
graph.add_edge("human_review", END)

# A checkpointer lets the graph remember where it paused, so it can resume
# later - necessary for the interrupt to work.
checkpointer = MemorySaver()
app = graph.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    # Grab one real policy chunk to run through the whole pipeline
    chunk = search("authentication", type="internal")[0]

    config = {"configurable": {"thread_id": "demo-run-1"}}

    # First run: goes Extractor -> Mapper -> Auditor -> then PAUSES at
    # human_review and returns control to us here.
    result = app.invoke({"policy_chunk": chunk}, config=config)
    print("\n--- Paused for human review ---")
    print(json.dumps(result, indent=2, default=str))

    # Simulate a human approving every finding (in the real dashboard, this
    # comes from someone clicking "approve" on each row).
    print("\n[Simulating human approval of all findings...]")
    final_result = app.invoke(
        Command(resume={"approved": result["__interrupt__"][0].value["findings"]}),
        config=config
    )

    print("\n--- Final result after human approval ---")
    print(json.dumps(final_result["approved_findings"], indent=2))
