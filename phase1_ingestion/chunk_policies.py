"""Create policy chunks with numbered sections or a page-based fallback.

Run after parse_pdfs.py. Uses only the Python standard library.
Each chunk stays on one PDF page; continuing sections keep their locator.
Unfamiliar layouts use page chunks when section splitting would omit text.
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
# Only these exact publication preambles from the three supplied templates
# are metadata-only. Changed/new introductory text must remain in the chunks.
# Values list the templates' empty parent headings, not policy requirements.
TEMPLATE_PREAMBLES = {
    "06e055d209b4076d83aa0851d34f4d36cae93f3dbeed7e3fffe2ce3b961df194": {"3 Policy requirements"},
    "5c9e619ad62f23e7ef30a876269322f5eabf25d3f864cf6a8f5b98c959b20399": {"3 Security requirements"},
    "2197846026c5619334761680cb33a1d241be5245e0506f465721f62faecafd4b": set(),
}


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


def chunk_record(source, page, locator, text, policy_type):
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


def make_chunk(source, page, heading, body, policy_type):
    body_text = join_paragraphs(body)
    if not body_text:
        return None  # A parent heading alone is not a separate evidence chunk.
    return chunk_record(
        source, page, f"Section {heading}", f"{heading}\n\n{body_text}", policy_type
    )


def make_page_chunk(record, policy_type):
    """Keep the extracted text and cite its actual page, without guessing a section."""
    page = record["page"]
    return chunk_record(
        record["source"], page, f"Page {page}",
        f"Page {page}\n\n{record['text'].strip()}", policy_type,
    )


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
        for record in source_pages:
            if not isinstance(record["text"], str) or not record["text"].strip():
                raise ValueError(
                    f"No text found in {source}, page {record['page']}. "
                    "Review the PDF; scanned pages may need OCR."
                )

        source_chunks = []
        headings = []
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
                            source_chunks.append(chunk)
                    heading = f"{match.group(1)} {match.group(2)}"
                    headings.append((match.group(1), heading))
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
                    source_chunks.append(chunk)
            # Keep heading for any continuation at the start of the next page.

        context = join_paragraphs(preamble)
        is_public_template = "Public release template" in context
        context_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()
        known_preamble = context_hash in TEMPLATE_PREAMBLES
        parent_headings = TEMPLATE_PREAMBLES.get(context_hash, set())
        used_headings = {chunk["text"].split("\n", 1)[0] for chunk in source_chunks}
        omitted_heading = any(
            name not in used_headings
            and not (
                name in parent_headings
                and any(other.startswith(number + ".") for other, _ in headings)
            )
            for number, name in headings
        )
        # The supplied public templates have a publication/owner preamble,
        # retained in source_context. For other documents, introductory text
        # may contain requirements and must be included in the chunks.
        use_pages = (
            not source_chunks
            or omitted_heading
            or (bool(context) and not known_preamble)
            or {chunk["page"] for chunk in source_chunks} != set(numbers)
        )
        if use_pages:
            source_chunks = [make_page_chunk(record, policy_type) for record in source_pages]
        chunks.extend(source_chunks)
        sources[source] = {
            "page_count": len(source_pages),
            "version": None,
            "revision_date": None,
            "is_public_template": True if is_public_template else None,
            "source_context": context,
            "chunking_strategy": "pages" if use_pages else "numbered_sections",
        }

    if not chunks:
        raise ValueError("No policy content found; review the extracted text.")
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
