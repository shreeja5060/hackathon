"""Phase 3: turn approved findings into a downloadable report."""

import csv
import io
import json
from datetime import date


def approved(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["status"] == "approved"]


def to_markdown(findings: list[dict], policy_name: str) -> str:
    lines = [
        "# Compliance Gap Report",
        "",
        f"**Policy:** {policy_name}  ",
        f"**Date:** {date.today().isoformat()}  ",
        f"**Approved findings:** {len(findings)}",
        "",
        "| ID | Requirement | NIST control | Coverage | Finding | Recommendation | Source |",
        "|---|---|---|---|---|---|---|",
    ]
    for f in findings:
        c = f["citation"]
        source = f"{c['source']}, {c['locator']}".strip(", ")
        cells = [f["id"], f["requirement"], f["framework_control"], f["coverage"],
                 f["finding"], f["recommendation"], source]
        lines.append("| " + " | ".join(str(x).replace("|", "\\|") for x in cells) + " |")

    notes = [f for f in findings if f["reviewer_note"]]
    if notes:
        lines += ["", "## Reviewer notes", ""]
        lines += [f"- **{f['id']} {f['requirement']}:** {f['reviewer_note']}" for f in notes]

    lines += ["", "_Every finding above was reviewed and approved by a person before export._"]
    return "\n".join(lines) + "\n"


def to_csv(findings: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "requirement", "framework_control", "coverage", "finding",
                     "recommendation", "source", "locator", "chunk_id", "reviewer_note"])
    for f in findings:
        c = f["citation"]
        writer.writerow([f["id"], f["requirement"], f["framework_control"], f["coverage"],
                         f["finding"], f["recommendation"], c["source"], c["locator"],
                         c["chunk_id"], f["reviewer_note"]])
    return buf.getvalue()


def to_json(findings: list[dict]) -> str:
    return json.dumps(findings, indent=2)
