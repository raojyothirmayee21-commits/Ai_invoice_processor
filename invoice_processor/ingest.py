"""Turn a file on disk into something the extractor can read.

Digital PDFs -> text (cheap, fast, and we can mask PII in it).
Scanned PDFs / photos -> page images sent to Claude's vision input.
"""

from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg"}
# A digital PDF with less text than this is almost certainly a scan.
MIN_TEXT_CHARS = 40
MAX_PAGES = 5  # invoices are short; cap cost on a 200-page accidental upload


@dataclass
class Document:
    source_path: Path
    file_hash: str
    text: str = ""
    images: list[tuple[str, str]] = field(default_factory=list)  # (media_type, base64)
    page_count: int = 0

    @property
    def is_scanned(self) -> bool:
        return not self.text and bool(self.images)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_document(path: str | Path) -> Document:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported file type: {suffix}")

    doc = Document(source_path=path, file_hash=file_sha256(path))

    if suffix in {".png", ".jpg", ".jpeg"}:
        media = "image/png" if suffix == ".png" else "image/jpeg"
        doc.images.append((media, base64.b64encode(path.read_bytes()).decode()))
        doc.page_count = 1
        return doc

    with pdfplumber.open(path) as pdf:
        pages = pdf.pages[:MAX_PAGES]
        doc.page_count = len(pdf.pages)
        text = "\n\n".join((p.extract_text() or "") for p in pages).strip()
        if len(text) >= MIN_TEXT_CHARS:
            doc.text = text
        else:
            for page in pages:
                img = page.to_image(resolution=150).original
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                doc.images.append(("image/png", base64.b64encode(buf.getvalue()).decode()))
    return doc


def list_inbox(inbox_dir: str | Path) -> list[Path]:
    inbox = Path(inbox_dir)
    if not inbox.exists():
        return []
    return sorted(p for p in inbox.rglob("*") if p.suffix.lower() in SUPPORTED_SUFFIXES)
