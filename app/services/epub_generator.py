"""EPUB 3 packaging with EbookLib."""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from ebooklib import epub

from app.exceptions import ConversionError
from app.services.models import (
    Book,
    Chapter,
    Element,
    Figure,
    Heading,
    ListBlock,
    PageBreakRun,
    PageMarker,
    Paragraph,
    Table,
    TextRun,
    TocNode,
)

logger = logging.getLogger(__name__)

TEXT_DIR = "text"
IMAGE_DIR = "images"
STYLE_PATH = "style/book.css"
# Split very long chapters into several XHTML files; some readers slow down on huge documents.
MAX_DOCUMENT_CHARS = 150_000
RTL_LANGUAGES = {"ar", "he", "fa", "ur", "yi", "ps", "sd", "ug"}

BOOK_CSS = """\
@charset "utf-8";
html { font-size: 100%; }
body { margin: 0 4%; line-height: 1.5; text-align: left; widows: 2; orphans: 2; }
h1, h2, h3, h4, h5, h6 { line-height: 1.25; page-break-after: avoid; break-after: avoid; text-align: left; }
h1 { font-size: 1.6em; margin: 1.2em 0 0.8em; }
h2 { font-size: 1.35em; margin: 1.2em 0 0.6em; }
h3 { font-size: 1.15em; margin: 1em 0 0.5em; }
h4, h5, h6 { font-size: 1em; margin: 1em 0 0.4em; }
p { margin: 0 0 0.75em; }
ul, ol { margin: 0 0 0.75em; padding-left: 1.6em; }
li { margin: 0 0 0.3em; }
figure { margin: 1em 0; text-align: center; page-break-inside: avoid; break-inside: avoid; }
figure img { max-width: 100%; height: auto; }
figure.page-scan img { width: 100%; }
.table-wrap { overflow-x: auto; margin: 1em 0; }
table { border-collapse: collapse; width: 100%; font-size: 0.9em; }
td, th { border: 1px solid #888; padding: 0.25em 0.4em; vertical-align: top; text-align: left; }
code { font-family: monospace; }
span.pagebreak { display: none; }
"""


@dataclass
class GenerationResult:
    path: Path
    document_count: int
    image_count: int
    chapter_count: int
    toc_entries: int
    warnings: list[str] = field(default_factory=list)


class FileBackedImage(epub.EpubImage):
    """EPUB image whose bytes are read from disk only when the archive is written."""

    def __init__(self, uid: str, file_name: str, media_type: str, source: Path) -> None:
        super().__init__(uid=uid, file_name=file_name, media_type=media_type)
        self._source = source

    def get_content(self, default: bytes | None = None) -> bytes:  # type: ignore[override]
        return self._source.read_bytes()


# ----- XHTML rendering ---------------------------------------------------------------------


def _attr(value: str) -> str:
    return escape(value, quote=True)


def render_runs(runs: list[TextRun]) -> str:
    parts: list[str] = []
    for run in runs:
        if isinstance(run, PageBreakRun):
            parts.append(_page_break(run.page_number))
            continue
        if not run.text:
            continue
        html = escape(run.text, quote=False)
        if run.monospace:
            html = f"<code>{html}</code>"
        if run.superscript:
            html = f"<sup>{html}</sup>"
        if run.italic:
            html = f"<em>{html}</em>"
        if run.bold:
            html = f"<strong>{html}</strong>"
        parts.append(html)
    return "".join(parts)


def _page_break(page_number: int) -> str:
    return (
        f'<span class="pagebreak" id="page-{page_number}" role="doc-pagebreak" aria-label="Page {page_number}"></span>'
    )


class _Renderer:
    def __init__(self, image_paths: dict[str, str]) -> None:
        self.image_paths = image_paths  # image filename -> href relative to the text directory

    def element(self, element: Element, heading_level: int) -> str:
        if isinstance(element, Heading):
            depth = max(1, min(6, 1 + element.level - heading_level))
            return f'<h{depth} id="{_attr(element.anchor)}">{render_runs(element.runs)}</h{depth}>'
        if isinstance(element, Paragraph):
            content = render_runs(element.runs)
            return f"<p>{content}</p>" if content else ""
        if isinstance(element, ListBlock):
            items = "".join(f"<li>{render_runs(item)}</li>" for item in element.items)
            if element.ordered:
                start = f' start="{element.start}"' if element.start != 1 else ""
                return f"<ol{start}>{items}</ol>"
            return f"<ul>{items}</ul>"
        if isinstance(element, Figure):
            href = self.image_paths.get(element.image.filename)
            if not href:
                return ""
            css = ' class="page-scan"' if element.image.is_page_scan else ""
            alt = f"Image from page {element.page + 1}"
            return f'<figure{css}><img src="{_attr(href)}" alt="{_attr(alt)}"/></figure>'
        if isinstance(element, Table):
            width = max(len(row) for row in element.rows)
            rows = "".join(
                "<tr>"
                + "".join(f"<td>{escape(cell, quote=False)}</td>" for cell in row + [""] * (width - len(row)))
                + "</tr>"
                for row in element.rows
            )
            return f'<div class="table-wrap"><table><tbody>{rows}</tbody></table></div>'
        if isinstance(element, PageMarker):
            return _page_break(element.page_number)
        return ""


# ----- packaging ---------------------------------------------------------------------------


@dataclass
class _Document:
    item: epub.EpubHtml
    anchors: set[str]


def _split_chapter(chapter: Chapter, renderer: _Renderer) -> list[tuple[str, set[str]]]:
    """Render a chapter into one or more XHTML bodies, splitting only between elements."""
    parts: list[tuple[str, set[str]]] = []
    buffer: list[str] = []
    anchors: set[str] = set()
    size = 0
    for element in chapter.elements:
        html = renderer.element(element, chapter.heading_level)
        if not html:
            continue
        if size + len(html) > MAX_DOCUMENT_CHARS and size > 0 and not isinstance(element, PageMarker):
            parts.append(("\n".join(buffer), anchors))
            buffer, anchors, size = [], set(), 0
        buffer.append(html)
        size += len(html)
        if isinstance(element, Heading):
            anchors.add(element.anchor)
    if buffer:
        parts.append(("\n".join(buffer), anchors))
    return parts


def _has_visible_content(html: str) -> bool:
    return any(tag in html for tag in ("<p", "<h", "<ul", "<ol", "<figure", "<table"))


def generate_epub(
    book: Book,
    output_path: Path,
    image_dir: Path,
    *,
    generate_toc: bool = True,
    cover_image: bytes | None = None,
    progress: callable | None = None,  # type: ignore[valid-type]
) -> GenerationResult:
    """Package ``book`` as an EPUB 3 file at ``output_path`` (written atomically)."""
    meta = book.metadata
    language = meta.language
    direction = "rtl" if language.split("-")[0].lower() in RTL_LANGUAGES else None

    ebook = epub.EpubBook()
    ebook.set_identifier(meta.identifier)
    ebook.set_title(meta.title)
    ebook.set_language(language)
    if direction:
        ebook.set_direction(direction)
    for author in meta.authors:
        ebook.add_author(author)
    if meta.publisher:
        ebook.add_metadata("DC", "publisher", meta.publisher)
    if meta.description:
        ebook.add_metadata("DC", "description", meta.description)
    for subject in meta.subjects:
        ebook.add_metadata("DC", "subject", subject)
    if meta.date:
        ebook.add_metadata("DC", "date", meta.date)

    style = epub.EpubItem(uid="style", file_name=STYLE_PATH, media_type="text/css", content=BOOK_CSS.encode())
    ebook.add_item(style)

    if cover_image:
        ebook.set_cover(f"{IMAGE_DIR}/cover.jpg", cover_image, create_page=True)
        # A non-linear cover page must be linked from somewhere (EPUBCheck OPF-096); keep it linear.
        ebook.get_item_with_id("cover").is_linear = True

    # Images actually referenced by the content.
    used_images: dict[str, tuple[str, str]] = {}
    for chapter in book.chapters:
        for element in chapter.elements:
            if isinstance(element, Figure):
                used_images[element.image.filename] = (element.image.media_type, element.image.filename)
    image_paths: dict[str, str] = {}
    for index, (filename, (media_type, _)) in enumerate(sorted(used_images.items()), start=1):
        source = image_dir / filename
        if not source.is_file():
            logger.warning("Extracted image %s missing; skipping", filename)
            continue
        ebook.add_item(FileBackedImage(f"image_{index:04d}", f"{IMAGE_DIR}/{filename}", media_type, source))
        image_paths[filename] = f"../{IMAGE_DIR}/{filename}"

    renderer = _Renderer(image_paths)
    documents: list[_Document] = []
    chapter_docs: list[list[_Document]] = []
    total = len(book.chapters)
    for number, chapter in enumerate(book.chapters, start=1):
        docs_for_chapter: list[_Document] = []
        parts = _split_chapter(chapter, renderer)
        for part_number, (body, anchors) in enumerate(parts, start=1):
            suffix = "" if part_number == 1 else f"_{part_number:02d}"
            file_name = f"{TEXT_DIR}/chapter_{number:03d}{suffix}.xhtml"
            item = epub.EpubHtml(
                uid=f"chapter_{number:03d}{suffix}",
                file_name=file_name,
                title=chapter.title if part_number == 1 else f"{chapter.title} ({part_number})",
                lang=language,
                direction=direction,
            )
            item.content = f"<html><body>{body}</body></html>"
            item.add_link(href=f"../{STYLE_PATH}", rel="stylesheet", type="text/css")
            ebook.add_item(item)
            doc = _Document(item, anchors)
            documents.append(doc)
            docs_for_chapter.append(doc)
        chapter_docs.append(docs_for_chapter)
        if progress:
            progress(number, total)

    if not documents or not any(_has_visible_content(d.item.content) for d in documents):
        raise ConversionError("The document produced no readable EPUB content.")

    def href_for(anchor: str, docs: list[_Document]) -> str:
        for doc in docs:
            if anchor in doc.anchors:
                return f"{doc.item.file_name}#{anchor}"
        return docs[0].item.file_name

    def toc_tree(nodes: list[TocNode], docs: list[_Document]) -> list:
        entries: list = []
        for node in nodes:
            link = epub.Link(href_for(node.anchor, docs), node.title, node.anchor)
            entries.append((link, toc_tree(node.children, docs)) if node.children else link)
        return entries

    toc: list = []
    toc_entries = 0
    for chapter, docs in zip(book.chapters, chapter_docs, strict=True):
        if not docs:
            continue
        href = href_for(chapter.anchor, docs) if chapter.anchor else docs[0].item.file_name
        link = epub.Link(href, chapter.title, chapter.anchor or docs[0].item.id)
        toc_entries += 1
        children = toc_tree(chapter.toc_children, docs) if generate_toc else []
        toc_entries += _count_nodes(chapter.toc_children) if generate_toc else 0
        toc.append((link, children) if children else link)
    ebook.toc = toc

    nav = epub.EpubNav(title="Contents")
    nav.add_link(href=STYLE_PATH, rel="stylesheet", type="text/css")
    ebook.add_item(nav)
    ebook.add_item(epub.EpubNcx())

    spine: list = []
    if cover_image:
        spine.append(("cover", "yes"))
    if generate_toc:
        spine.append("nav")
    spine.extend(doc.item for doc in documents)
    ebook.spine = spine

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=output_path.parent, prefix=".epub-", suffix=".tmp")
    os.close(fd)
    try:
        epub.write_epub(tmp_name, ebook, {"raise_exceptions": True, "epub3_pages": False})
        os.replace(tmp_name, output_path)
    except OSError as exc:
        _unlink_quietly(tmp_name)
        raise ConversionError("The EPUB file could not be written (server storage error).") from exc
    except Exception as exc:
        _unlink_quietly(tmp_name)
        logger.exception("EbookLib failed to write the EPUB")
        raise ConversionError("EPUB generation failed.") from exc

    return GenerationResult(
        path=output_path,
        document_count=len(documents),
        image_count=len(image_paths) + (1 if cover_image else 0),
        chapter_count=len(book.chapters),
        toc_entries=toc_entries,
    )


def _count_nodes(nodes: list[TocNode]) -> int:
    return sum(1 + _count_nodes(node.children) for node in nodes)


def _unlink_quietly(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)
