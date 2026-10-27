"""
PDF se text nikalna.

Strategy:
  1. pdfplumber se text + tables nikalo (free, fast) - typed PDFs (purchase orders) ke liye.
  2. Agar text bahut kam mila (scanned/photo PDF) -> needs_vision = True,
     tab extractor PDF ko seedha Claude ko bhej dega (Claude PDF padh sakta hai).
"""
import io
from dataclasses import dataclass

import pdfplumber

MIN_TEXT_CHARS = 40  # isse kam text = shayad scanned PDF hai


@dataclass
class PdfResult:
    text: str
    page_count: int
    needs_vision: bool


def table_to_text(table: list[list]) -> str:
    rows = []
    for row in table:
        cells = [str(c).strip() if c is not None else "" for c in row]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def extract_pdf_text(content: bytes) -> PdfResult:
    parts = []
    page_count = 0
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            page_count = len(pdf.pages)
            for i, page in enumerate(pdf.pages, start=1):
                page_text = page.extract_text() or ""
                tables = page.extract_tables() or []

                parts.append(f"--- Page {i} ---")
                if page_text.strip():
                    parts.append(page_text.strip())
                # Tables alag se bhi do - line items aksar table mein hote hain
                for t in tables:
                    parts.append("[TABLE]\n" + table_to_text(t))
    except Exception as e:
        # Corrupt / password PDF - Claude ko try karne do
        return PdfResult(text=f"(PDF read error: {e})", page_count=0, needs_vision=True)

    text = "\n".join(parts).strip()
    real_chars = len(text.replace("--- Page", "").strip())
    return PdfResult(text=text, page_count=page_count, needs_vision=real_chars < MIN_TEXT_CHARS)
