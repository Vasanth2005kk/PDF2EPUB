"""EPUB 3 packaging and validation."""

from __future__ import annotations

import io
import posixpath
import zipfile
from pathlib import Path

import pytest
from ebooklib import epub
from lxml import etree
from PIL import Image

from app.services.epub_generator import generate_epub
from app.services.epub_validator import run_epubcheck, structural_check
from app.services.models import (
    Book,
    BookMetadata,
    Chapter,
    Figure,
    Heading,
    ImageRef,
    ListBlock,
    PageBreakRun,
    PageMarker,
    Paragraph,
    Table,
    TextRun,
    TocNode,
)
from tests.conftest import EPUBCHECK_PATH, requires_epubcheck

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "x": "http://www.w3.org/1999/xhtml",
}


def _image(directory: Path) -> ImageRef:
    directory.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buffer, format="PNG")
    (directory / "img_0001.png").write_bytes(buffer.getvalue())
    return ImageRef("img_0001.png", "image/png", 64, 48, page=1, bbox=(0, 0, 64, 48))


def _sample_book(image: ImageRef) -> Book:
    h1 = Heading(1, [TextRun("Chapter One")], page=0, anchor="h1")
    h2 = Heading(2, [TextRun("Section <A> & B")], page=0, anchor="h2")
    h3 = Heading(1, [TextRun("Chapter Two")], page=1, anchor="h3")
    chapter_one = Chapter(
        title="Chapter One",
        anchor="h1",
        elements=[
            PageMarker(1, 0),
            h1,
            Paragraph([TextRun("Plain "), TextRun("bold", bold=True), TextRun(" and "), TextRun("it", italic=True)], 0),
            h2,
            Paragraph([TextRun("<script>alert(1)</script> stays text")], 0),
            ListBlock(False, [[TextRun("one")], [TextRun("two")]], 0),
            ListBlock(True, [[TextRun("three")], [TextRun("four")]], 0, start=3),
            Paragraph([TextRun("Across "), PageBreakRun("", page_number=2), TextRun("pages")], 0),
        ],
        toc_children=[TocNode("Section <A> & B", "h2", 2)],
    )
    chapter_two = Chapter(
        title="Chapter Two",
        anchor="h3",
        elements=[
            h3,
            Figure(image, 1),
            Table([["Name", "Qty"], ["Apples", "3"]], 1),
            Paragraph([TextRun("தமிழ் உரை")], 1),
        ],
    )
    meta = BookMetadata(
        title="Sample Book",
        language="en",
        identifier="urn:uuid:12345678-1234-5678-1234-567812345678",
        authors=["Ann Author"],
        publisher="Test Press",
        description="A description",
        subjects=["testing"],
        date="2024-01-31",
    )
    return Book(metadata=meta, chapters=[chapter_one, chapter_two])


@pytest.fixture
def generated(tmp_path: Path):
    image_dir = tmp_path / "images"
    book = _sample_book(_image(image_dir))
    cover = io.BytesIO()
    Image.new("RGB", (300, 400), (240, 240, 240)).save(cover, format="JPEG")
    output = tmp_path / "book.epub"
    result = generate_epub(book, output, image_dir, generate_toc=True, cover_image=cover.getvalue())
    return output, result


def _opf(archive: zipfile.ZipFile) -> tuple[etree._Element, str]:
    container = etree.fromstring(archive.read("META-INF/container.xml"))
    path = container.find(".//{*}rootfile").get("full-path")
    return etree.fromstring(archive.read(path)), posixpath.dirname(path)


def test_valid_archive_layout(generated):
    output, result = generated
    with zipfile.ZipFile(output) as archive:
        first = archive.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"
        assert "META-INF/container.xml" in archive.namelist()
        assert archive.testzip() is None
    assert result.chapter_count == 2
    assert result.toc_entries == 3


def test_epub3_metadata(generated):
    output, _ = generated
    with zipfile.ZipFile(output) as archive:
        opf, _ = _opf(archive)
    assert opf.get("version") == "3.0"
    meta = opf.find("opf:metadata", NS)
    assert meta.findtext("dc:title", namespaces=NS) == "Sample Book"
    assert meta.findtext("dc:language", namespaces=NS) == "en"
    assert meta.findtext("dc:creator", namespaces=NS) == "Ann Author"
    assert meta.findtext("dc:publisher", namespaces=NS) == "Test Press"
    assert meta.findtext("dc:description", namespaces=NS) == "A description"
    identifier = meta.find(f"dc:identifier[@id='{opf.get('unique-identifier')}']", NS)
    assert identifier.text == "urn:uuid:12345678-1234-5678-1234-567812345678"
    assert meta.find("opf:meta[@property='dcterms:modified']", NS) is not None


def test_navigation_links_resolve(generated):
    output, _ = generated
    with zipfile.ZipFile(output) as archive:
        opf, opf_dir = _opf(archive)
        nav_item = opf.find("opf:manifest/opf:item[@properties='nav']", NS)
        nav_path = posixpath.join(opf_dir, nav_item.get("href"))
        nav = etree.fromstring(archive.read(nav_path))
        links = [(a.text, a.get("href")) for a in nav.iter("{http://www.w3.org/1999/xhtml}a")]
        assert [text for text, _ in links] == ["Chapter One", "Section <A> & B", "Chapter Two"]
        # Nested entry is inside the first chapter's list item.
        nested = nav.find(".//x:ol/x:li/x:ol/x:li/x:a", NS)
        assert nested is not None and nested.text == "Section <A> & B"
        for _, href in links:
            path, _, fragment = href.partition("#")
            document = etree.fromstring(archive.read(posixpath.join(posixpath.dirname(nav_path), path)))
            assert document.find(f".//*[@id='{fragment}']") is not None


def test_xhtml_content_images_and_css(generated):
    output, _ = generated
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        chapter_one = etree.fromstring(archive.read("EPUB/text/chapter_001.xhtml"))
        chapter_two = etree.fromstring(archive.read("EPUB/text/chapter_002.xhtml"))
    body_one = etree.tostring(chapter_one, encoding="unicode")
    assert "<strong>bold</strong>" in body_one and "<em>it</em>" in body_one
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body_one  # escaped, never markup
    assert chapter_one.find(".//x:script", NS) is None
    assert chapter_one.find(".//x:h1", NS).text == "Chapter One"
    assert chapter_one.find(".//x:h2", NS).get("id") == "h2"
    assert chapter_one.find(".//x:ol", NS).get("start") == "3"
    assert chapter_one.find(".//x:span[@id='page-2']", NS) is not None

    img = chapter_two.find(".//x:img", NS)
    assert img.get("alt")
    assert posixpath.normpath(posixpath.join("EPUB/text", img.get("src"))) in names
    assert chapter_two.find(".//x:table//x:td", NS).text == "Name"
    assert "தமிழ் உரை" in etree.tostring(chapter_two, encoding="unicode")
    css = chapter_two.find(".//x:link[@rel='stylesheet']", NS).get("href")
    assert posixpath.normpath(posixpath.join("EPUB/text", css)) in names


def test_readable_by_ebooklib(generated):
    output, _ = generated
    book = epub.read_epub(str(output))
    assert book.get_metadata("DC", "title")[0][0] == "Sample Book"
    documents = [item.get_name() for item in book.get_items_of_type(9)]  # ebooklib.ITEM_DOCUMENT
    assert "text/chapter_001.xhtml" in documents


def test_structural_validation_passes(generated):
    output, _ = generated
    report = structural_check(output)
    assert report.structural_valid, report.structural_errors
    assert report.stats["images"] == 2  # content image + cover
    assert "EPUBCheck not installed" in report.summary or report.epubcheck_ran is False


def test_structural_validation_detects_broken_epub(generated, tmp_path):
    output, _ = generated
    broken = tmp_path / "broken.epub"
    with zipfile.ZipFile(output) as source, zipfile.ZipFile(broken, "w") as target:
        for info in source.infolist():
            if info.filename.endswith("img_0001.png"):
                continue  # missing image
            data = source.read(info)
            if info.filename.endswith("chapter_001.xhtml"):
                data = data.replace(b"</body>", b"<a href='chapter_002.xhtml#nope'>x</a></body>")
            target.writestr(info, data)
    report = structural_check(broken)
    assert not report.structural_valid
    joined = " ".join(report.structural_errors)
    assert "img_0001.png" in joined
    assert "missing anchor" in joined


def test_large_chapter_split_into_documents(tmp_path):
    paragraphs = [Paragraph([TextRun(f"Paragraph {i} " + "word " * 400)], 0) for i in range(120)]
    heading = Heading(2, [TextRun("Late section")], 0, anchor="h2")
    chapter = Chapter("Long", [*paragraphs, heading, Paragraph([TextRun("end")], 0)], toc_children=[])
    chapter.toc_children = [TocNode("Late section", "h2", 2)]
    book = Book(BookMetadata("Long Book", "en", "urn:uuid:1"), [chapter])
    output = tmp_path / "long.epub"
    result = generate_epub(book, output, tmp_path, generate_toc=True)
    assert result.document_count > 1
    report = structural_check(output)
    assert report.structural_valid, report.structural_errors  # TOC anchor points into the right file


@requires_epubcheck
def test_epubcheck_accepts_generated_epub(generated):
    output, _ = generated
    report = structural_check(output)
    run_epubcheck(output, EPUBCHECK_PATH, 180, report)
    assert report.epubcheck_ran
    assert report.epubcheck_valid, report.epubcheck_messages
