"""
Phase 2: The Auditor Agent.

Takes a mapped requirement (from mapper.py) and decides how well it covers
the matched NIST control: Full, Partial, Missing, or Not observable. Drafts
a recommendation for anything less than Full. This is the agent that
produces your actual gap report - the thing judges will look at.

Same call pattern as Day 1 and Day 2. The new idea today: we give Claude a
FIXED SET of allowed answers (Full/Partial/Missing/Not observable) rather
than open text, because the dashboard needs to color-code and sort by this
value reliably. This is called "constrained output" - naming the exact
options the model can choose from, so you don't get "Mostly Full" or
"Pretty good coverage" randomly showing up.
"""

import os
import sys
import json
from dotenv import load_dotenv
from anthropic import Anthropic


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

load_dotenv()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


AUDITOR_PROMPT = """You are a compliance auditor. Compare the policy \
requirement below against the NIST control it was mapped to, and classify \
how well the policy covers that control.

Policy requirement: "{requirement_text}"
Matched NIST control ({control_id}): "{control_text}"

Classify coverage using EXACTLY one of these four values (no other wording):
- "Full": the policy fully satisfies the control's intent
- "Partial": the policy addresses the control but leaves something undefined, \
vague, or incomplete (e.g. "where required" without saying where)
- "Missing": the policy does not address this control at all
- "Not observable": you cannot tell from the text given

Then write a one-sentence finding explaining the classification, and if the \
classification is not "Full", a one-sentence recommendation to fix the gap.

Also write a PLAIN-LANGUAGE explanation suitable for someone new to \
cybersecurity compliance - avoid jargon and control IDs, use a simple \
analogy if it helps. And if you are not fully confident in this \
classification, list 1-2 clarifying questions a human reviewer should \
answer to judge this properly (e.g. "which systems count as privileged?"). \
If you are confident, return an empty list for clarifying_questions.

Return ONLY JSON in this exact shape:
{{"coverage": "Full|Partial|Missing|Not observable", "finding": "...", "recommendation": "...", "plain_language": "...", "clarifying_questions": ["..."]}}

If coverage is "Full", set recommendation to "None" and clarifying_questions to [].
"""


def audit_requirement(mapped_requirement: dict) -> dict:
    """
    Takes one mapped requirement (has requirement_text + mapped_control) and
    returns the final gap-report entry: coverage, finding, recommendation.
    """
    control_id = mapped_requirement["mapped_control"]

    # If the Mapper found no matching control at all, there is nothing to
    # audit against - this is itself a finding worth surfacing, not a crash.
    if not control_id:
        return {
            "requirement": mapped_requirement["requirement"],
            "coverage": "Not observable",
            "finding": "No matching framework control was found for this requirement in the current framework set.",
            "recommendation": "Review manually, or expand the framework coverage to include a relevant control.",
            "plain_language": "We could not find a rule in the framework that this policy statement "
                              "corresponds to, so we cannot judge it automatically. A person should "
                              "check whether it matters and which rule, if any, applies.",
            "clarifying_questions": [
                "Which framework control, if any, should this requirement be checked against?"
            ],
            "citation": {
                "chunk_id": mapped_requirement["chunk_id"],
                "source": mapped_requirement["source"],
                "locator": mapped_requirement["locator"],
            },
            "framework_control": None,
            "status": "pending"
        }

    # Fetch the EXACT chunk the Mapper matched, by its chunk_id, rather than
    # searching again by control_id text - this avoids any chance of a
    # second search returning a different or no result (per Maryam's note).
    control_chunk_id = mapped_requirement.get("mapped_control_chunk_id")
    control_chunk = get_original_chunk(control_chunk_id) if control_chunk_id else None
    control_text = control_chunk["text"] if control_chunk else "Unknown control text"

    prompt = AUDITOR_PROMPT.format(
        requirement_text=mapped_requirement["requirement_text"],
        control_id=control_id,
        control_text=control_text
    )

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw_text = response.content[0].text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`").replace("json\n", "", 1)

    result = json.loads(raw_text)

    # Build the final gap-report entry in the exact shape the team agreed
    # on in shared/sample_findings.json, so Mahsa's dashboard can render
    # this directly with zero changes.
    return {
        "requirement": mapped_requirement["requirement"],
        "coverage": result["coverage"],
        "finding": result["finding"],
        "recommendation": result["recommendation"],
        "plain_language": result.get("plain_language", ""),
        "clarifying_questions": result.get("clarifying_questions", []),
        "citation": {
            "chunk_id": mapped_requirement["chunk_id"],
            "source": mapped_requirement["source"],
            "locator": mapped_requirement["locator"],
        },
        "framework_control": control_id,
        "status": "pending"   # the human-review step changes this later
    }


if __name__ == "__main__":
    with open("mapped_requirements.json") as f:
        mapped_requirements = json.load(f)

    gap_report = []
    for req in mapped_requirements:
        print(f"\nAuditing: {req['requirement']}")
        entry = audit_requirement(req)
        gap_report.append(entry)
        print(f"  Coverage: {entry['coverage']}")
        print(f"  Finding: {entry['finding']}")
        print(f"  Recommendation: {entry['recommendation']}")

    with open("gap_report.json", "w") as f:
        json.dump(gap_report, f, indent=2)
    print("\nSaved to gap_report.json - this is your real MVP output!")
