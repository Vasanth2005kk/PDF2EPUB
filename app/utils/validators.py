"""Input validation helpers: job IDs, filenames, upload signatures and conversion options."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import PurePath
from typing import Any

from app.exceptions import AppError, InvalidFileError

JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
PDF_SIGNATURE = b"%PDF-"
# The PDF spec allows junk before the header; readers accept it within the first 1 KiB.
SIGNATURE_SEARCH_WINDOW = 1024
ALLOWED_EXTENSIONS = {".pdf"}
ACCEPTED_MIME_TYPES = {"application/pdf", "application/x-pdf", "application/acrobat", "application/octet-stream", ""}

LANGUAGE_PATTERN = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$")
IMAGE_QUALITY_LEVELS = ("original", "high", "medium", "low")


def is_valid_job_id(job_id: str) -> bool:
    """Job IDs are UUID4 hex strings; anything else is rejected before touching the filesystem."""
    return bool(JOB_ID_PATTERN.fullmatch(job_id or ""))


def validate_upload_name(filename: str | None) -> str:
    """Validate the browser-supplied filename and return its display-safe form.

    The name is only used for display and as the default output name; it never
    becomes part of a filesystem path.
    """
    if not filename:
        raise InvalidFileError("No file was selected.")
    name = PurePath(filename.replace("\\", "/")).name
    if PurePath(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise InvalidFileError("Only PDF files (.pdf) can be converted.")
    return name


def validate_mime_type(mime_type: str | None) -> None:
    """Reject uploads whose declared type is clearly not a PDF (the signature is checked separately)."""
    base = (mime_type or "").split(";")[0].strip().lower()
    if base not in ACCEPTED_MIME_TYPES:
        raise InvalidFileError("The selected file does not appear to be a PDF document.")


def has_pdf_signature(head: bytes) -> bool:
    return PDF_SIGNATURE in head[:SIGNATURE_SEARCH_WINDOW]


def sanitize_output_filename(raw: str | None, fallback: str) -> str:
    """Return a safe ``*.epub`` filename built from user input (no directories, no control chars)."""
    candidate = (raw or "").strip() or fallback
    candidate = PurePath(candidate.replace("\\", "/")).name
    candidate = unicodedata.normalize("NFC", candidate)
    if candidate.lower().endswith(".epub"):
        candidate = candidate[:-5]
    elif candidate.lower().endswith(".pdf"):
        candidate = candidate[:-4]
    # Keep letters, digits and combining marks (e.g. Tamil vowel signs) from any script,
    # plus a few safe punctuation characters.
    candidate = "".join(
        ch if (ch.isalnum() or unicodedata.category(ch).startswith("M") or ch in " ._-()") else "_" for ch in candidate
    )
    candidate = re.sub(r"\s+", " ", candidate).strip(" ._") or "converted-book"
    return candidate[:120] + ".epub"


@dataclass(frozen=True)
class ConversionOptions:
    """User-selected conversion settings (validated)."""

    enable_ocr: bool = True
    extract_images: bool = True
    preserve_original_images: bool = True
    image_quality: str = "high"
    detect_headings: bool = True
    generate_toc: bool = True
    remove_headers_footers: bool = True
    extract_tables: bool = True
    generate_cover: bool = True
    output_filename: str = "converted-book.epub"
    language: str = "en"
    title: str = ""
    author: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversionOptions:
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


def _as_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "1", "yes", "on", "false", "0", "no", "off"}:
        return value.lower() in {"true", "1", "yes", "on"}
    raise AppError(f"Setting '{key}' must be true or false.")


def _as_text(data: dict[str, Any], key: str, max_length: int) -> str:
    value = data.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AppError(f"Setting '{key}' must be text.")
    value = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C").strip()
    if len(value) > max_length:
        raise AppError(f"Setting '{key}' must be at most {max_length} characters.")
    return value


def parse_conversion_options(data: dict[str, Any] | None, default_filename: str) -> ConversionOptions:
    """Validate the JSON settings sent with a conversion request."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise AppError("Conversion settings must be a JSON object.")

    defaults = ConversionOptions()
    language = _as_text(data, "language", 35) or defaults.language
    if not LANGUAGE_PATTERN.fullmatch(language):
        raise AppError("Language must be a language code such as 'en', 'ta' or 'en-US'.")

    image_quality = _as_text(data, "image_quality", 16) or defaults.image_quality
    if image_quality not in IMAGE_QUALITY_LEVELS:
        raise AppError(f"Image quality must be one of: {', '.join(IMAGE_QUALITY_LEVELS)}.")

    epub_version = str(data.get("epub_version", "3"))
    if epub_version not in {"3", "3.0"}:
        raise AppError("Only EPUB 3 output is supported.")

    return ConversionOptions(
        enable_ocr=_as_bool(data, "enable_ocr", defaults.enable_ocr),
        extract_images=_as_bool(data, "extract_images", defaults.extract_images),
        preserve_original_images=_as_bool(data, "preserve_original_images", defaults.preserve_original_images),
        image_quality=image_quality,
        detect_headings=_as_bool(data, "detect_headings", defaults.detect_headings),
        generate_toc=_as_bool(data, "generate_toc", defaults.generate_toc),
        remove_headers_footers=_as_bool(data, "remove_headers_footers", defaults.remove_headers_footers),
        extract_tables=_as_bool(data, "extract_tables", defaults.extract_tables),
        generate_cover=_as_bool(data, "generate_cover", defaults.generate_cover),
        output_filename=sanitize_output_filename(_as_text(data, "output_filename", 200), default_filename),
        language=language,
        title=_as_text(data, "title", 300),
        author=_as_text(data, "author", 300),
    )
