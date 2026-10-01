"""Parse our normalized synthetic demo format, not arbitrary AWS API exports."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data/examples/synthetic_environment.json"


def parse_environment(path):
    path = Path(path)
    raw = path.read_bytes()
    data = json.loads(raw)
    if data.get("schema_version") != 1 or data.get("synthetic") is not True:
        raise ValueError("Expected synthetic demo schema version 1.")
    for key in ("environment", "captured_at"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"Missing {key}")
    stamp = datetime.fromisoformat(data["captured_at"].replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("captured_at must include a timezone")
    resources = data.get("resources")
    if not isinstance(resources, list) or not resources:
        raise ValueError("resources must be a nonempty list")
    chunks, seen = [], set()
    for position, resource in enumerate(resources):
        for key in ("resource_id", "resource_type"):
            if not isinstance(resource.get(key), str) or not resource[key].strip():
                raise ValueError(f"Missing resource {key}")
        identity = (resource["resource_type"], resource["resource_id"])
        if identity in seen:
            raise ValueError(f"Duplicate resource: {identity}")
        seen.add(identity)
        settings = resource.get("settings")
        if not isinstance(settings, dict) or not settings:
            raise ValueError("Each resource needs a nonempty settings object")
        locator = f"/resources/{position}"
        lines = [
            f"SYNTHETIC DEMO | {resource['resource_type']} | {resource['resource_id']}",
            f"Environment: {data['environment']}; snapshot: {data['captured_at']}",
            "Reported configuration settings (not a compliance verdict):",
        ]
        for name, value in settings.items():
            rendered = "unknown / not supplied" if value is None else json.dumps(value, sort_keys=True)
            lines.append(f"{name.replace('_', ' ')} [{name}]: {rendered}")
        lines.append("Missing settings are unknown. This snapshot does not establish ongoing operation or full coverage.")
        text = "\n".join(lines)
        digest = hashlib.sha256(json.dumps([path.name, locator, text]).encode()).hexdigest()
        chunks.append({"chunk_id": "evidence-" + digest, "text": text,
                       "source": path.name, "page": None, "type": "evidence",
                       "doc_kind": "system_configuration", "locator": locator})
    sources = {path.name: {"path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else path.name,
                           "sha256": hashlib.sha256(raw).hexdigest(),
                           "synthetic": True, "captured_at": data["captured_at"],
                           "environment": data["environment"]}}
    return chunks, sources


def main():
    chunks, sources = parse_environment(DEFAULT_INPUT)
    output = ROOT / "data/processed"
    output.mkdir(parents=True, exist_ok=True)
    # One bundle keeps source provenance and chunks together.
    target = output / "environment_bundle.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"chunks": chunks, "sources": sources}, indent=2) + "\n")
    temporary.replace(target)
    print(f"Prepared {len(chunks)} synthetic environment chunks: {target}")


if __name__ == "__main__":
    main()
