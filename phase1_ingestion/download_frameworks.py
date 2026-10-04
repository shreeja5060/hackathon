"""Download the two official NIST catalogs and record their provenance.

Uses a fixed commit from NIST's OSCAL Content v1.5.0 release so teammates
receive identical files. Run with Python's standard library; no API key needed.
This downloads and validates the catalogs. Chunking and indexing come later.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRAMEWORK_DIR = PROJECT_ROOT / "data" / "frameworks"
NIST_COMMIT = "78650f02ad9321bb7b817846f8fbd4f2bcd620de"
BASE_URL = (
    f"https://raw.githubusercontent.com/usnistgov/oscal-content/{NIST_COMMIT}/nist.gov"
)
CATALOGS = (
    {
        "filename": "NIST_SP-800-53_rev5_catalog.json",
        "path": "SP800-53/rev5/json/NIST_SP-800-53_rev5_catalog.json",
        "title": "NIST SP 800-53 Revision 5, Release 5.2.0",
        "version": "5.2.0",
        "publication_date": "2025-08-27",
        "publication_url": "https://csrc.nist.gov/News/2025/nist-releases-revision-to-sp-800-53-controls",
        "catalog_version": "5.2.0",
        "sha256": "01f37cf90ea99d92242c936cbfbdebcc338eef1f71454e2acac36cc56e9bc062",
    },
    {
        "filename": "NIST_CSF_v2.0_catalog.json",
        "path": "CSF/v2.0/json/NIST_CSF_v2.0_catalog.json",
        "title": "NIST Cybersecurity Framework 2.0",
        "version": "2.0",
        "publication_date": "2024-02-26",
        "publication_url": "https://www.nist.gov/publications/nist-cybersecurity-framework-csf-20",
        "catalog_version": "1.2.0",
        "sha256": "4836943f21d393f2821df85ada4e6f3c0617243b4ddf44263fe68205400210b4",
    },
)


def validate_catalog(raw, spec):
    digest = hashlib.sha256(raw).hexdigest()
    if digest != spec["sha256"]:
        raise ValueError(
            f"{spec['filename']}: file does not match the pinned NIST release. "
            "Review the file before using it; existing files were not overwritten."
        )
    catalog = json.loads(raw)["catalog"]
    metadata = catalog["metadata"]
    if metadata["version"] != spec["catalog_version"]:
        raise ValueError(f"Unexpected catalog version in {spec['filename']}")
    if not catalog.get("groups") and not catalog.get("controls"):
        raise ValueError(f"No catalog entries found in {spec['filename']}")
    return metadata


def main():
    FRAMEWORK_DIR.mkdir(parents=True, exist_ok=True)
    manifest_file = FRAMEWORK_DIR / "framework_sources.json"
    previous = (
        json.loads(manifest_file.read_text(encoding="utf-8"))
        if manifest_file.exists()
        else {}
    )
    sources = dict(previous)
    downloads = []

    for spec in CATALOGS:
        destination = FRAMEWORK_DIR / spec["filename"]
        url = f"{BASE_URL}/{spec['path']}"
        if destination.exists():
            print(f"Checking existing {spec['filename']}...", flush=True)
            raw = destination.read_bytes()
            fetched_at = None
        else:
            print(f"Downloading {spec['filename']}...", flush=True)
            request = Request(url, headers={"User-Agent": "AI-Compliance-Copilot/0.1"})
            try:
                with urlopen(request, timeout=30) as response:
                    raw = response.read(20 * 1024 * 1024 + 1)
            except (URLError, TimeoutError) as error:
                raise RuntimeError(
                    f"Could not download {spec['filename']}: {error}. "
                    "Check your connection and try again."
                ) from error
            if len(raw) > 20 * 1024 * 1024:
                raise ValueError(f"Unexpected download size for {spec['filename']}")
            fetched_at = datetime.now(timezone.utc).isoformat()

        metadata = validate_catalog(raw, spec)
        old_record = previous.get(spec["filename"], {})
        if fetched_at is None and old_record.get("sha256") == spec["sha256"]:
            fetched_at = old_record.get("downloaded_at")

        sources[spec["filename"]] = {
            "title": spec["title"],
            "version": spec["version"],
            "publication_date": spec["publication_date"],
            "publication_url": spec["publication_url"],
            "source_url": url,
            "catalog_version": metadata["version"],
            "oscal_version": metadata["oscal-version"],
            "catalog_last_modified": metadata.get("last-modified"),
            "sha256": spec["sha256"],
            "downloaded_at": fetched_at,
        }
        if not destination.exists():
            downloads.append((destination, raw))

    # Validate both catalogs before saving any new source files.
    for destination, raw in downloads:
        temp = destination.with_suffix(".json.tmp")
        temp.write_bytes(raw)
        temp.replace(destination)
    temp_manifest = manifest_file.with_suffix(".json.tmp")
    temp_manifest.write_text(
        json.dumps(sources, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temp_manifest.replace(manifest_file)

    for spec in CATALOGS:
        print(f"Ready: {spec['title']}")
    print(f"Saved source records to {manifest_file}")
    print("Both catalogs are ready for parsing and chunking.")


if __name__ == "__main__":
    main()
