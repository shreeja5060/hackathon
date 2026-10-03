"""Phase 3: summary metrics, filters and the clickable gap table."""

import pandas as pd
import streamlit as st

from data_source import COVERAGE_LEVELS, STATUSES

STATUS_LABEL = {"pending": "Pending", "approved": "Approved", "rejected": "Rejected"}

# Translucent fills so the colors read well in both light and dark themes.
COVERAGE_COLOR = {
    "Full": "rgba(46, 160, 67, 0.30)",
    "Partial": "rgba(210, 153, 34, 0.35)",
    "Missing": "rgba(248, 81, 73, 0.35)",
    "Not observable": "rgba(139, 148, 158, 0.30)",
}
STATUS_COLOR = {
    "Approved": "rgba(46, 160, 67, 0.30)",
    "Rejected": "rgba(248, 81, 73, 0.35)",
}


def render_metrics(findings: list[dict]) -> None:
    cols = st.columns(6)
    cols[0].metric("Findings", len(findings))
    for col, level in zip(cols[1:4], ["Full", "Partial", "Missing"]):
        col.metric(level, sum(f["coverage"] == level for f in findings))
    reviewed = sum(f["status"] != "pending" for f in findings)
    cols[4].metric("Reviewed", f"{reviewed}/{len(findings)}")
    cols[5].metric("Approved", sum(f["status"] == "approved" for f in findings))


def render_gap_table(findings: list[dict]) -> str | None:
    """Show filters and the table; return the ID of the selected finding, if any."""
    f1, f2, f3 = st.columns(3)
    coverage = f1.multiselect("Coverage", COVERAGE_LEVELS, placeholder="All")
    controls = sorted({f["framework_control"] for f in findings if f["framework_control"]})
    control = f2.multiselect("NIST control", controls, placeholder="All")
    status = f3.multiselect("Review status", STATUSES, placeholder="All",
                            format_func=lambda s: STATUS_LABEL[s])

    shown = [f for f in findings
             if (not coverage or f["coverage"] in coverage)
             and (not control or f["framework_control"] in control)
             and (not status or f["status"] in status)]

    if not shown:
        st.info("No findings match these filters.")
        return None

    table = pd.DataFrame([{
        "ID": f["id"],
        "Requirement": f["requirement"],
        "NIST control": f["framework_control"],
        "Coverage": f["coverage"],
        "Finding": f["finding"],
        "Recommendation": f["recommendation"],
        "Status": STATUS_LABEL[f["status"]],
    } for f in shown])

    styled = (table.style
              .map(lambda v: _fill(COVERAGE_COLOR.get(v)), subset=["Coverage"])
              .map(lambda v: _fill(STATUS_COLOR.get(v)), subset=["Status"]))

    event = st.dataframe(
        styled,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        # Keyed on the visible rows so a stale selection can't point at a
        # different finding after the filters change.
        key="gap_table_" + "-".join(f["id"] for f in shown),
    )
    st.caption("Click a row to see its evidence and approve or reject it.")

    rows = event.selection.rows
    return table.iloc[rows[0]]["ID"] if rows else None


def _fill(color: str | None) -> str:
    return f"background-color: {color}" if color else ""
