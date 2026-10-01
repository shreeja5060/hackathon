"""Convert the pinned NIST catalogs into complete, cited framework records.

Run download_frameworks.py first. Python standard library only; no LLM calls.
SP 800-53: active controls/enhancements, statements and explanatory discussion.
CSF: active subcategory outcomes, with context and labelled implementation examples.
The original catalogs retain withdrawn entries and SP 800-53A assessment material.

Each output chunk preserves a whole control or outcome. Before embedding, the
indexer must check the model's token limit and split long records with citations
preserved. Never silently truncate a control to fit an embedding model.
"""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

if __package__:
    from .download_frameworks import CATALOGS, validate_catalog
else:
    from download_frameworks import CATALOGS, validate_catalog


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRAMEWORK_DIR = PROJECT_ROOT / "data" / "frameworks"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
INSERT = re.compile(r"\{\{\s*insert:\s*param,\s*([^\s{}]+)\s*\}\}")


def is_withdrawn(node):
    return any(
        prop.get("name") == "status" and prop.get("value") == "withdrawn"
        for prop in node.get("props", [])
    )


def label(node):
    # Prefer AC-2 / AC-2(1), rather than the alternate zero-padded labels.
    for prop in node.get("props", []):
        if prop.get("name") == "label" and not prop.get("class"):
            return prop["value"]
    return node.get("id", "")


def walk_controls(node, ancestors=()):
    for control in node.get("controls", []):
        yield control, ancestors
        yield from walk_controls(control, ancestors + (control,))
    for group in node.get("groups", []):
        yield from walk_controls(group, ancestors + (group,))


def parameter_map(nodes):
    parameters = {}
    for node in nodes:
        for parameter in node.get("params", []):
            identifiers = [parameter["id"]]
            for prop in parameter.get("props", []):
                if prop.get("name") == "alt-identifier":
                    identifiers.append(prop["value"])
            for identifier in identifiers:
                if identifier in parameters and parameters[identifier] != parameter:
                    raise ValueError(f"Ambiguous OSCAL parameter: {identifier}")
                parameters[identifier] = parameter
    return parameters


def render_prose(prose, parameters, stack=()):
    """Expand OSCAL placeholders into labels/options, never guessed values."""
    def replace(match):
        identifier = match.group(1)
        if identifier not in parameters:
            raise ValueError(f"Unknown OSCAL parameter: {identifier}")
        parameter = parameters[identifier]
        canonical_id = parameter["id"]
        if canonical_id in stack:
            raise ValueError(f"Cyclic OSCAL parameter: {canonical_id}")
        next_stack = stack + (canonical_id,)

        if parameter.get("values"):
            return "; ".join(
                render_prose(value, parameters, next_stack)
                for value in parameter["values"]
            )
        if "select" in parameter:
            selection = parameter["select"]
            cardinality = selection.get("how-many", "one")
            if cardinality not in {"one", "one-or-more"}:
                raise ValueError(f"Unknown selection cardinality: {cardinality}")
            choices = [
                render_prose(choice, parameters, next_stack).strip()
                for choice in selection.get("choice", [])
            ]
            if not choices:
                raise ValueError(f"No choices for parameter {canonical_id}")
            count = "one or more" if cardinality == "one-or-more" else "one"
            return f"[Selection ({count}): {'; '.join(choices)}]"
        if parameter.get("label"):
            description = render_prose(parameter["label"], parameters, next_stack)
            if not description.lower().startswith("organization-defined"):
                description = f"organization-defined {description}"
            return f"[Assignment: {description}]"
        raise ValueError(f"Parameter has no usable label/options: {canonical_id}")

    result = INSERT.sub(replace, prose)
    if "{{" in result or "}}" in result:
        raise ValueError(f"Unsupported OSCAL insertion: {result[:160]}")
    return result.strip()


def render_part(part, parameters, depth=0):
    lines = []
    if part.get("prose"):
        prefix = label(part) + " " if part.get("name") == "item" else ""
        prose = render_prose(part["prose"], parameters)
        lines.append("  " * depth + prefix + prose)
    for child in part.get("parts", []):
        lines.extend(render_part(child, parameters, depth + 1))
    return lines


def render_named_parts(control, name, parameters):
    return "\n".join(
        line
        for part in control.get("parts", [])
        if part.get("name") == name
        for line in render_part(part, parameters)
    ).strip()


def make_chunk(source, source_sha256, locator, text, doc_kind):
    identity = json.dumps([source, source_sha256, locator, text], ensure_ascii=False)
    return {
        "chunk_id": "framework-" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "text": text,
        "source": source,
        "type": "framework",
        "doc_kind": doc_kind,
        "page": None,
        "locator": locator,
    }


def parse_catalog(catalog, spec):
    chunks = []
    counts = Counter()
    withdrawn_ids = []
    sp80053 = spec["filename"] == "NIST_SP-800-53_rev5_catalog.json"
    control_records = list(walk_controls(catalog))
    # IDs are catalog-wide: SC-42(2), for example, references a parameter
    # declared in sibling enhancement SC-42(1).
    parameter_nodes = [catalog]
    for control, ancestors in control_records:
        parameter_nodes.extend((*ancestors, control))
    parameters = parameter_map(parameter_nodes)
    for control, ancestors in control_records:
        if any(is_withdrawn(node) for node in (*ancestors, control)):
            withdrawn_ids.append(control["id"])
            continue

        control_class = control.get("class")
        if not sp80053 and control_class == "category":
            counts["categories_used_as_context"] += 1
            continue
        expected = {"SP800-53", "SP800-53-enhancement"} if sp80053 else {"subcategory"}
        if control_class not in expected:
            raise ValueError(f"Unexpected control class: {control_class}")

        statement = render_named_parts(control, "statement", parameters)
        if not statement:
            raise ValueError(f"Missing statement for active entry {control['id']}")

        if sp80053:
            locator = label(control)
            content = [
                f"NIST SP 800-53 Release {spec['version']} | {locator}: {control['title']}",
                "Context: " + " > ".join(
                    f"{label(node)}: {node['title']}" for node in ancestors
                ),
                "Control statement:\n" + statement,
            ]
            guidance = render_named_parts(control, "guidance", parameters)
            if guidance:
                content.append("Discussion (explanatory context):\n" + guidance)
            doc_kind = "control_catalog"
            counts["base_controls" if control_class == "SP800-53" else "enhancements"] += 1
        else:
            locator = control["id"]
            content = [f"NIST CSF {spec['version']} | {locator}"]
            for ancestor in ancestors:
                context = f"{ancestor['id']}: {ancestor['title']}"
                description = render_named_parts(ancestor, "statement", parameters)
                if description:
                    context += " — " + description
                content.append("Context: " + context)
            content.append("CSF outcome:\n" + statement)
            examples = [
                f"- {' '.join(render_part(part, parameters))}"
                for part in control.get("parts", [])
                if part.get("name") == "example"
            ]
            if examples:
                content.append(
                    "Implementation examples (illustrative ways to achieve the outcome; "
                    "not additional mandatory requirements):\n" + "\n".join(examples)
                )
            doc_kind = "cybersecurity_framework"
            counts["outcomes"] += 1

        chunks.append(make_chunk(
            spec["filename"], spec["sha256"], locator, "\n\n".join(content), doc_kind
        ))

    report = {
        "version": spec["version"],
        "chunks": len(chunks),
        **dict(counts),
        "withdrawn_entries_skipped": len(withdrawn_ids),
        "withdrawn_ids": withdrawn_ids,
    }
    return chunks, report


def main():
    manifest_path = FRAMEWORK_DIR / "framework_sources.json"
    if not manifest_path.exists():
        raise FileNotFoundError("Run download_frameworks.py first.")
    sources = json.loads(manifest_path.read_text(encoding="utf-8"))
    chunks = []
    reports = {}

    for spec in CATALOGS:
        source_record = sources.get(spec["filename"], {})
        if (source_record.get("sha256") != spec["sha256"]
                or source_record.get("version") != spec["version"]):
            raise ValueError(f"Source record mismatch for {spec['filename']}")
        raw = (FRAMEWORK_DIR / spec["filename"]).read_bytes()
        validate_catalog(raw, spec)
        catalog_chunks, report = parse_catalog(json.loads(raw)["catalog"], spec)
        chunks.extend(catalog_chunks)
        reports[spec["filename"]] = report
        print(
            f"{spec['title']}: {len(catalog_chunks)} chunks; "
            f"{report['withdrawn_entries_skipped']} withdrawn entries skipped"
        )

    if len({chunk["chunk_id"] for chunk in chunks}) != len(chunks):
        raise ValueError("Duplicate framework chunk IDs")
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    for filename, data in (
        ("framework_chunks.json", chunks),
        ("framework_parse_report.json", reports),
    ):
        path = PROCESSED_DIR / filename
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(path)
        print(f"Saved {path}")
    print(f"Total: {len(chunks)} framework chunks from both catalogs")


if __name__ == "__main__":
    main()
