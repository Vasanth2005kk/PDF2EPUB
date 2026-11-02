"""PDF validation, text/image extraction, OCR and structure reconstruction."""

from __future__ import annotations

import io
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from app.exceptions import AppError, CorruptedPDFError, EmptyPDFError, EncryptedPDFError, InvalidFileError
from app.services.content_processor import (
    StructureOptions,
    _append_with_join,
    build_book,
    order_page_items,
)
from app.services.models import (
    BookMetadata,
    Heading,
    ListBlock,
    OutlineEntry,
    PageContent,
    PageMarker,
    Paragraph,
    Table,
    TextBlock,
    TextLine,
    TextRun,
)
from app.services.ocr_service import OcrStatus, get_ocr_status, ocr_page, resolve_ocr_language
from app.services.pdf_analyzer import analyze_pdf, clean_metadata_title, open_pdf, parse_pdf_date
from app.services.pdf_extractor import (
    ImageSettings,
    ImageStore,
    extract_page_images,
    extract_tables,
    extract_text_blocks,
)
from app.utils.validators import (
    is_valid_job_id,
    parse_conversion_options,
    sanitize_output_filename,
    validate_upload_name,
)
from tests.conftest import requires_ocr

# ----- validation ------------------------------------------------------------------------


def test_valid_pdf_opens(make_pdf):
    with open_pdf(make_pdf("simple_text_pdf")) as doc:
        assert doc.page_count == 1


@pytest.mark.parametrize(
    ("builder", "error"),
    [
        ("encrypted_pdf", EncryptedPDFError),
        ("corrupted_pdf", CorruptedPDFError),
    ],
)
def test_unusable_pdfs_are_rejected(make_pdf, builder, error):
    with pytest.raises(error), open_pdf(make_pdf(builder)):
        pass


def test_empty_file_rejected(tmp_path: Path):
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(EmptyPDFError), open_pdf(empty):
        pass


def test_invalid_signature_rejected(tmp_path: Path):
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"This is plain text pretending to be a PDF")
    with pytest.raises(InvalidFileError, match="signature"), open_pdf(fake):
        pass


def test_page_limit_enforced(make_pdf):
    with pytest.raises(AppError, match="maximum supported"), open_pdf(make_pdf("book_pdf"), max_pages=2):
        pass


def test_job_id_and_filename_validation():
    assert is_valid_job_id("0" * 32)
    for bad in ("", "../etc/passwd", "0" * 31, "Z" * 32, "0" * 32 + "/"):
        assert not is_valid_job_id(bad)
    assert validate_upload_name("C:\\Users\\me\\Book.PDF") == "Book.PDF"
    with pytest.raises(InvalidFileError):
        validate_upload_name("notes.txt")
    assert sanitize_output_filename("../../etc/passwd", "x.pdf") == "passwd.epub"
    assert sanitize_output_filename("", "My Report.pdf") == "My Report.epub"
    assert sanitize_output_filename("தமிழ் நூல்.epub", "x.pdf") == "தமிழ் நூல்.epub"
    assert sanitize_output_filename("a<b>:c", "x.pdf") == "a_b__c.epub"


@pytest.mark.parametrize(
    "payload",
    [{"language": "english!"}, {"image_quality": "ultra"}, {"enable_ocr": "maybe"}, {"epub_version": "2"}, []],
)
def test_invalid_conversion_options(payload):
    with pytest.raises(AppError):
        parse_conversion_options(payload, "book.pdf")


def test_conversion_option_defaults():
    options = parse_conversion_options({}, "Book.pdf")
    assert options.output_filename == "Book.epub"
    assert options.language == "en"
    assert options.extract_images and options.generate_toc


def test_metadata_helpers():
    assert parse_pdf_date("D:20240131120000+05'30'") == "2024-01-31T06:30:00Z"
    assert parse_pdf_date("garbage") == ""
    assert clean_metadata_title("Microsoft Word - draft.docx") == ""
    assert clean_metadata_title("A Real Title") == "A Real Title"


def test_analyze_text_pdf(make_pdf):
    info = analyze_pdf(make_pdf("simple_text_pdf"), max_pages=100, ocr_available=False)
    assert info["page_count"] == 1
    assert info["title"] == "A Simple Document"
    assert info["metadata"]["author"] == "Test Author"
    assert info["document_kind"] == "text"
    assert info["text_extraction_available"] is True
    assert info["ocr_recommended"] is False


def test_analyze_scanned_pdf(make_pdf):
    info = analyze_pdf(make_pdf("scanned_pdf"), max_pages=100, ocr_available=True)
    assert info["document_kind"] == "scanned"
    assert info["text_extraction_available"] is False
    assert info["ocr_recommended"] is True


# ----- text extraction -------------------------------------------------------------------


def _blocks(path: Path, page_index: int = 0) -> list[TextBlock]:
    with pymupdf.open(path) as doc:
        blocks, _ = extract_text_blocks(doc[page_index], page_index)
    return blocks


def test_text_extraction_with_font_info(make_pdf):
    blocks = _blocks(make_pdf("simple_text_pdf"))
    title = blocks[0].lines[0]
    assert title.text.strip() == "A Simple Document"
    assert title.size == pytest.approx(24, abs=0.5)
    assert title.bold
    body = next(b for b in blocks if b.text.startswith("First paragraph"))
    assert len(body.lines) >= 2
    assert not body.lines[0].bold
    assert body.lines[0].size == pytest.approx(11, abs=0.5)


def test_multi_page_extraction(make_pdf):
    path = make_pdf("book_pdf")
    with pymupdf.open(path) as doc:
        texts = [" ".join(b.text for b in extract_text_blocks(doc[i], i)[0]) for i in range(doc.page_count)]
    assert len(texts) == 4
    assert "Chapter 1" in texts[0] and "Chapter 2" in texts[2] and "Chapter 3" in texts[3]


def test_unicode_and_tamil_text(make_pdf):
    text = " ".join(b.text for b in _blocks(make_pdf("tamil_pdf")))
    assert "தமிழ் ஒரு செம்மொழி" in text
    assert "English line of text" in text
    assert "café naïve" in text and "€" in text


def _build(pages_pdf: Path, **options) -> object:
    pages: list[PageContent] = []
    with pymupdf.open(pages_pdf) as doc:
        for index, page in enumerate(doc):
            blocks, chars = extract_text_blocks(page, index)
            content = PageContent(index, page.rect.width, page.rect.height, blocks=blocks, char_count=chars)
            content.tables = extract_tables(page, index)
            pages.append(content)
        outline = [OutlineEntry(level, title, number - 1) for level, title, number in doc.get_toc()]
    metadata = BookMetadata(title="", language="en", identifier="urn:uuid:test")
    return build_book(pages, outline, StructureOptions(**options), metadata, fallback_title="Fallback")


def _all_elements(book) -> list:
    return [element for chapter in book.chapters for element in chapter.elements]


def test_heading_and_chapter_detection(make_pdf):
    book = _build(make_pdf("book_pdf"))
    assert book.structure_source == "headings"
    assert [c.title for c in book.chapters] == [
        "Chapter 1: The Beginning",
        "Chapter 2: The Middle",
        "Chapter 3: The End",
    ]
    assert [n.title for n in book.chapters[0].toc_children] == ["1.1 Background", "1.2 Details"]
    headings = [e for e in _all_elements(book) if isinstance(e, Heading)]
    assert all(h.anchor for h in headings)


def test_paragraphs_lists_and_page_continuation(make_pdf):
    elements = _all_elements(_build(make_pdf("book_pdf")))
    paragraphs = [e.text for e in elements if isinstance(e, Paragraph)]
    # Lines are joined into whole paragraphs.
    assert any(
        p.startswith("Opening paragraph of chapter one.") and p.endswith("verified by the tests.") for p in paragraphs
    )
    # A sentence broken by a page break is merged into one paragraph.
    assert (
        "This sentence starts on the first page and continues onto the second page without a break in the middle."
        in paragraphs
    )
    lists = [e for e in elements if isinstance(e, ListBlock)]
    assert any(not lst.ordered and len(lst.items) == 3 for lst in lists)
    ordered = next(lst for lst in lists if lst.ordered)
    assert next("".join(r.text for r in item) for item in ordered.items) == "First numbered step"
    # Running headers and page numbers are removed.
    all_text = " ".join(paragraphs)
    assert "Running Header" not in all_text


def test_headers_kept_when_disabled(make_pdf):
    elements = _all_elements(_build(make_pdf("book_pdf"), remove_headers_footers=False))
    assert any("Running Header" in e.text for e in elements if isinstance(e, Paragraph))


def test_heading_detection_can_be_disabled(make_pdf):
    book = _build(make_pdf("book_pdf"), detect_headings=False)
    assert book.structure_source == "page-ranges"
    assert not any(isinstance(e, Heading) for e in _all_elements(book))


def test_outline_drives_chapters(make_pdf):
    book = _build(make_pdf("outline_pdf"))
    assert book.structure_source == "outline"
    assert [c.title for c in book.chapters] == ["Part 1 overview", "Part 2 overview", "Part 3 overview"]
    assert [n.title for n in book.chapters[1].toc_children] == ["Nested bookmark"]


def test_page_range_sections_without_headings(make_pdf):
    book = _build(make_pdf("large_pdf", pages=25))
    assert book.structure_source == "page-ranges"
    assert [c.title for c in book.chapters] == ["Pages 1–10", "Pages 11–20", "Pages 21–25"]


def test_two_column_reading_order(make_pdf):
    paragraphs = [e.text for e in _all_elements(_build(make_pdf("two_column_pdf"))) if isinstance(e, Paragraph)]
    left = next(i for i, p in enumerate(paragraphs) if p.startswith("Left column"))
    right = next(i for i, p in enumerate(paragraphs) if p.startswith("Right column"))
    assert left < right
    assert "Right column" not in paragraphs[left]


def test_table_extraction(make_pdf):
    tables = [e for e in _all_elements(_build(make_pdf("table_pdf"))) if isinstance(e, Table)]
    assert tables and tables[0].rows == [["Name", "Quantity", "Price"], ["Apples", "3", "1.20"], ["Pears", "5", "2.40"]]


def test_dehyphenation_rules():
    runs = [TextRun("recon-")]
    _append_with_join(runs, [TextRun("struction works")])
    assert "".join(r.text for r in runs) == "reconstruction works"
    runs = [TextRun("well-")]
    _append_with_join(runs, [TextRun("Known")])  # capitalised: keep the hyphen
    assert "".join(r.text for r in runs) == "well- Known"
    runs = [TextRun("end of line")]
    _append_with_join(runs, [TextRun("next line")])
    assert "".join(r.text for r in runs) == "end of line next line"


def test_column_ordering_helper():
    def block(x0, y0, x1, y1, text):
        line = TextLine([TextRun(text)], (x0, y0, x1, y1), 10, False, False)
        return TextBlock([line], (x0, y0, x1, y1), 0)

    items = [
        block(320, 100, 540, 300, "R1" * 30),
        block(50, 100, 280, 300, "L1" * 30),
        block(50, 320, 280, 500, "L2" * 30),
        block(320, 320, 540, 500, "R2" * 30),
    ]
    order = [b.text[:2] for b in order_page_items(items, 595)]
    assert order == ["L1", "L2", "R1", "R2"]


# ----- images ----------------------------------------------------------------------------


def _extract_all_images(path: Path, tmp_path: Path, settings: ImageSettings | None = None):
    store = ImageStore(tmp_path / "images")
    refs = []
    warnings = []
    with pymupdf.open(path) as doc:
        for index, page in enumerate(doc):
            page_refs, page_warnings = extract_page_images(doc, page, index, store, settings or ImageSettings())
            refs += page_refs
            warnings += page_warnings
    return store, refs, warnings


def test_images_extracted_and_deduplicated(make_pdf, tmp_path):
    _store, refs, warnings = _extract_all_images(make_pdf("images_pdf"), tmp_path)
    assert not warnings
    assert len(refs) == 4  # four placements...
    files = sorted(p.name for p in (tmp_path / "images").iterdir())
    assert len(files) == 3  # ...of three distinct images
    assert {Path(f).suffix for f in files} == {".png", ".jpg"}
    assert refs[0].filename == refs[-1].filename  # the repeated PNG reuses one file
    assert [r.page for r in refs] == [0, 0, 1, 1]
    assert refs[0].bbox[1] < refs[1].bbox[1]  # placement order on the page
    for name in files:
        with Image.open(tmp_path / "images" / name) as image:
            image.verify()


def test_transparent_image_keeps_alpha(make_pdf, tmp_path):
    _, refs, _ = _extract_all_images(make_pdf("images_pdf"), tmp_path)
    with Image.open(tmp_path / "images" / refs[2].filename) as image:
        assert image.mode in ("RGBA", "LA")


def test_pdf_without_images(make_pdf, tmp_path):
    _, refs, _ = _extract_all_images(make_pdf("simple_text_pdf"), tmp_path)
    assert refs == []


def test_tiny_decorative_images_skipped(make_pdf, tmp_path):
    _, refs, _ = _extract_all_images(make_pdf("tiny_image_pdf"), tmp_path)
    assert refs == []


def test_unsupported_colorspace_converted(tmp_path):
    """CMYK JPEGs render poorly in many readers; they are converted to RGB PNG."""
    cmyk = io.BytesIO()
    Image.new("CMYK", (120, 80), (0, 200, 200, 0)).save(cmyk, format="JPEG")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(50, 50, 170, 130), stream=cmyk.getvalue())
    path = tmp_path / "cmyk.pdf"
    doc.save(path)
    doc.close()
    _, refs, _ = _extract_all_images(path, tmp_path)
    assert len(refs) == 1 and refs[0].media_type == "image/png"
    with Image.open(tmp_path / "images" / refs[0].filename) as image:
        assert image.mode == "RGB"


def test_image_recompression(make_pdf, tmp_path):
    _, refs, _ = _extract_all_images(
        make_pdf("images_pdf"), tmp_path, ImageSettings(preserve_original=False, quality="low")
    )
    assert len(refs) == 4
    for ref in refs:
        assert max(ref.width, ref.height) <= 1000


# ----- OCR -------------------------------------------------------------------------------


def test_ocr_language_resolution():
    status = OcrStatus(True, languages=("eng",))
    assert resolve_ocr_language("en", status) == ("eng", None)
    lang, warning = resolve_ocr_language("ta", status)
    assert lang == "eng" and "not installed" in warning
    assert resolve_ocr_language("ta", OcrStatus(True, languages=("eng", "tam")))[0] == "tam+eng"


@requires_ocr
def test_ocr_reads_scanned_page(make_pdf):
    assert get_ocr_status(None).available
    with pymupdf.open(make_pdf("scanned_pdf")) as doc:
        blocks = ocr_page(doc[0], 0, dpi=200, lang="eng", timeout=60)
    text = " ".join(b.text for b in blocks)
    assert "Scanned Document Title" in text
    assert "scanned from paper" in text
    assert all(b.source == "ocr" for b in blocks)


def test_title_page_headings_do_not_become_chapters():
    """Big title-page lines followed by one long body must not yield a split with one giant chapter."""
    from app.services.content_processor import StructureBuilder

    body = [Paragraph([TextRun(f"Body sentence number {i}. " * 20)], page=1 + i // 10) for i in range(60)]
    elements = [
        PageMarker(1, 0),
        Heading(1, [TextRun("THE OLD MAN")], 0),
        Heading(1, [TextRun("AND THE SEA")], 0),
        Heading(1, [TextRun("By An Author")], 0),
        *body,
    ]
    builder = StructureBuilder([], [], StructureOptions())
    chapters, source = builder.segment(elements, "The Old Man and the Sea")
    assert source == "page-ranges"
    assert [n.title for n in chapters[0].toc_children] == ["THE OLD MAN", "AND THE SEA", "By An Author"]

    # Headings-only chapters are merged forward when a split is accepted.
    elements = [
        Heading(1, [TextRun("Title")], 0),
        Heading(1, [TextRun("Chapter A")], 0),
        Paragraph([TextRun("a " * 50)], 0),
        Heading(1, [TextRun("Chapter B")], 1),
        Paragraph([TextRun("b " * 50)], 1),
    ]
    chapters, source = StructureBuilder([], [], StructureOptions()).segment(elements, "Book")
    assert source == "headings"
    assert [c.title for c in chapters] == ["Title", "Chapter B"]
    assert [n.title for n in chapters[0].toc_children] == ["Chapter A"]
