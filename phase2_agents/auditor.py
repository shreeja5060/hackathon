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
import json
from dotenv import load_dotenv
from anthropic import Anthropic

import sys
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))
from fake_search import search

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

Return ONLY JSON in this exact shape:
{{"coverage": "Full|Partial|Missing|Not observable", "finding": "...", "recommendation": "..."}}

If coverage is "Full", set recommendation to "None".
"""


def audit_requirement(mapped_requirement: dict) -> dict:
    """
    Takes one mapped requirement (has requirement_text + mapped_control) and
    returns the final gap-report entry: coverage, finding, recommendation.
    """
    control_id = mapped_requirement["mapped_control"]

    # Look up the actual NIST control text so Claude can compare against it,
    # not just the control ID
    framework_chunks = search(control_id, type="framework")
    control_text = framework_chunks[0]["text"] if framework_chunks else "Unknown control text"

    prompt = AUDITOR_PROMPT.format(
        requirement_text=mapped_requirement["requirement_text"],
        control_id=control_id,
        control_text=control_text
    )

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=300,
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
