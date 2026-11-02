"""Intermediate document model shared by the extraction, structuring and EPUB stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

BBox = tuple[float, float, float, float]


@dataclass
class TextRun:
    """A piece of text with uniform inline style."""

    text: str
    bold: bool = False
    italic: bool = False
    superscript: bool = False
    monospace: bool = False


@dataclass
class TextLine:
    runs: list[TextRun]
    bbox: BBox
    size: float  # dominant font size in points (character-weighted)
    bold: bool  # majority of characters are bold
    italic: bool
    font: str = ""

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)


@dataclass
class TextBlock:
    lines: list[TextLine]
    bbox: BBox
    page: int  # 0-based page index
    source: Literal["text", "ocr"] = "text"

    @property
    def text(self) -> str:
        return " ".join(line.text.strip() for line in self.lines)


@dataclass
class ImageRef:
    """An extracted image stored on disk in the job's work directory."""

    filename: str  # basename inside the images work directory
    media_type: str
    width: int
    height: int
    page: int
    bbox: BBox
    digest: str = ""
    is_page_scan: bool = False  # whole page rendered because its text could not be recovered


@dataclass
class TableData:
    rows: list[list[str]]
    bbox: BBox
    page: int


@dataclass
class PageContent:
    index: int  # 0-based
    width: float
    height: float
    blocks: list[TextBlock] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)
    tables: list[TableData] = field(default_factory=list)
    char_count: int = 0
    image_coverage: float = 0.0  # fraction of the page area covered by images
    needs_ocr: bool = False
    ocr_applied: bool = False


@dataclass
class OutlineEntry:
    level: int
    title: str
    page: int  # 0-based page index


# ----- structured (reconstructed) document ------------------------------------------------


@dataclass
class Heading:
    level: int  # 1 = top level
    runs: list[TextRun]
    page: int
    anchor: str = ""
    from_outline: bool = False

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs).strip()


@dataclass
class PageBreakRun(TextRun):
    """Inline marker for a print page boundary that falls inside a paragraph."""

    page_number: int = 0


@dataclass
class Paragraph:
    runs: list[TextRun]
    page: int

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs).strip()


@dataclass
class ListBlock:
    ordered: bool
    items: list[list[TextRun]]
    page: int
    start: int = 1


@dataclass
class Figure:
    image: ImageRef
    page: int


@dataclass
class Table:
    rows: list[list[str]]
    page: int


@dataclass
class PageMarker:
    """Start of a print page (1-based number), rendered as an invisible page-break anchor."""

    page_number: int
    page: int = 0


Element = Heading | Paragraph | ListBlock | Figure | Table | PageMarker


@dataclass
class TocNode:
    title: str
    anchor: str
    level: int
    children: list[TocNode] = field(default_factory=list)


@dataclass
class Chapter:
    title: str
    elements: list[Element]
    anchor: str = ""  # anchor of the chapter heading, if the chapter starts with one
    toc_children: list[TocNode] = field(default_factory=list)
    heading_level: int = 1  # structural level that maps to <h1> inside this chapter
    first_page: int = 0
    last_page: int = 0


@dataclass
class BookMetadata:
    title: str
    language: str
    identifier: str
    authors: list[str] = field(default_factory=list)
    publisher: str = ""
    description: str = ""
    subjects: list[str] = field(default_factory=list)
    date: str = ""


@dataclass
class Book:
    metadata: BookMetadata
    chapters: list[Chapter]
    cover_image: ImageRef | None = None
    structure_source: str = ""  # "outline", "headings" or "page-ranges"
