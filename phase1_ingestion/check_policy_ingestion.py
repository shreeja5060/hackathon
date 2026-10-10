"""Offline PDF ingestion checks; no Claude calls or search-index writes.

Run from the repository root:
    python -m phase1_ingestion.check_policy_ingestion

Synthetic PDFs are created in a temporary directory and removed afterwards.
These checks verify parsing and citations, not the agents' findings.
"""

import argparse
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pymupdf

from phase1_ingestion import parse_pdfs
from phase1_ingestion.chunk_policies import chunk_pages


ROOT = Path(__file__).resolve().parents[1]
CHUNK_KEYS = {"chunk_id", "text", "source", "type", "doc_kind", "page", "locator"}


def write_pdf(path, page_texts, **save_options):
    with pymupdf.open() as document:
        for text in page_texts:
            height = max(842, 100 + 15 * len(text.splitlines()))
            page = document.new_page(width=595, height=height)
            if text:
                page.insert_text((50, 60), text, fontsize=10)
        document.save(path, **save_options)


class PolicyIngestionChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def pdf_pages(self, texts, name="Unfamiliar_Policy.pdf"):
        path = self.root / name
        write_pdf(path, texts)
        return parse_pdfs.parse_pdf(path)

    def assert_page_fallback(self, pages):
        chunks, sources = chunk_pages(pages)
        self.assertEqual(len(chunks), len(pages))
        for record, chunk in zip(pages, chunks):
            self.assertEqual(set(chunk), CHUNK_KEYS)
            self.assertEqual(chunk["source"], record["source"])
            self.assertEqual(chunk["page"], record["page"])
            self.assertEqual(chunk["locator"], f"Page {record['page']}")
            self.assertEqual(chunk["type"], "internal")
            self.assertEqual(chunk["doc_kind"], "policy")
            # Every extracted character survives, including introductory text.
            self.assertEqual(
                chunk["text"], f"Page {record['page']}\n\n{record['text']}"
            )
            self.assertEqual(sources[record["source"]]["chunking_strategy"], "pages")
        self.assertEqual(len({chunk["chunk_id"] for chunk in chunks}), len(chunks))
        self.assertEqual(chunk_pages(pages)[0], chunks)
        return chunks

    def test_unnumbered_and_roman_headings_keep_text_and_pages(self):
        for heading in ("Access controls", "I. Access controls"):
            with self.subTest(heading=heading):
                pages = self.pdf_pages([
                    "Synthetic remote access policy\n" + heading + "\n"
                    "Staff must use MFA for remote access.",
                    "II. Logging\nAdministrators must record configuration changes.",
                ], name=f"Layout_{heading[0]}.pdf")
                self.assert_page_fallback(pages)

    def test_introductory_requirement_is_not_dropped(self):
        pages = self.pdf_pages([
            "Access is allowed only after manager approval.\n"
            "1 Logging\nAdministrators must record configuration changes."
        ])
        self.assert_page_fallback(pages)

    def test_numbered_requirements_are_not_discarded_as_empty_headings(self):
        pages = self.pdf_pages([
            "1. Users must use MFA.\n2. Administrators must review logs."
        ])
        self.assert_page_fallback(pages)

    def test_parent_requirement_is_not_discarded(self):
        pages = self.pdf_pages([
            "1 Users must report security incidents.\n"
            "1.1 Timing\nReports must be made immediately."
        ])
        self.assert_page_fallback(pages)

    def test_public_template_label_does_not_hide_new_requirements(self):
        pages = self.pdf_pages([
            "Public release template\nAccess requires manager approval.\n"
            "1 Logging\nAdministrators must review audit logs."
        ])
        self.assert_page_fallback(pages)

    def test_numbered_sections_keep_page_boundaries_and_continuation(self):
        pages = self.pdf_pages([
            "1 Access\nStaff must use MFA.",
            "Administrators must also use MFA.\n2 Logging\nChanges must be logged.",
        ])
        chunks, sources = chunk_pages(pages)
        self.assertEqual([chunk["page"] for chunk in chunks], [1, 2, 2])
        self.assertEqual(
            [chunk["locator"] for chunk in chunks],
            ["Section 1 Access", "Section 1 Access", "Section 2 Logging"],
        )
        self.assertNotIn("Administrators must also", chunks[0]["text"])
        self.assertIn("Administrators must also", chunks[1]["text"])
        self.assertEqual(sources[pages[0]["source"]]["chunking_strategy"], "numbered_sections")

    def test_long_page_is_not_truncated_before_embedding(self):
        text = "\n".join(
            f"Rule {i:03}: Employees must record administrator changes in the audit log."
            for i in range(180)
        )
        pages = self.pdf_pages([text])
        chunks = self.assert_page_fallback(pages)
        self.assertIn("Rule 000:", chunks[0]["text"])
        self.assertIn("Rule 179:", chunks[0]["text"])

    def test_document_sources_are_kept_separate(self):
        first = self.pdf_pages(["Staff must use MFA."], "First.pdf")
        second = self.pdf_pages(["Administrators must review logs."], "Second.pdf")
        chunks, sources = chunk_pages(second + first)
        self.assertEqual([chunk["source"] for chunk in chunks], ["First.pdf", "Second.pdf"])
        self.assertEqual(set(sources), {"First.pdf", "Second.pdf"})
        self.assertNotEqual(chunks[0]["chunk_id"], chunks[1]["chunk_id"])

    def test_missing_duplicate_and_empty_pages_are_rejected(self):
        page = {"source": "Invalid.pdf", "page": 1, "text": "Staff must use MFA."}
        for records in ([], [page, page], [{**page, "page": 2}], [{**page, "text": ""}]):
            with self.subTest(records=records):
                with self.assertRaises(ValueError):
                    chunk_pages(records)

    def test_blank_or_scanned_page_does_not_replace_previous_output(self):
        write_pdf(self.root / "Partial.pdf", ["Staff must use MFA.", ""])
        output = self.root / "policy_pages.json"
        output.write_text("previous processed data", encoding="utf-8")
        with patch.multiple(parse_pdfs, POLICY_DIR=self.root, OUTPUT_FILE=output):
            with self.assertRaisesRegex(ValueError, r"Partial.pdf, page 2.*OCR"):
                parse_pdfs.parse_pdfs()
        self.assertEqual(output.read_text(encoding="utf-8"), "previous processed data")

    def test_password_protected_pdf_is_rejected(self):
        path = self.root / "Protected.pdf"
        write_pdf(
            path, ["Staff must use MFA."], encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="synthetic-owner", user_pw="synthetic-reader",
        )
        with self.assertRaisesRegex(ValueError, "Password-protected"):
            parse_pdfs.parse_pdf(path)

    def test_original_templates_keep_28_sections_and_authentication_id(self):
        files = sorted((ROOT / "data" / "policies").glob("*.pdf"))
        expected_counts = {
            "Public_Release_Computer_Acceptable_Use_Policy.pdf": 9,
            "Public_Release_Computer_Security_Policy.pdf": 10,
            "Public_Release_Internet_Email_Acceptable_Use_Policy.pdf": 9,
        }
        pages = [record for path in files if path.name in expected_counts
                 for record in parse_pdfs.parse_pdf(path)]
        chunks, sources = chunk_pages(pages)
        self.assertEqual(dict(Counter(chunk["source"] for chunk in chunks)), expected_counts)
        self.assertTrue(all(info["chunking_strategy"] == "numbered_sections" for info in sources.values()))
        authentication = [chunk for chunk in chunks if chunk["locator"] == "Section 3.5 Authentication"]
        self.assertEqual(len(authentication), 1)
        self.assertEqual(authentication[0]["page"], 2)
        self.assertEqual(
            authentication[0]["chunk_id"],
            "policy-ac75c1f081a14787f00279a86232e184b9efebd22a354adf9d930ef4eb9b1972",
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pdf", type=Path,
        help="Inspect one policy PDF instead of running the fixture checks.",
    )
    args = parser.parse_args()
    if args.pdf is None:
        unittest.main(argv=["check_policy_ingestion"], verbosity=2)
        return

    pages = parse_pdfs.parse_pdf(args.pdf)
    chunks, sources = chunk_pages(pages)
    print(f"Read {args.pdf.name}: {len(pages)} pages, {len(chunks)} chunks")
    print(f"Chunking strategy: {sources[args.pdf.name]['chunking_strategy']}")
    for chunk in chunks:
        print(f"{chunk['locator']} | page {chunk['page']} | {chunk['chunk_id']}")
    print("Inspection only: no processed files, index writes or Claude calls.")


if __name__ == "__main__":
    main()
