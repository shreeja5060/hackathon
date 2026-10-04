"""Create section chunks from the three numbered public policy templates.

Run after parse_pdfs.py. Uses only the Python standard library.
Each chunk stays on one PDF page; continuing sections keep their locator.
The type/doc_kind split follows the approved Slack proposal. The policy role
defaults to 'internal' and can be changed with --policy-type if the team
chooses a different label. Search scores are computed later, during retrieval.
"""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
HEADING = re.compile(r"^(\d+(?:\.\d+)*)(?:\.)?\s+([A-Z].*)$")
FOOTER = (
    "Public template | Organization-specific and security-sensitive details removed"
)


def clean_line(line):
    line = re.sub(r"\s+", " ", line).strip()
    if line in {"PUBLIC RELEASE", FOOTER}:
        return None
    return line.replace("\uf0b7", "•")


def join_paragraphs(lines):
    """Join PDF line wraps, retaining separate paragraphs and bullet items."""
    paragraphs = []
    current = ""
    for line in lines:
        if not line or line.startswith("•"):
            if current:
                paragraphs.append(current)
            current = line
        elif current:
            separator = "" if current.endswith("-") else " "
            current += separator + line
        else:
            current = line
    if current:
        paragraphs.append(current)
    return "\n\n".join(paragraphs)


def make_chunk(source, page, heading, body, policy_type):
    body_text = join_paragraphs(body)
    if not body_text:
        return None  # A parent heading alone is not a separate evidence chunk.

    locator = f"Section {heading}"
    text = f"{heading}\n\n{body_text}"
    identity = json.dumps([source, page, locator, text], ensure_ascii=False)
    chunk_id = "policy-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return {
        "chunk_id": chunk_id,
        "text": text,
        "source": source,
        "type": policy_type,
        "doc_kind": "policy",
        "page": page,
        "locator": locator,
    }


def chunk_pages(pages, policy_type="internal"):
    if not pages:
        raise ValueError("The input contains no pages. Run parse_pdfs.py first.")
    by_source = defaultdict(list)
    for record in pages:
        by_source[record["source"]].append(record)

    chunks = []
    sources = {}
    for source, source_pages in sorted(by_source.items()):
        source_pages.sort(key=lambda record: record["page"])
        numbers = [record["page"] for record in source_pages]
        if numbers != list(range(1, len(source_pages) + 1)):
            raise ValueError(f"Missing or duplicate page numbers in {source}")

        heading = None
        preamble = []
        for record in source_pages:
            body = []
            for raw_line in record["text"].splitlines():
                line = clean_line(raw_line)
                if line is None:
                    continue
                match = HEADING.fullmatch(line)
                if match:
                    if heading:
                        chunk = make_chunk(
                            source, record["page"], heading, body, policy_type
                        )
                        if chunk:
                            chunks.append(chunk)
                    heading = f"{match.group(1)} {match.group(2)}"
                    body = []
                elif heading is None:
                    preamble.append(line)
                else:
                    body.append(line)

            if heading:
                chunk = make_chunk(
                    source, record["page"], heading, body, policy_type
                )
                if chunk:
                    chunks.append(chunk)
            # Keep heading for any continuation at the start of the next page.

        if heading is None:
            raise ValueError(f"No numbered sections found in {source}; review its layout.")

        context = join_paragraphs(preamble)
        sources[source] = {
            "page_count": len(source_pages),
            "version": None,
            "revision_date": None,
            "is_public_template": (
                True if "Public release template" in context else None
            ),
            "source_context": context,
        }

    if not chunks:
        raise ValueError("No section content found; review the extracted text.")
    return chunks, sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-type", default="internal")
    args = parser.parse_args()
    input_file = PROCESSED_DIR / "policy_pages.json"
    if not input_file.exists():
        raise FileNotFoundError(f"Run parse_pdfs.py first: {input_file} is missing.")

    pages = json.loads(input_file.read_text(encoding="utf-8"))
    chunks, sources = chunk_pages(pages, policy_type=args.policy_type)
    for name, content in (
        ("policy_chunks.json", chunks),
        ("policy_sources.json", sources),
    ):
        output_file = PROCESSED_DIR / name
        output_file.write_text(
            json.dumps(content, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Saved {output_file}")

    for source, count in sorted(Counter(c["source"] for c in chunks).items()):
        print(f"{source}: {count} chunks")
    print(f"Total: {len(chunks)} chunks from {len(sources)} policies")


if __name__ == "__main__":
    main()
