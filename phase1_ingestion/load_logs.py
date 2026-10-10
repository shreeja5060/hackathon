"""Load a CloudTrail Records JSON file as cited security-log evidence.

This module parses supplied records. It does not connect to AWS, infer resource
links, or determine whether an event complies with an organizational control.
"""

import argparse
import gzip
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = ROOT / "data/processed/logs_bundle.json"
UNKNOWN = "unknown / not supplied"
SCOPE = (
    "Scope: supplied events only; collection coverage is unknown. These records "
    "do not establish ongoing enforcement or a compliance verdict."
)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def event_time(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Each event needs eventTime")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("eventTime must include a timezone")
    return stamp.astimezone(timezone.utc)


def reported_outcome(record):
    response = record.get("responseElements") or {}
    login = response.get("ConsoleLogin") if (
        record["eventSource"] == "signin.amazonaws.com"
        and record["eventName"] == "ConsoleLogin"
    ) else None
    error = bool(record.get("errorCode") or record.get("errorMessage"))
    if login == "Success" and error:
        return "unknown / conflicting success and error fields"
    if login == "Failure" or error:
        return "failure"
    if login == "Success":
        return "success"
    return UNKNOWN  # Absence of an error is not treated as an explicit result.


def reported_mfa(record):
    if record["eventSource"] != "signin.amazonaws.com" or record["eventName"] != "ConsoleLogin":
        return UNKNOWN
    value = (record.get("additionalEventData") or {}).get("MFAUsed")
    return {"Yes": "true", "No": "false"}.get(value, UNKNOWN) if isinstance(value, str) else UNKNOWN


def render(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return UNKNOWN
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def parse_logs(path, *, environment, synthetic=False):
    path = Path(path)
    if not isinstance(environment, str) or not environment.strip():
        raise ValueError("An explicit environment name is required")
    if not isinstance(synthetic, bool):
        raise ValueError("synthetic must be true or false")
    raw = path.read_bytes()
    payload = gzip.decompress(raw) if path.suffix.lower() == ".gz" else raw
    data = json.loads(payload, object_pairs_hook=unique_object, parse_constant=reject_constant)
    if not isinstance(data, dict) or not isinstance(data.get("Records"), list) or not data["Records"]:
        raise ValueError("Expected a nonempty CloudTrail Records array")

    chunks, stamps, seen = [], [], set()
    for position, record in enumerate(data["Records"]):
        locator = f"/Records/{position}"
        if not isinstance(record, dict):
            raise ValueError(f"{locator} must be an object")
        for field in ("eventID", "eventTime", "eventName", "eventSource", "eventVersion"):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise ValueError(f"{locator}: missing or invalid {field}")
        if re.fullmatch(r"1\.\d+", record["eventVersion"]) is None:
            raise ValueError(f"{locator}: unsupported CloudTrail eventVersion")
        if record["eventID"] in seen:
            raise ValueError(f"Duplicate eventID: {record['eventID']}")
        seen.add(record["eventID"])
        stamps.append(event_time(record["eventTime"]))
        for field in ("userIdentity", "requestParameters", "responseElements", "additionalEventData"):
            if record.get(field) is not None and not isinstance(record[field], dict):
                raise ValueError(f"{locator}: {field} must be an object or null")
        if record.get("resources") is not None and (
            not isinstance(record["resources"], list)
            or any(not isinstance(resource, dict) for resource in record["resources"])
        ):
            raise ValueError(f"{locator}: resources must be an array of objects or null")
        for field in ("errorCode", "errorMessage"):
            if record.get(field) is not None and not isinstance(record[field], str):
                raise ValueError(f"{locator}: {field} must be a string or null")

        identity = record.get("userIdentity") or {}
        label = "SYNTHETIC DEMO SECURITY LOG" if synthetic else "SECURITY LOG EVIDENCE"
        text = "\n".join([
            f"{label} | {record['eventName']}",
            f"Environment (provided by caller): {environment}",
            f"Event ID: {record['eventID']}",
            f"Recorded event time: {record['eventTime']}",
            f"Recorded service: {record['eventSource']}",
            f"Recorded actor type: {render(identity.get('type'))}",
            f"Recorded userName: {render(identity.get('userName'))}",
            f"Recorded actor ARN: {render(identity.get('arn'))}",
            f"Recorded resource targets: {render(record.get('resources'))}",
            f"Reported result: {reported_outcome(record)}",
            f"Reported MFAUsed for this event: {reported_mfa(record)}",
            SCOPE,
            "Complete supplied CloudTrail record:",
            json.dumps(record, sort_keys=True, ensure_ascii=False, indent=2),
        ])
        identity_text = json.dumps([path.name, locator, text], ensure_ascii=False)
        chunk_id = "log-" + hashlib.sha256(identity_text.encode("utf-8")).hexdigest()
        chunks.append({
            "chunk_id": chunk_id, "text": text, "source": path.name,
            "page": None, "type": "evidence", "doc_kind": "security_log", "locator": locator,
        })

    source = {
        "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "format": "aws_cloudtrail_v1", "synthetic": synthetic,
        "environment": environment, "environment_basis": "caller-provided",
        "event_count": len(chunks),
        "observed_first_event": min(stamps).isoformat(),
        "observed_last_event": max(stamps).isoformat(),
        "collection_coverage": "unknown",
    }
    return chunks, {path.name: source}


def write_bundle(chunks, sources, target=None):
    target = BUNDLE_PATH if target is None else Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"chunks": chunks, "sources": sources}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="CloudTrail JSON or JSON.gz file")
    parser.add_argument("--environment", required=True, help="Explicit label for these supplied records")
    parser.add_argument("--synthetic", action="store_true", help="Label every record as synthetic demo evidence")
    args = parser.parse_args()
    chunks, sources = parse_logs(args.input, environment=args.environment, synthetic=args.synthetic)
    write_bundle(chunks, sources)
    print(f"Prepared {len(chunks)} security-log chunks: {BUNDLE_PATH}")
    print("Next: rebuild the index. No AWS or Claude calls were made.")


if __name__ == "__main__":
    main()
