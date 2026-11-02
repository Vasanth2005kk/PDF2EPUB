"""PDF validation and pre-conversion analysis (metadata, page count, text availability)."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pymupdf

from app.exceptions import CorruptedPDFError, EmptyPDFError, EncryptedPDFError, InvalidFileError, PDFTooLargeError
from app.utils.validators import has_pdf_signature

logger = logging.getLogger(__name__)

# PyMuPDF is not thread-safe. Inside the web process all PyMuPDF access goes through this lock;
# conversions run in their own processes and are unaffected.
PYMUPDF_LOCK = threading.Lock()

# A page with fewer extractable characters than this is treated as having no text layer.
MIN_TEXT_CHARS_PER_PAGE = 20
PREVIEW_SAMPLE_PAGES = 30

_PLACEHOLDER_TITLES = re.compile(r"^(untitled|document\d*|microsoft word - .*|.*\.(docx?|pdf|indd|tex|odt))$", re.I)


@contextmanager
def open_pdf(path: Path | str, *, max_pages: int | None = None) -> Iterator[pymupdf.Document]:
    """Open and validate a PDF, raising user-facing errors for unusable files.

    Checks the file signature, parser errors, encryption and page count. The document is
    always closed on exit.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            head = handle.read(1024)
    except OSError as exc:
        raise InvalidFileError("The uploaded file could not be read.") from exc

    if size == 0:
        raise EmptyPDFError("The uploaded file is empty.")
    if not has_pdf_signature(head):
        raise InvalidFileError("The file is not a valid PDF (missing PDF signature).")

    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises a variety of types for broken files
        logger.info("PyMuPDF could not open %s: %s", path.name, exc)
        raise CorruptedPDFError("The PDF file is corrupted or unreadable.") from exc

    try:
        if doc.needs_pass:
            raise EncryptedPDFError("This PDF is password-protected. Please remove the password and upload it again.")
        if not doc.is_pdf:
            raise InvalidFileError("The file is not a PDF document.")
        if doc.page_count == 0:
            raise EmptyPDFError("The PDF does not contain any pages.")
        if max_pages is not None and doc.page_count > max_pages:
            raise PDFTooLargeError(f"The PDF has {doc.page_count} pages; the maximum supported is {max_pages} pages.")
        try:
            doc.load_page(0).get_text("text")
        except Exception as exc:
            raise CorruptedPDFError("The PDF file is corrupted or unreadable.") from exc
        yield doc
    finally:
        doc.close()


def parse_pdf_date(value: str | None) -> str:
    """Convert a PDF date string (``D:YYYYMMDDHHmmSS+HH'mm'``) to ISO 8601, or return ``""``."""
    if not value:
        return ""
    match = re.match(
        r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(?:([Zz+\-])(\d{2})?'?(\d{2})?'?)?",
        value.strip(),
    )
    if not match:
        return ""
    year, month, day, hour, minute, second, sign, tz_h, tz_m = match.groups()
    try:
        tz = UTC
        if sign in {"+", "-"} and tz_h:
            offset = timedelta(hours=int(tz_h), minutes=int(tz_m or 0))
            tz = timezone(offset if sign == "+" else -offset)
        moment = datetime(
            int(year), int(month or 1), int(day or 1), int(hour or 0), int(minute or 0), int(second or 0), tzinfo=tz
        )
    except ValueError:
        return ""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_metadata_title(title: str | None) -> str:
    """Return the metadata title unless it is empty or an obvious placeholder/filename."""
    title = (title or "").strip()
    if not title or _PLACEHOLDER_TITLES.match(title) or len(title) > 300:
        return ""
    return title


def page_image_coverage(page: pymupdf.Page) -> float:
    """Approximate fraction of the page area covered by images (overlaps are not merged)."""
    page_area = abs(page.rect) or 1.0
    covered = 0.0
    try:
        for info in page.get_image_info():
            rect = pymupdf.Rect(info["bbox"]) & page.rect
            if not rect.is_empty:
                covered += abs(rect)
    except Exception:
        return 0.0
    return min(1.0, covered / page_area)


def _sample_indices(page_count: int, limit: int) -> list[int]:
    if page_count <= limit:
        return list(range(page_count))
    step = page_count / limit
    return sorted({int(i * step) for i in range(limit)})


def analyze_pdf(path: Path | str, *, max_pages: int, ocr_available: bool) -> dict[str, Any]:
    """Validate a PDF and return preview information.

    Text availability is estimated from a sample of pages; nothing here claims content
    has been extracted.
    """
    with PYMUPDF_LOCK, open_pdf(path, max_pages=max_pages) as doc:
        meta = doc.metadata or {}
        indices = _sample_indices(doc.page_count, PREVIEW_SAMPLE_PAGES)
        text_pages = 0
        scanned_pages = 0
        image_count = 0
        for index in indices:
            page = doc.load_page(index)
            chars = len(page.get_text("text").strip())
            coverage = page_image_coverage(page)
            image_count += len(page.get_images(full=False))
            if chars >= MIN_TEXT_CHARS_PER_PAGE:
                text_pages += 1
            elif coverage > 0.3 or chars == 0:
                scanned_pages += 1

        first = doc.load_page(0)
        sampled = len(indices)
        if text_pages == 0:
            kind = "scanned" if scanned_pages else "no-text"
        elif scanned_pages:
            kind = "mixed"
        else:
            kind = "text"

        try:
            outline_entries = len(doc.get_toc(simple=True))
        except Exception:
            outline_entries = 0

        return {
            "page_count": doc.page_count,
            "title": clean_metadata_title(meta.get("title")),
            "metadata": {
                "title": (meta.get("title") or "").strip(),
                "author": (meta.get("author") or "").strip(),
                "subject": (meta.get("subject") or "").strip(),
                "keywords": (meta.get("keywords") or "").strip(),
                "creator": (meta.get("creator") or "").strip(),
                "producer": (meta.get("producer") or "").strip(),
                "created": parse_pdf_date(meta.get("creationDate")),
                "modified": parse_pdf_date(meta.get("modDate")),
                "pdf_version": (meta.get("format") or "").strip(),
            },
            "page_size": {"width": round(first.rect.width, 1), "height": round(first.rect.height, 1)},
            "has_outline": outline_entries > 0,
            "outline_entries": outline_entries,
            "sampled_pages": sampled,
            "sampled_text_pages": text_pages,
            "sampled_scanned_pages": scanned_pages,
            "sampled_images": image_count,
            "document_kind": kind,
            "text_extraction_available": text_pages > 0,
            "ocr_recommended": scanned_pages > 0,
            "ocr_available": ocr_available,
            "was_repaired": bool(getattr(doc, "is_repaired", False)),
        }
