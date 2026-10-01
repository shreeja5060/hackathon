import json
from pathlib import Path

import pymupdf


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = PROJECT_ROOT / "data" / "policies"
OUTPUT_FILE = PROJECT_ROOT / "data" / "processed" / "policy_pages.json"


def parse_pdfs():
    pdf_files = sorted(POLICY_DIR.glob("*.pdf"))

    if not pdf_files:
        raise FileNotFoundError(f"No policy PDFs found in {POLICY_DIR}")

    pages = []

    for pdf_path in pdf_files:
        with pymupdf.open(pdf_path) as document:
            for page_number, page in enumerate(document, start=1):
                text = page.get_text("text", sort=True).strip()

                if not text:
                    raise ValueError(
                        f"No text found in {pdf_path.name}, page {page_number}. "
                        "This page may need OCR."
                    )

                pages.append({
                    "source": pdf_path.name,
                    "page": page_number,
                    "text": text,
                })

            print(f"Read {pdf_path.name}: {len(document)} pages")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(pages, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"\nSaved {len(pages)} pages to {OUTPUT_FILE}")


if __name__ == "__main__":
    parse_pdfs()