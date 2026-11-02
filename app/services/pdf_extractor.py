"""Page-level extraction of text (with font information), images and tables using PyMuPDF."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from PIL import Image

from app.services.models import BBox, ImageRef, TableData, TextBlock, TextLine, TextRun

logger = logging.getLogger(__name__)

TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP  # ligatures are expanded
FLAG_SUPERSCRIPT = 1
FLAG_ITALIC = 2
FLAG_MONOSPACE = 8
FLAG_BOLD = 16

_BOLD_FONT = re.compile(r"bold|black|heavy|semibold|demi", re.I)
_ITALIC_FONT = re.compile(r"italic|oblique", re.I)
# XML 1.0 forbids most C0 controls; also drop BOM / zero-width no-break space.
_INVALID_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f﻿￾￿]")

MIN_IMAGE_PIXELS = 16  # images narrower or shorter than this are decorative rules/dots
MIN_IMAGE_POINTS = 6
QUALITY_PRESETS = {  # max dimension (px), JPEG quality
    "high": (2400, 90),
    "medium": (1600, 80),
    "low": (1000, 65),
}


def clean_text(text: str) -> str:
    text = _INVALID_CHARS.sub("", text.replace("\t", " "))
    return unicodedata.normalize("NFC", text)


# ----- text --------------------------------------------------------------------------------


def _merge_runs(runs: list[TextRun]) -> list[TextRun]:
    merged: list[TextRun] = []
    for run in runs:
        if merged:
            last = merged[-1]
            same = (last.bold, last.italic, last.superscript, last.monospace) == (
                run.bold,
                run.italic,
                run.superscript,
                run.monospace,
            )
            if same:
                last.text += run.text
                continue
        merged.append(TextRun(run.text, run.bold, run.italic, run.superscript, run.monospace))
    return merged


def _build_line(spans: list[dict]) -> TextLine | None:
    runs: list[TextRun] = []
    size_weights: dict[float, int] = {}
    bold_chars = italic_chars = total_chars = 0
    fonts: dict[str, int] = {}
    x0 = y0 = float("inf")
    x1 = y1 = float("-inf")
    previous_end: float | None = None

    for span in spans:
        text = clean_text(span.get("text", ""))
        if not text:
            continue
        flags = span.get("flags", 0)
        font = span.get("font", "") or ""
        bold = bool(flags & FLAG_BOLD) or bool(_BOLD_FONT.search(font))
        italic = bool(flags & FLAG_ITALIC) or bool(_ITALIC_FONT.search(font))
        sx0, sy0, sx1, sy1 = span["bbox"]
        size = float(span.get("size", 0) or 0)

        # Spans placed with a visible gap but no space character: insert one.
        if (
            runs
            and previous_end is not None
            and sx0 - previous_end > max(size, 1) * 0.25
            and not runs[-1].text.endswith(" ")
            and not text.startswith(" ")
        ):
            runs[-1].text += " "
        previous_end = sx1

        runs.append(
            TextRun(
                text=text,
                bold=bold,
                italic=italic,
                superscript=bool(flags & FLAG_SUPERSCRIPT),
                monospace=bool(flags & FLAG_MONOSPACE),
            )
        )
        weight = len(text.strip())
        if weight:
            size_weights[round(size, 1)] = size_weights.get(round(size, 1), 0) + weight
            fonts[font] = fonts.get(font, 0) + weight
            total_chars += weight
            bold_chars += weight if bold else 0
            italic_chars += weight if italic else 0
        x0, y0, x1, y1 = min(x0, sx0), min(y0, sy0), max(x1, sx1), max(y1, sy1)

    if not runs or total_chars == 0:
        return None
    return TextLine(
        runs=_merge_runs(runs),
        bbox=(x0, y0, x1, y1),
        size=max(size_weights, key=size_weights.__getitem__),
        bold=bold_chars >= total_chars * 0.6,
        italic=italic_chars >= total_chars * 0.6,
        font=max(fonts, key=fonts.__getitem__),
    )


def _same_row(a: BBox, b: BBox) -> bool:
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    height = min(a[3] - a[1], b[3] - b[1])
    return height > 0 and overlap / height > 0.6 and b[0] >= a[2] - 1


def _join_same_row_lines(lines: list[TextLine]) -> list[TextLine]:
    """PyMuPDF may split one visual line into several (e.g. justified text); rejoin them."""
    result: list[TextLine] = []
    for line in lines:
        if result and _same_row(result[-1].bbox, line.bbox):
            prev = result[-1]
            if not prev.text.endswith(" ") and not line.text.startswith(" "):
                prev.runs[-1].text += " "
            prev.runs = _merge_runs(prev.runs + line.runs)
            prev.bbox = (
                min(prev.bbox[0], line.bbox[0]),
                min(prev.bbox[1], line.bbox[1]),
                max(prev.bbox[2], line.bbox[2]),
                max(prev.bbox[3], line.bbox[3]),
            )
            continue
        result.append(line)
    return result


def extract_text_blocks(page: pymupdf.Page, page_index: int) -> tuple[list[TextBlock], int]:
    """Extract text blocks with per-line font size/weight. Returns ``(blocks, character_count)``."""
    data = page.get_text("dict", flags=TEXT_FLAGS)
    blocks: list[TextBlock] = []
    char_count = 0
    for raw_block in data.get("blocks", []):
        if raw_block.get("type") != 0:
            continue
        lines = [line for raw in raw_block.get("lines", []) if (line := _build_line(raw.get("spans", [])))]
        lines = _join_same_row_lines(lines)
        if not lines:
            continue
        for line in lines:
            char_count += len(line.text.strip())
        bbox = (
            min(line.bbox[0] for line in lines),
            min(line.bbox[1] for line in lines),
            max(line.bbox[2] for line in lines),
            max(line.bbox[3] for line in lines),
        )
        blocks.append(TextBlock(lines=lines, bbox=bbox, page=page_index))
    return blocks, char_count


# ----- tables ------------------------------------------------------------------------------


def extract_tables(page: pymupdf.Page, page_index: int) -> list[TableData]:
    """Detect ruled tables. Conservative: only tables drawn with lines, at least 2x2, mostly filled.

    Cell text is assembled from word boxes in native reading order; ``Table.extract()`` can split
    small glyphs (such as decimal points) onto separate lines.
    """
    try:
        found = page.find_tables(strategy="lines", use_layout=False)  # the ML layout model costs ~0.1 s/page
    except Exception as exc:
        logger.debug("Table detection failed on page %d: %s", page_index + 1, exc)
        return []
    if not found.tables:
        return []
    words = page.get_text("words", flags=TEXT_FLAGS)
    tables: list[TableData] = []
    for table in found.tables:
        rows: list[list[str]] = []
        for row in table.rows:
            cells: list[str] = []
            for cell in row.cells:
                if cell is None:
                    cells.append("")
                    continue
                x0, y0, x1, y1 = cell
                inside = [w for w in words if x0 <= (w[0] + w[2]) / 2 <= x1 and y0 <= (w[1] + w[3]) / 2 <= y1]
                inside.sort(key=lambda w: (w[5], w[6], w[7]))
                cells.append(clean_text(" ".join(w[4] for w in inside)).strip())
            rows.append(cells)
        rows = [row for row in rows if any(row)]
        if len(rows) < 2 or max(len(row) for row in rows) < 2:
            continue
        cells_flat = [cell for row in rows for cell in row]
        if sum(1 for cell in cells_flat if cell) / len(cells_flat) < 0.4:
            continue
        tables.append(TableData(rows=rows, bbox=tuple(table.bbox), page=page_index))
    return tables


# ----- images ------------------------------------------------------------------------------


@dataclass
class ImageSettings:
    preserve_original: bool = True
    quality: str = "high"
    max_pixels: int = 60_000_000


@dataclass
class _StoredImage:
    filename: str
    media_type: str
    width: int
    height: int
    digest: str


@dataclass
class ImageStore:
    """Writes images to disk once, de-duplicated by xref and by content hash."""

    directory: Path
    by_xref: dict[int, _StoredImage] = field(default_factory=dict)
    by_digest: dict[str, _StoredImage] = field(default_factory=dict)
    counter: int = 0

    def store(self, data: bytes, ext: str, width: int, height: int) -> _StoredImage:
        digest = hashlib.sha256(data).hexdigest()
        if digest in self.by_digest:
            return self.by_digest[digest]
        self.counter += 1
        filename = f"img_{self.counter:04d}.{ext}"
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / filename).write_bytes(data)
        stored = _StoredImage(filename, "image/jpeg" if ext == "jpg" else f"image/{ext}", width, height, digest)
        self.by_digest[digest] = stored
        return stored


def _pixmap_to_rgb(pix: pymupdf.Pixmap) -> pymupdf.Pixmap:
    if pix.colorspace and pix.colorspace.n not in (1, 3):
        return pymupdf.Pixmap(pymupdf.csRGB, pix)
    return pix


def _pixmap_png(pix: pymupdf.Pixmap) -> tuple[bytes, int, int]:
    pix = _pixmap_to_rgb(pix)
    return pix.tobytes("png"), pix.width, pix.height


def _raw_image(doc: pymupdf.Document, xref: int) -> tuple[bytes, str, int, int]:
    """Return image bytes in a browser/e-reader friendly format (JPEG or PNG), preserving originals."""
    raw = doc.extract_image(xref)
    if not raw:
        raise ValueError("image could not be extracted")
    ext = (raw.get("ext") or "").lower()
    smask = raw.get("smask") or 0
    components = raw.get("colorspace") or 0

    if smask:
        base = pymupdf.Pixmap(doc, xref)
        try:
            masked = pymupdf.Pixmap(_pixmap_to_rgb(base) if base.alpha == 0 else base, pymupdf.Pixmap(doc, smask))
            data, width, height = _pixmap_png(masked)
        except Exception:
            data, width, height = _pixmap_png(base)
        return data, "png", width, height

    if ext in {"jpeg", "jpg"} and components in (1, 3):
        return raw["image"], "jpg", raw["width"], raw["height"]
    if ext == "png":
        return raw["image"], "png", raw["width"], raw["height"]
    # JPX, JBIG2, CCITT, CMYK JPEG, ...: decode and re-encode losslessly as PNG.
    data, width, height = _pixmap_png(pymupdf.Pixmap(doc, xref))
    return data, "png", width, height


def _recompress(data: bytes, ext: str, quality: str) -> tuple[bytes, str, int, int]:
    max_dim, jpeg_quality = QUALITY_PRESETS[quality]
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
        if max(image.size) > max_dim:
            image.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        if has_alpha or (ext == "png" and image.mode in ("1", "P")):
            image.save(out, format="PNG", optimize=True)
            result_ext = "png"
        else:
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            image.save(out, format="JPEG", quality=jpeg_quality, optimize=True)
            result_ext = "jpg"
        return out.getvalue(), result_ext, image.width, image.height


def _verify_image(data: bytes) -> None:
    with Image.open(io.BytesIO(data)) as image:
        image.verify()


def extract_page_images(
    doc: pymupdf.Document,
    page: pymupdf.Page,
    page_index: int,
    store: ImageStore,
    settings: ImageSettings,
    *,
    skip_page_scans: bool = False,
) -> tuple[list[ImageRef], list[str]]:
    """Extract the images placed on a page. Returns ``(images, warnings)``.

    ``skip_page_scans`` omits images that cover most of the page (used when the page text
    was recovered by OCR and the scan itself would only duplicate it).
    """
    refs: list[ImageRef] = []
    warnings: list[str] = []
    page_rect = page.rect
    page_area = abs(page_rect) or 1.0
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception as exc:
        return [], [f"Images on page {page_index + 1} could not be read ({exc.__class__.__name__})."]

    for info in infos:
        bbox = pymupdf.Rect(info["bbox"]) & page_rect
        if bbox.is_empty or bbox.width < MIN_IMAGE_POINTS or bbox.height < MIN_IMAGE_POINTS:
            continue
        width, height = int(info.get("width", 0)), int(info.get("height", 0))
        if width < MIN_IMAGE_PIXELS or height < MIN_IMAGE_PIXELS:
            continue
        if skip_page_scans and abs(bbox) / page_area > 0.6:
            continue
        if width * height > settings.max_pixels:
            warnings.append(f"An image on page {page_index + 1} is too large ({width}x{height} px) and was skipped.")
            continue

        xref = int(info.get("xref", 0) or 0)
        stored = store.by_xref.get(xref) if xref else None
        if stored is None:
            try:
                if xref:
                    data, ext, width, height = _raw_image(doc, xref)
                else:
                    # Inline image without an xref: render its area instead.
                    pix = page.get_pixmap(clip=bbox, dpi=150, alpha=False)
                    data, width, height = _pixmap_png(pix)
                    ext = "png"
                _verify_image(data)
                if not settings.preserve_original and settings.quality in QUALITY_PRESETS:
                    data, ext, width, height = _recompress(data, ext, settings.quality)
            except Exception as exc:
                logger.info("Image xref %s on page %d failed: %s", xref, page_index + 1, exc)
                try:
                    pix = page.get_pixmap(clip=bbox, dpi=150, alpha=False)
                    data, width, height = _pixmap_png(pix)
                    ext = "png"
                    warnings.append(
                        f"An image on page {page_index + 1} uses an unsupported format and was rendered instead."
                    )
                except Exception:
                    warnings.append(f"An image on page {page_index + 1} could not be extracted.")
                    continue
            stored = store.store(data, ext, width, height)
            if xref:
                store.by_xref[xref] = stored

        refs.append(
            ImageRef(
                filename=stored.filename,
                media_type=stored.media_type,
                width=stored.width,
                height=stored.height,
                page=page_index,
                bbox=tuple(bbox),
                digest=stored.digest,
            )
        )
    return refs, warnings


def render_page_image(page: pymupdf.Page, store: ImageStore, page_index: int, *, dpi: int = 150) -> ImageRef:
    """Render a whole page as an image (used for scanned pages that could not be OCR'd)."""
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    pix = _pixmap_to_rgb(pix)
    stored = store.store(pix.tobytes("jpeg", jpg_quality=85), "jpg", pix.width, pix.height)
    return ImageRef(
        filename=stored.filename,
        media_type=stored.media_type,
        width=stored.width,
        height=stored.height,
        page=page_index,
        bbox=tuple(page.rect),
        digest=stored.digest,
        is_page_scan=True,
    )


def render_cover(doc: pymupdf.Document, max_height: int = 1600) -> bytes:
    """Render the first page as a JPEG cover image."""
    page = doc.load_page(0)
    zoom = max(0.5, min(4.0, max_height / max(page.rect.height, 1)))
    pix = _pixmap_to_rgb(page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False))
    return pix.tobytes("jpeg", jpg_quality=85)
