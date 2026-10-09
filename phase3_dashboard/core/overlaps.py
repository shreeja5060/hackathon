"""Where an organization's policies overlap, disagree, or leave a gap that another policy fills.

A company rarely has one policy. An internet and email policy and a computer security
policy can both talk about passwords; a remote work policy can set its own screen-lock
time. The ledger keeps every analyzed policy, so each finding can be compared with what
the organization's other policies say about the same NIST control:

* conflict           both set a value for the same thing and the values differ
                     ("15 minutes" here, "5 minutes" there)
* covered elsewhere  this finding is a gap (Partial or Missing) and another policy
                     covers the control fully, so the gap may already be closed
* overlap            both address the control; not wrong, but worth keeping consistent

Findings are compared on their NIST control (the Mapper's job is exactly to put
different wordings on a common reference). Values are compared deterministically, with
units normalized (1 hour = 60 minutes), so the result is explainable and testable.
Which policy wins in a conflict is the organization's call; the dashboard shows both
and the reviewer decides. Rejected findings are left out: a reviewer said they're wrong.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable

GAPS = ("Partial", "Missing")
COVERAGE_RANK = {"Full": 3, "Partial": 2, "Missing": 1, "Not observable": 0}

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty-five": 45, "sixty": 60, "ninety": 90,
}
# unit word -> (dimension, factor to the dimension's base unit)
_UNITS = {
    "second": ("time", 1 / 60), "minute": ("time", 1), "min": ("time", 1), "hour": ("time", 60), "hr": ("time", 60),
    "day": ("time", 1440), "business day": ("time", 1440), "working day": ("time", 1440),
    "week": ("time", 10080), "month": ("time", 43200), "year": ("time", 525600),
    "character": ("length", 1), "char": ("length", 1), "digit": ("length", 1),
    "attempt": ("attempts", 1), "try": ("attempts", 1), "tries": ("attempts", 1),
}
_FREQUENCY = {"hourly": 60, "daily": 1440, "nightly": 1440, "weekly": 10080, "monthly": 43200,
              "quarterly": 129600, "annually": 525600, "annual": 525600, "yearly": 525600}
_NUMBER = r"(\d+(?:\.\d+)?|" + "|".join(sorted(map(re.escape, _NUMBER_WORDS), key=len, reverse=True)) + r")"
_UNIT = r"(business days?|working days?|seconds?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|" \
        r"characters?|chars?|digits?|attempts?|tries|try)"
# Up to two describing words may sit between the number and the unit ("10 failed sign-in attempts"),
# but not another number ("a 15 minute period" is 15 minutes, not 1).
_FILLER = r"(?:(?!\d)(?!(?:" + "|".join(_NUMBER_WORDS) + r")\b)[A-Za-z][\w-]*\s+){0,2}"
_QUANTITY = re.compile(rf"\b{_NUMBER}[\s-]+{_FILLER}{_UNIT}\b", re.I)
_ONE_UNIT = re.compile(r"\b(?:a|an|one)\s+(minute|hour|day|week|month|year)\b", re.I)  # "within an hour"
_FREQ = re.compile(r"\b(" + "|".join(_FREQUENCY) + r")\b", re.I)
_EVERY = re.compile(rf"\bevery\s+(?:{_NUMBER}\s+)?(minute|hour|day|week|month|year)s?\b", re.I)


def _number(token: str) -> float:
    token = token.lower()
    return float(_NUMBER_WORDS[token]) if token in _NUMBER_WORDS else float(token)


def _unit(word: str) -> tuple[str, float]:
    word = word.lower()
    if word in _UNITS:
        return _UNITS[word]
    for suffix in ("s", "es"):
        if word.endswith(suffix) and word[: -len(suffix)] in _UNITS:
            return _UNITS[word[: -len(suffix)]]
    return _UNITS[word.rstrip("s")]


def quantities(text: str | None) -> dict[str, dict[float, str]]:
    """The checkable values a requirement states, by dimension, each with the words that stated it:
    {"time": {15.0: "15 minutes"}} for "lock after 15 minutes" (time is in minutes, so 1 hour == 60)."""
    found: dict[str, dict[float, str]] = defaultdict(dict)
    if not text:
        return {}
    for match in _QUANTITY.finditer(text):
        number, unit = match.group(1), match.group(2)
        dimension, factor = _unit(unit)
        found[dimension].setdefault(round(_number(number) * factor, 4), f"{number} {unit}".lower())
    for match in _ONE_UNIT.finditer(text):
        found["time"].setdefault(float(_UNITS[match.group(1).lower()][1]), " ".join(match.group(0).lower().split()))
    for match in _FREQ.finditer(text):
        found["time"].setdefault(float(_FREQUENCY[match.group(1).lower()]), match.group(1).lower())
    for match in _EVERY.finditer(text):
        number, unit = match.group(1), match.group(2)
        value = round((_number(number) if number else 1) * _UNITS[unit.lower()][1], 4)
        found["time"].setdefault(value, " ".join(match.group(0).lower().split()))
    return dict(found)


def value_conflict(text_a: str | None, text_b: str | None) -> tuple[str, list[str], list[str]] | None:
    """(dimension, values only in a, values only in b) when both state values of one kind and each has
    one the other lacks: "within 30 days and annually" vs "within 14 days and annually" differ on 30/14 days.
    One text simply saying more ("15 minutes" vs "15 minutes, reviewed daily") is not a conflict."""
    a, b = quantities(text_a), quantities(text_b)
    for dimension in ("time", "length", "attempts"):
        values_a, values_b = a.get(dimension, {}), b.get(dimension, {})
        only_a = [values_a[v] for v in sorted(set(values_a) - set(values_b))]
        only_b = [values_b[v] for v in sorted(set(values_b) - set(values_a))]
        if values_a and values_b and only_a and only_b:
            return dimension, only_a, only_b
    return None


def same_control(a: str | None, b: str | None) -> bool:
    return bool(a) and a == b


# ------------------------------------------------------------------ relating one finding to the rest

def relate(focus: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """How another policy's statement relates to the focus finding. Both share a control."""
    conflict = value_conflict(focus.get("requirement_text"), other.get("requirement_text"))
    if conflict:
        dimension, mine, theirs = conflict
        return {"kind": "conflict", "detail": f"{' / '.join(mine)} here, {' / '.join(theirs)} there"}
    if focus.get("coverage") in GAPS and other.get("coverage") == "Full":
        return {"kind": "covered_elsewhere", "detail": f"rated Full there, {focus.get('coverage')} here"}
    return {"kind": "overlap", "detail": "both address this control"}


KIND_ORDER = {"conflict": 0, "covered_elsewhere": 1, "overlap": 2}
KIND_LABELS = {"conflict": "Values differ", "covered_elsewhere": "Covered elsewhere", "overlap": "Also addressed"}


def related(focus: dict[str, Any], statements: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Statements from other policies (and other sections of this one) on the focus finding's control."""
    out = []
    for other in statements:
        if not same_control(focus.get("control"), other.get("control")):
            continue
        if other.get("run_id") == focus.get("run_id") and other.get("finding_id") == focus.get("finding_id"):
            continue
        if other.get("run_id") == focus.get("run_id") and other.get("locator") == focus.get("locator"):
            continue  # the same section restating itself isn't news
        out.append({**other, **relate(focus, other), "same_policy": other.get("policy") == focus.get("policy")})
    out.sort(key=lambda item: (KIND_ORDER[item["kind"]], item["same_policy"], item.get("policy") or ""))
    return out


# ------------------------------------------------------------------ the organization-wide picture

def coverage_map(statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per control: the best coverage each policy gives it, and whether policies agree."""
    by_control: dict[str, list[dict]] = defaultdict(list)
    for statement in statements:
        by_control[statement["control"]].append(statement)
    rows = []
    for control, items in sorted(by_control.items()):
        policies: dict[str, str] = {}
        for item in items:
            current = policies.get(item["policy"])
            if current is None or COVERAGE_RANK.get(item["coverage"], -1) > COVERAGE_RANK.get(current, -1):
                policies[item["policy"]] = item["coverage"]
        conflicts = []
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if a["policy"] == b["policy"] and a.get("locator") == b.get("locator"):
                    continue
                found = value_conflict(a.get("requirement_text"), b.get("requirement_text"))
                if found:
                    conflicts.append({"a": a, "b": b, "values": found})
        best = max((COVERAGE_RANK.get(c, -1) for c in policies.values()), default=-1)
        rows.append({
            "control": control,
            "policies": policies,
            "statements": items,
            "conflicts": conflicts,
            "overlap": len(policies) > 1,
            "best": next((name for name, rank in COVERAGE_RANK.items() if rank == best), None),
        })
    return rows


def summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "controls": len(rows),
        "overlaps": sum(1 for row in rows if row["overlap"]),
        "conflicts": sum(1 for row in rows if row["conflicts"]),
        "gaps_everywhere": sum(1 for row in rows if row["best"] in GAPS),
    }


def unaddressed(rows: list[dict[str, Any]], baseline: Iterable[str]) -> list[str]:
    """Baseline controls that no stored policy addresses at all (not even an enhancement of the control)."""
    addressed = {row["control"].split("(", 1)[0] for row in rows}
    return [control for control in baseline if control.split("(", 1)[0] not in addressed]
