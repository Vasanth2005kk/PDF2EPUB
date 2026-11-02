"""Generate sample PDFs for tests (no binary fixtures are committed)."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

A4 = pymupdf.paper_rect("a4")
MARGIN = 72

LOREM = (
    "Reading on small screens works best when text can reflow. This paragraph is long enough to wrap "
    "across several lines so that paragraph reconstruction can be verified by the tests."
)

TAMIL_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansTamil-Regular.ttf",
    "/usr/share/fonts/truetype/lohit-tamil/Lohit-Tamil.ttf",
]
RASTER_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
]


def find_font(candidates: list[str]) -> str | None:
    return next((path for path in candidates if os.path.exists(path)), None)


def _textbox(
    page: pymupdf.Page,
    y: float,
    text: str,
    size: float = 11,
    font: str = "helv",
    height: float = 0,
    fontfile: str | None = None,
) -> float:
    """Insert wrapped text at ``y``; returns the y coordinate below it."""
    lines_estimate = max(1, int(len(text) * size * 0.5 / (A4.width - 2 * MARGIN)) + 1)
    box_height = height or lines_estimate * size * 1.5 + size
    rect = pymupdf.Rect(MARGIN, y, A4.width - MARGIN, y + box_height)
    remaining = page.insert_textbox(rect, text, fontsize=size, fontname=font, fontfile=fontfile)
    assert remaining >= 0, f"text did not fit: {text[:40]}"
    return y + box_height - remaining + size * 0.8


def _save(doc: pymupdf.Document, path: Path, **kwargs) -> Path:
    doc.save(path, **kwargs)
    doc.close()
    return path


def simple_text_pdf(path: Path, title: str = "A Simple Document") -> Path:
    doc = pymupdf.open()
    doc.set_metadata({"title": title, "author": "Test Author", "subject": "Testing", "keywords": "pdf, epub"})
    page = doc.new_page(width=A4.width, height=A4.height)
    y = _textbox(page, MARGIN, title, size=24, font="hebo")
    y = _textbox(page, y + 6, "First paragraph. " + LOREM)
    _textbox(page, y + 6, "Second paragraph. " + LOREM)
    return _save(doc, path)


def book_pdf(path: Path) -> Path:
    """Multi-page book: chapters, sub-headings, lists, running headers, page numbers and a
    paragraph that continues across a page break."""
    doc = pymupdf.open()
    doc.set_metadata({"title": "The Test Book", "author": "Jane Writer"})

    def new_page(number: int) -> pymupdf.Page:
        page = doc.new_page(width=A4.width, height=A4.height)
        page.insert_text((MARGIN, 40), "The Test Book - Running Header", fontsize=9, fontname="helv")
        page.insert_text((A4.width / 2, A4.height - 30), str(number), fontsize=9, fontname="helv")
        return page

    page = new_page(1)
    y = _textbox(page, MARGIN, "Chapter 1: The Beginning", size=20, font="hebo")
    y = _textbox(page, y, "Opening paragraph of chapter one. " + LOREM)
    y = _textbox(page, y, "1.1 Background", size=14, font="hebo")
    y = _textbox(page, y, "Background paragraph text. " + LOREM)
    # Base-14 fonts cannot encode "•", so the list uses a TrueType font when available.
    unicode_font = find_font(RASTER_FONT_CANDIDATES)
    for item in ("Bullet item alpha", "Bullet item beta", "Bullet item gamma"):
        if unicode_font:
            y = _textbox(page, y, f"• {item}", font="dejavu", fontfile=unicode_font)
        else:
            y = _textbox(page, y, f"- {item}")
    # A paragraph that runs off the bottom of the page and continues on page 2.
    _textbox(page, A4.height - 140, "This sentence starts on the first page and continues", height=40)

    page = new_page(2)
    y = _textbox(page, MARGIN, "onto the second page without a break in the middle.")
    y = _textbox(page, y, "1.2 Details", size=14, font="hebo")
    for index, item in enumerate(("First numbered step", "Second numbered step", "Third numbered step"), 1):
        y = _textbox(page, y, f"{index}. {item}")
    _textbox(page, y, "Closing paragraph of chapter one. " + LOREM)

    page = new_page(3)
    y = _textbox(page, MARGIN, "Chapter 2: The Middle", size=20, font="hebo")
    y = _textbox(page, y, "Chapter two has its own text. " + LOREM)
    y = _textbox(page, y, "2.1 Analysis", size=14, font="hebo")
    _textbox(page, y, "Analysis paragraph. " + LOREM)

    page = new_page(4)
    y = _textbox(page, MARGIN, "Chapter 3: The End", size=20, font="hebo")
    _textbox(page, y, "Final chapter text. " + LOREM)
    return _save(doc, path)


def _png(color: tuple[int, ...], size: tuple[int, int] = (200, 120), mode: str = "RGB") -> bytes:
    image = Image.new(mode, size, color)
    draw = ImageDraw.Draw(image)
    if min(size) > 20:
        draw.rectangle([10, 10, size[0] - 10, size[1] - 10], outline=(0, 0, 0) if mode == "RGB" else (0, 0, 0, 255))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _jpeg(color: tuple[int, int, int], size: tuple[int, int] = (240, 160)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="JPEG", quality=90)
    return out.getvalue()


def images_pdf(path: Path) -> Path:
    """Two pages, three distinct images (PNG, JPEG, PNG with alpha); the PNG appears twice."""
    doc = pymupdf.open()
    doc.set_metadata({"title": "Pictures"})
    png = _png((200, 40, 40))
    page = doc.new_page(width=A4.width, height=A4.height)
    y = _textbox(page, MARGIN, "Text above the first picture. " + LOREM)
    page.insert_image(pymupdf.Rect(MARGIN, y, MARGIN + 200, y + 120), stream=png)
    y += 140
    y = _textbox(page, y, "Text between pictures. " + LOREM)
    page.insert_image(pymupdf.Rect(MARGIN, y, MARGIN + 240, y + 160), stream=_jpeg((40, 160, 40)))

    page = doc.new_page(width=A4.width, height=A4.height)
    y = _textbox(page, MARGIN, "Second page text. " + LOREM)
    page.insert_image(pymupdf.Rect(MARGIN, y, MARGIN + 200, y + 120), stream=_png((40, 40, 200, 128), mode="RGBA"))
    page.insert_image(pymupdf.Rect(MARGIN, y + 150, MARGIN + 200, y + 270), stream=png)  # duplicate
    return _save(doc, path)


def tiny_image_pdf(path: Path) -> Path:
    """Only decorative 4x4 px images: nothing should be extracted."""
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    _textbox(page, MARGIN, "Text with a decorative dot. " + LOREM)
    page.insert_image(pymupdf.Rect(MARGIN, 300, MARGIN + 4, 304), stream=_png((0, 0, 0), size=(4, 4)))
    return _save(doc, path)


def scanned_pdf(path: Path, lines: list[str] | None = None) -> Path:
    """A page that is a raster image of text (no text layer)."""
    lines = lines or ["Scanned Document Title", "", "This page was scanned from paper.", "OCR should read it."]
    font_path = find_font(RASTER_FONT_CANDIDATES)
    width, height = 1240, 1754  # A4 at 150 dpi
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    y = 150
    for index, line in enumerate(lines):
        size = 56 if index == 0 else 36
        font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        draw.text((120, y), line, fill=0, font=font)
        y += int(size * 1.8)
    out = io.BytesIO()
    image.save(out, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_image(page.rect, stream=out.getvalue())
    return _save(doc, path)


def encrypted_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((MARGIN, MARGIN), "Secret content", fontsize=12)
    return _save(doc, path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="user-secret", owner_pw="owner-secret")


def blank_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    doc.new_page()
    return _save(doc, path)


def corrupted_pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.7\n" + os.urandom(2048) + b"\n%%EOF")
    return path


def tamil_pdf(path: Path) -> Path | None:
    font = find_font(TAMIL_FONT_CANDIDATES)
    if font is None:
        return None
    doc = pymupdf.open()
    doc.set_metadata({"title": "Tamil and English"})
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_text((MARGIN, MARGIN), "Bilingual Sample", fontsize=22, fontname="hebo")
    page.insert_text((MARGIN, MARGIN + 50), "English line of text in the sample document.", fontsize=12)
    page.insert_text((MARGIN, MARGIN + 80), "தமிழ் ஒரு செம்மொழி", fontsize=12, fontname="tamil", fontfile=font)
    unicode_font = find_font(RASTER_FONT_CANDIDATES)
    if unicode_font:
        page.insert_text(
            (MARGIN, MARGIN + 110),
            "Unicode: café naïve – “quotes” — €",
            fontsize=12,
            fontname="dejavu",
            fontfile=unicode_font,
        )
    return _save(doc, path)


def table_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    _textbox(page, MARGIN, "A document with a ruled table below. " + LOREM)
    rows = [["Name", "Quantity", "Price"], ["Apples", "3", "1.20"], ["Pears", "5", "2.40"]]
    x0, y0, col_w, row_h = MARGIN, 220, 120, 24
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            rect = pymupdf.Rect(x0 + c * col_w, y0 + r * row_h, x0 + (c + 1) * col_w, y0 + (r + 1) * row_h)
            page.draw_rect(rect, color=(0, 0, 0), width=0.8)
            page.insert_text((rect.x0 + 4, rect.y1 - 8), cell, fontsize=10)
    return _save(doc, path)


def outline_pdf(path: Path) -> Path:
    """Chapters marked only by PDF bookmarks (headings are plain body-size text)."""
    doc = pymupdf.open()
    toc = []
    for number in range(1, 4):
        page = doc.new_page(width=A4.width, height=A4.height)
        y = _textbox(page, MARGIN, f"Part {number} overview")
        _textbox(page, y, f"Body text for part {number}. " + LOREM)
        toc.append([1, f"Part {number} overview", number])
    toc.insert(2, [2, "Nested bookmark", 2])
    doc.set_toc(toc)
    return _save(doc, path)


def two_column_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=A4.width, height=A4.height)
    mid = A4.width / 2
    page.insert_textbox(pymupdf.Rect(MARGIN, MARGIN, A4.width - MARGIN, MARGIN + 40), "Two Column Article",
                        fontsize=20, fontname="hebo", align=pymupdf.TEXT_ALIGN_CENTER)  # fmt: skip
    left = "Left column first paragraph. " + LOREM + " Left column second sentence ends here."
    right = "Right column paragraph. " + LOREM + " Right column ends."
    page.insert_textbox(pymupdf.Rect(MARGIN, 140, mid - 12, 500), left, fontsize=10)
    page.insert_textbox(pymupdf.Rect(mid + 12, 140, A4.width - MARGIN, 500), right, fontsize=10)
    return _save(doc, path)


def large_pdf(path: Path, pages: int) -> Path:
    doc = pymupdf.open()
    for number in range(1, pages + 1):
        page = doc.new_page(width=A4.width, height=A4.height)
        _textbox(page, MARGIN, f"Page {number} body text. " + LOREM)
    return _save(doc, path)
