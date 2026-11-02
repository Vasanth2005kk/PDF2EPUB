"""Optional OCR for scanned pages using Tesseract (via pytesseract)."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from functools import lru_cache

import pymupdf
from PIL import Image

from app.services.models import TextBlock, TextLine, TextRun

logger = logging.getLogger(__name__)

try:
    import pytesseract
except ImportError:  # pragma: no cover - pytesseract is a declared dependency
    pytesseract = None  # type: ignore[assignment]

# ISO 639-1 (EPUB language metadata) -> Tesseract traineddata names.
ISO_TO_TESSERACT = {
    "en": "eng", "ta": "tam", "hi": "hin", "te": "tel", "kn": "kan", "ml": "mal", "bn": "ben",
    "mr": "mar", "gu": "guj", "pa": "pan", "ur": "urd", "fr": "fra", "de": "deu", "es": "spa",
    "it": "ita", "pt": "por", "nl": "nld", "ru": "rus", "ar": "ara", "zh": "chi_sim", "ja": "jpn",
    "ko": "kor", "si": "sin", "el": "ell", "he": "heb", "tr": "tur", "pl": "pol", "sv": "swe",
}  # fmt: skip


class OcrError(Exception):
    pass


@dataclass(frozen=True)
class OcrStatus:
    available: bool
    version: str = ""
    languages: tuple[str, ...] = field(default_factory=tuple)
    message: str = ""


@lru_cache(maxsize=4)
def get_ocr_status(tesseract_cmd: str | None = None) -> OcrStatus:
    """Detect whether Tesseract is installed and which languages it supports (cached)."""
    if pytesseract is None:
        return OcrStatus(False, message="OCR is unavailable: the pytesseract package is not installed.")
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    elif not shutil.which("tesseract"):
        return OcrStatus(False, message="OCR is unavailable: Tesseract is not installed on the server.")
    try:
        version = str(pytesseract.get_tesseract_version())
        languages = tuple(lang for lang in pytesseract.get_languages(config="") if lang != "osd")
    except Exception as exc:
        logger.warning("Tesseract detection failed: %s", exc)
        return OcrStatus(False, message="OCR is unavailable: Tesseract could not be started.")
    return OcrStatus(True, version=version, languages=languages, message=f"Tesseract {version} available.")


def resolve_ocr_language(language: str, status: OcrStatus) -> tuple[str, str | None]:
    """Map a book language code to installed Tesseract languages.

    Returns ``(tesseract_lang, warning)``. English is added as a secondary language because
    mixed-language documents are common.
    """
    primary = ISO_TO_TESSERACT.get(language.split("-")[0].lower(), "eng")
    installed = set(status.languages)
    if primary in installed:
        if primary != "eng" and "eng" in installed:
            return f"{primary}+eng", None
        return primary, None
    fallback = "eng" if "eng" in installed else (status.languages[0] if status.languages else "eng")
    return fallback, (
        f"Tesseract language data for '{language}' ({primary}) is not installed; OCR used '{fallback}' instead."
    )


def ocr_page(page: pymupdf.Page, page_index: int, *, dpi: int, lang: str, timeout: int) -> list[TextBlock]:
    """Render a page and recognise its text. Returns blocks in page coordinates (points)."""
    if pytesseract is None:
        raise OcrError("pytesseract is not installed")
    try:
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
        image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    except Exception as exc:
        raise OcrError(f"page could not be rendered: {exc}") from exc

    try:
        data = pytesseract.image_to_data(
            image, lang=lang, config="--psm 3", output_type=pytesseract.Output.DICT, timeout=timeout
        )
    except RuntimeError as exc:  # pytesseract raises RuntimeError on timeout
        raise OcrError(f"OCR timed out after {timeout}s") from exc
    except Exception as exc:
        raise OcrError(str(exc)) from exc
    finally:
        image.close()

    scale = 72.0 / dpi
    # (block, paragraph) -> line -> list of words
    paragraphs: dict[tuple[int, int], dict[int, list[tuple[str, float, float, float, float]]]] = {}
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        if not word:
            continue
        try:
            confidence = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if confidence < 0:
            continue
        # Low-confidence specks and symbols are almost always noise from graphics or borders.
        if confidence < 30 and (len(word) <= 2 or not any(ch.isalnum() for ch in word)):
            continue
        x0 = data["left"][i] * scale
        y0 = data["top"][i] * scale
        x1 = x0 + data["width"][i] * scale
        y1 = y0 + data["height"][i] * scale
        key = (data["block_num"][i], data["par_num"][i])
        paragraphs.setdefault(key, {}).setdefault(data["line_num"][i], []).append((word, x0, y0, x1, y1))

    blocks: list[TextBlock] = []
    for key in sorted(paragraphs):
        lines: list[TextLine] = []
        for line_no in sorted(paragraphs[key]):
            words = sorted(paragraphs[key][line_no], key=lambda w: w[1])
            bbox = (
                min(w[1] for w in words),
                min(w[2] for w in words),
                max(w[3] for w in words),
                max(w[4] for w in words),
            )
            # Word boxes span ascender to descender; median height approximates the font size.
            heights = sorted(w[4] - w[2] for w in words)
            size = round(heights[len(heights) // 2], 1)
            text = " ".join(w[0] for w in words)
            lines.append(TextLine(runs=[TextRun(text)], bbox=bbox, size=size, bold=False, italic=False))
        if lines:
            bbox = (
                min(line.bbox[0] for line in lines),
                min(line.bbox[1] for line in lines),
                max(line.bbox[2] for line in lines),
                max(line.bbox[3] for line in lines),
            )
            blocks.append(TextBlock(lines=lines, bbox=bbox, page=page_index, source="ocr"))
    return blocks
