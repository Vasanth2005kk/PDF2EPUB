"""Reconstruct a logical eBook structure from extracted PDF page content.

Pipeline:
    1. Remove running headers, footers and page numbers (text repeated in page margins).
    2. Order blocks, images and tables per page in reading order (column-aware).
    3. Split blocks into paragraphs and join lines (de-hyphenation).
    4. Classify paragraphs as headings (font size ratio, weight, patterns, PDF outline) or list items.
    5. Merge paragraphs that continue across page/column breaks.
    6. Segment into chapters at the most significant heading level that occurs repeatedly,
       falling back to page-range sections when no reliable headings exist.

The text itself is never rewritten or summarised; only whitespace, hyphenation at line ends
and layout artefacts (running headers/page numbers) are changed.
"""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from statistics import median

from app.exceptions import ConversionError
from app.services.models import (
    Book,
    BookMetadata,
    Chapter,
    Element,
    Figure,
    Heading,
    ImageRef,
    ListBlock,
    OutlineEntry,
    PageBreakRun,
    PageContent,
    PageMarker,
    Paragraph,
    Table,
    TableData,
    TextBlock,
    TextLine,
    TextRun,
    TocNode,
)

# ----- patterns ------------------------------------------------------------------------------

PAGE_NUMBER_RE = re.compile(
    r"^[\-–—\s]*(page\s+)?(\d{1,4}|[ivxlcdm]{1,7})(\s*(of|/)\s*\d{1,4})?[\-–—\s]*$", re.IGNORECASE
)
CHAPTER_RE = re.compile(
    r"^(chapter|part|book|prologue|epilogue|appendix|அத்தியாயம்|பாகம்|அதிகாரம்)\b[\s\dIVXLCDM.:\-–—]*",
    re.IGNORECASE,
)
NUMBERED_SECTION_RE = re.compile(r"^\d{1,2}(\.\d{1,2}){0,3}\.?\s+\S")
BULLET_RE = re.compile(r"^\s*([•◦▪▫●○■□►▸‣⁃∙·*]|[\-–])\s+")
ORDERED_RE = re.compile(r"^\s*(\d{1,3})[.)]\s+")
TERMINAL_PUNCT = tuple('.!?:;"”’»)]…।')

MARGIN_FRACTION = 0.1  # top/bottom share of the page treated as header/footer zone
HEADING_SIZE_RATIO = 1.18  # minimum size relative to body text for a size-based heading
OCR_HEADING_SIZE_RATIO = 1.5  # OCR sizes come from word-box heights and are much noisier
MAX_CHAPTER_SHARE = 0.75  # a split is rejected if one chapter would hold more of the text than this
BALANCE_MIN_CHARS = 15_000  # shorter documents are not checked for balance
MAX_HEADING_CHARS = 160
SECTION_PAGES = 10  # pages per section when no headings are available


@dataclass
class StructureOptions:
    detect_headings: bool = True
    remove_headers_footers: bool = True


# ----- working types -------------------------------------------------------------------------


@dataclass
class _Para:
    lines: list[TextLine]
    page: int
    source: str
    size: float
    bold: bool
    standalone: bool  # the block contained only this paragraph
    runs: list[TextRun] = field(default_factory=list)
    ratio: float = 1.0
    kind: str = "para"  # para | heading | list
    level: int = 0
    from_outline: bool = False
    list_ordered: bool = False
    list_number: int = 0
    list_dash: bool = False
    item_runs: list[TextRun] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs).strip()


@dataclass
class _OutlineHeading:
    """An outline (bookmark) entry not yet matched to a paragraph on its page."""

    entry: OutlineEntry


_Item = _Para | ImageRef | TableData | PageMarker | _OutlineHeading


# ----- helpers -------------------------------------------------------------------------------


def _normalize_key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"\d+", "#", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_title(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\W_]+", " ", text).strip()


def _bbox_of(item: TextBlock | ImageRef | TableData) -> tuple[float, float, float, float]:
    return item.bbox


def _is_cased_lower(ch: str) -> bool:
    return ch.isalpha() and ch.islower()


def _copy_runs(runs: list[TextRun]) -> list[TextRun]:
    return [TextRun(r.text, r.bold, r.italic, r.superscript, r.monospace) for r in runs]


def _merge_adjacent(runs: list[TextRun]) -> list[TextRun]:
    merged: list[TextRun] = []
    for run in runs:
        if not run.text and not isinstance(run, PageBreakRun):
            continue
        if (
            merged
            and not isinstance(run, PageBreakRun)
            and not isinstance(merged[-1], PageBreakRun)
            and (merged[-1].bold, merged[-1].italic, merged[-1].superscript, merged[-1].monospace)
            == (run.bold, run.italic, run.superscript, run.monospace)
        ):
            merged[-1].text += run.text
        else:
            merged.append(run)
    return merged


def _strip_runs(runs: list[TextRun]) -> list[TextRun]:
    runs = [r for r in runs if r.text or isinstance(r, PageBreakRun)]
    if runs and not isinstance(runs[0], PageBreakRun):
        runs[0].text = runs[0].text.lstrip()
    if runs and not isinstance(runs[-1], PageBreakRun):
        runs[-1].text = runs[-1].text.rstrip()
    return [r for r in runs if r.text or isinstance(r, PageBreakRun)]


def _append_with_join(target: list[TextRun], addition: list[TextRun]) -> None:
    """Append ``addition`` to ``target`` as a continuation, de-hyphenating at the join."""
    addition = _strip_runs(_copy_runs(addition))
    if not addition:
        return
    last = next((r for r in reversed(target) if not isinstance(r, PageBreakRun) and r.text), None)
    first_text = next((r.text for r in addition if not isinstance(r, PageBreakRun) and r.text), "")
    if last is not None:
        last.text = last.text.rstrip(" ")
        if last.text.endswith("­") or (
            last.text.endswith("-")
            and len(last.text) >= 2
            and last.text[-2].isalpha()
            and first_text[:1]
            and _is_cased_lower(first_text[0])
        ):
            last.text = last.text[:-1]
        else:
            last.text += " "
    target.extend(addition)


def _lines_to_runs(lines: list[TextLine]) -> list[TextRun]:
    runs: list[TextRun] = []
    for line in lines:
        _append_with_join(runs, line.runs)
    return _merge_adjacent(_strip_runs(runs))


def _dominant(lines: list[TextLine]) -> tuple[float, bool]:
    weights: Counter[float] = Counter()
    bold = 0
    total = 0
    for line in lines:
        n = len(line.text.strip())
        weights[line.size] += n
        total += n
        bold += n if line.bold else 0
    size = weights.most_common(1)[0][0] if weights else 0.0
    return size, total > 0 and bold >= total * 0.6


# ----- step 1: running headers / footers -----------------------------------------------------


def remove_running_artifacts(pages: list[PageContent]) -> None:
    """Drop text and images repeated in the page margins (running heads, footers, page numbers)."""
    text_pages = [p for p in pages if p.blocks]
    if not text_pages:
        return
    threshold = max(3, math.ceil(0.4 * len(text_pages)))

    def in_margin(bbox: tuple[float, float, float, float], page: PageContent) -> bool:
        return bbox[3] <= page.height * MARGIN_FRACTION or bbox[1] >= page.height * (1 - MARGIN_FRACTION)

    key_pages: dict[str, set[int]] = {}
    digest_pages: dict[str, set[int]] = {}
    for page in pages:
        for block in page.blocks:
            if len(block.lines) <= 2 and in_margin(block.bbox, page):
                key_pages.setdefault(_normalize_key(block.text), set()).add(page.index)
        for image in page.images:
            if in_margin(image.bbox, page):
                digest_pages.setdefault(image.digest, set()).add(page.index)

    repeated = {key for key, seen in key_pages.items() if len(seen) >= threshold and key}
    repeated_images = {digest for digest, seen in digest_pages.items() if len(seen) >= threshold}
    for page in pages:
        page.blocks = [
            block
            for block in page.blocks
            if not (
                len(block.lines) <= 2
                and in_margin(block.bbox, page)
                and (_normalize_key(block.text) in repeated or PAGE_NUMBER_RE.match(block.text.strip()))
            )
        ]
        page.images = [
            image for image in page.images if not (image.digest in repeated_images and in_margin(image.bbox, page))
        ]


# ----- step 2: reading order -----------------------------------------------------------------


def order_page_items(
    items: list[TextBlock | ImageRef | TableData], page_width: float
) -> list[TextBlock | ImageRef | TableData]:
    """Order page items for reading, handling simple two-column layouts.

    Items spanning the page centre ("full width") split the page into horizontal bands; within
    a band the left column is read before the right column.
    """
    if len(items) < 2:
        return list(items)
    mid = page_width / 2
    tolerance = page_width * 0.03

    def column(item: TextBlock | ImageRef | TableData) -> str:
        x0, _, x1, _ = _bbox_of(item)
        if x1 <= mid + tolerance:
            return "left"
        if x0 >= mid - tolerance:
            return "right"
        return "full"

    def weight(item: TextBlock | ImageRef | TableData) -> int:
        return len(item.text) if isinstance(item, TextBlock) else 50

    columns = {id(item): column(item) for item in items}
    left = [i for i in items if columns[id(i)] == "left"]
    right = [i for i in items if columns[id(i)] == "right"]
    total = sum(weight(i) for i in items) or 1
    two_columns = (
        len(left) >= 2 and len(right) >= 2 and (sum(map(weight, left)) + sum(map(weight, right))) / total >= 0.5
    )

    def by_position(seq: list) -> list:
        return sorted(seq, key=lambda i: (round(_bbox_of(i)[1], 0), _bbox_of(i)[0]))

    if not two_columns:
        return by_position(items)

    ordered: list[TextBlock | ImageRef | TableData] = []
    band: list[TextBlock | ImageRef | TableData] = []

    def flush() -> None:
        ordered.extend(by_position([i for i in band if columns[id(i)] == "left"]))
        ordered.extend(by_position([i for i in band if columns[id(i)] == "right"]))
        band.clear()

    for item in sorted(items, key=lambda i: _bbox_of(i)[1]):
        if columns[id(item)] == "full":
            flush()
            ordered.append(item)
        else:
            band.append(item)
    flush()
    return ordered


# ----- step 3: paragraphs --------------------------------------------------------------------


def split_block_into_paragraphs(block: TextBlock) -> list[list[TextLine]]:
    lines = block.lines
    if len(lines) <= 1:
        return [list(lines)]
    left = min(line.bbox[0] for line in lines)
    right = max(line.bbox[2] for line in lines)
    width = max(right - left, 1.0)
    gaps = [max(0.0, lines[i].bbox[1] - lines[i - 1].bbox[3]) for i in range(1, len(lines))]
    typical_gap = median(gaps) if gaps else 0.0

    paragraphs: list[list[TextLine]] = [[lines[0]]]
    for i in range(1, len(lines)):
        prev, line = lines[i - 1], lines[i]
        em = max(line.size, 1.0)
        prev_text = prev.text.rstrip()
        new_paragraph = False
        if (
            BULLET_RE.match(line.text)
            or ORDERED_RE.match(line.text)
            or gaps[i - 1] > typical_gap + 0.6 * em
            or abs(line.size - prev.size) > 0.15 * max(line.size, prev.size)
            or (prev.bold != line.bold and (prev.bold and len(prev_text) < MAX_HEADING_CHARS))
        ):
            new_paragraph = True
        elif line.bbox[0] - left > 0.8 * em and prev.bbox[0] - left < 0.4 * em and len(lines) >= 3:
            new_paragraph = True  # first-line indent
        elif prev_text.endswith(TERMINAL_PUNCT) and prev.bbox[2] < right - 0.15 * width and len(lines) >= 3:
            new_paragraph = True  # short last line of the previous paragraph
        if new_paragraph:
            paragraphs.append([line])
        else:
            paragraphs[-1].append(line)
    return paragraphs


# ----- the builder ---------------------------------------------------------------------------


class StructureBuilder:
    def __init__(
        self,
        pages: list[PageContent],
        outline: list[OutlineEntry],
        options: StructureOptions,
    ) -> None:
        self.pages = pages
        self.outline = [e for e in outline if e.title.strip() and 0 <= e.page < len(pages)]
        self.options = options
        self.use_outline = len(self.outline) >= 2
        self.detected_title: str = ""
        self._anchor_counter = 0

    def _anchor(self) -> str:
        self._anchor_counter += 1
        return f"h{self._anchor_counter}"

    # -- assemble the item stream ------------------------------------------------------------
    def _page_items(self) -> list[_Item]:
        stream: list[_Item] = []
        outline_by_page: dict[int, list[OutlineEntry]] = {}
        for entry in self.outline if self.use_outline else []:
            outline_by_page.setdefault(entry.page, []).append(entry)

        for page in self.pages:
            stream.append(PageMarker(page_number=page.index + 1, page=page.index))
            for entry in outline_by_page.get(page.index, []):
                stream.append(_OutlineHeading(entry))
            items: list[TextBlock | ImageRef | TableData] = [*page.blocks, *page.images, *page.tables]
            for item in order_page_items(items, page.width):
                if isinstance(item, TextBlock):
                    groups = split_block_into_paragraphs(item)
                    for lines in groups:
                        size, bold = _dominant(lines)
                        stream.append(
                            _Para(
                                lines=lines,
                                page=page.index,
                                source=item.source,
                                size=size,
                                bold=bold,
                                standalone=len(groups) == 1,
                                runs=_lines_to_runs(lines),
                            )
                        )
                else:
                    stream.append(item)
        return stream

    # -- classification ----------------------------------------------------------------------
    def _body_sizes(self, paras: list[_Para]) -> dict[str, float]:
        weights: dict[str, Counter[float]] = {}
        for para in paras:
            for line in para.lines:
                weights.setdefault(para.source, Counter())[round(line.size * 2) / 2] += len(line.text.strip())
        return {source: counter.most_common(1)[0][0] or 1.0 for source, counter in weights.items() if counter}

    def _heading_candidate(self, para: _Para) -> str | None:
        text = para.text
        if not text or len(text) > MAX_HEADING_CHARS or len(para.lines) > 3:
            return None
        if sum(ch.isalpha() for ch in text) < 2 or text.endswith((",", ";")):
            return None
        if PAGE_NUMBER_RE.match(text):
            return None
        visible = [ch for ch in text if not ch.isspace()]
        wordlike = sum(1 for ch in visible if ch.isalnum() or unicodedata.category(ch).startswith("M"))
        if wordlike < 3 or wordlike < 0.6 * len(visible):
            return None  # symbols, OCR noise
        if para.ratio >= (OCR_HEADING_SIZE_RATIO if para.source == "ocr" else HEADING_SIZE_RATIO):
            return "size"
        if CHAPTER_RE.match(text) and para.ratio >= 0.95 and (para.bold or len(text) <= 60) and para.standalone:
            return "chapter"
        if (
            para.bold
            and para.ratio >= 0.95
            and len(text) <= 100
            and not text.endswith(".")
            and para.standalone
            and not BULLET_RE.match(text)
        ):
            return "bold"
        return None

    def _classify(self, paras: list[_Para]) -> None:
        body = self._body_sizes(paras)
        for para in paras:
            para.ratio = para.size / body.get(para.source, para.size or 1.0) if para.size else 1.0

        reasons: dict[int, str] = {}
        if self.options.detect_headings:
            for para in paras:
                reason = self._heading_candidate(para)
                if reason:
                    reasons[id(para)] = reason
            # If "bold" headings are implausibly common the document simply uses bold body text.
            bold_count = sum(1 for r in reasons.values() if r == "bold")
            if bold_count > 8 and bold_count > 0.3 * len(paras):
                reasons = {k: v for k, v in reasons.items() if v != "bold"}

        # Map size ratios to levels: cluster distinct ratios, largest first.
        ratios = sorted({round(p.ratio, 2) for p in paras if reasons.get(id(p)) == "size"}, reverse=True)
        clusters: list[float] = []
        for ratio in ratios:
            if not clusters or clusters[-1] - ratio > 0.12:
                clusters.append(ratio)

        def size_level(ratio: float) -> int:
            for index, anchor in enumerate(clusters):
                if ratio >= anchor - 0.12:
                    return min(index + 1, 4)
            return min(len(clusters), 4) or 1

        bold_level = min(len(clusters) + 1, 5)
        for para in paras:
            reason = reasons.get(id(para))
            if reason == "size":
                para.kind, para.level = "heading", size_level(para.ratio)
            elif reason == "chapter":
                para.kind, para.level = "heading", 1
            elif reason == "bold":
                para.kind, para.level = "heading", bold_level
            else:
                self._classify_list(para)

        # Title candidate: the most prominent heading on the first page with text.
        first_page = min((p.page for p in paras), default=0)
        candidates = [p for p in paras if p.page == first_page and p.kind == "heading" and p.ratio >= 1.3]
        if candidates:
            self.detected_title = max(candidates, key=lambda p: p.ratio).text

    @staticmethod
    def _classify_list(para: _Para) -> None:
        text = para.text
        bullet = BULLET_RE.match(text)
        ordered = ORDERED_RE.match(text)
        if not bullet and not ordered:
            return
        match = bullet or ordered
        assert match is not None
        runs = _copy_runs(para.runs)
        # The runs are left-stripped, so the match offsets line up with the concatenated runs.
        remaining = match.end()
        for run in runs:
            if remaining <= 0:
                break
            cut = min(len(run.text), remaining)
            run.text = run.text[cut:]
            remaining -= cut
        para.kind = "list"
        para.item_runs = _strip_runs([r for r in runs if r.text])
        para.list_ordered = bool(ordered)
        para.list_number = int(ordered.group(1)) if ordered else 0
        para.list_dash = bool(bullet and bullet.group(1) in "-–")
        if not para.item_runs:
            para.kind = "para"

    def _apply_outline(self, stream: list[_Item]) -> None:
        """Match outline entries to paragraphs; unmatched entries become headings at page start."""
        if not self.use_outline:
            return
        max_level = max(e.level for e in self.outline)
        paras_by_page: dict[int, list[_Para]] = {}
        for item in stream:
            if isinstance(item, _Para):
                paras_by_page.setdefault(item.page, []).append(item)

        matched: set[int] = set()
        # Unmatched entries that follow a matched entry on the same page are moved after it.
        relocate: dict[int, list[_OutlineHeading]] = {}
        last_match: tuple[int, _Para] | None = None  # (page, paragraph)
        for index, item in enumerate(stream):
            if not isinstance(item, _OutlineHeading):
                continue
            entry = item.entry
            target = _normalize_title(entry.title)
            best: _Para | None = None
            best_score = 0.0
            for para in paras_by_page.get(entry.page, [])[:40]:
                if id(para) in matched or len(para.text) > MAX_HEADING_CHARS * 2:
                    continue
                candidate = _normalize_title(para.text)
                if not candidate:
                    continue
                if candidate == target:
                    score = 1.0
                elif len(target) >= 4 and (candidate.startswith(target) or target.startswith(candidate)):
                    score = 0.9 if min(len(candidate), len(target)) >= 4 else 0.0
                else:
                    score = difflib.SequenceMatcher(None, candidate, target).ratio()
                if score > best_score:
                    best, best_score = para, score
            if best is not None and best_score >= 0.85:
                matched.add(id(best))
                best.kind, best.level, best.from_outline = "heading", entry.level, True
                stream[index] = PageMarker(page_number=-1)  # placeholder, removed below
                last_match = (entry.page, best)
            elif last_match is not None and last_match[0] == entry.page:
                relocate.setdefault(id(last_match[1]), []).append(item)
                stream[index] = PageMarker(page_number=-1)
            # Other unmatched entries stay at the top of their page, rendered from the bookmark title.
        if relocate:
            rebuilt: list[_Item] = []
            for item in stream:
                rebuilt.append(item)
                rebuilt.extend(relocate.get(id(item), []))
            stream[:] = rebuilt

        # Font-detected headings that are not in the outline sit below the outline levels.
        for item in stream:
            if isinstance(item, _Para) and item.kind == "heading" and not item.from_outline:
                item.level = min(max_level + 1, 6)
        stream[:] = [i for i in stream if not (isinstance(i, PageMarker) and i.page_number == -1)]

    # -- element stream ----------------------------------------------------------------------
    def _to_elements(self, stream: list[_Item]) -> list[Element]:
        elements: list[Element] = []
        pending_list: list[_Para] = []

        def flush_list() -> None:
            if not pending_list:
                return
            groups: list[list[_Para]] = []
            for para in pending_list:
                if (
                    groups
                    and groups[-1][-1].list_ordered == para.list_ordered
                    and (not para.list_ordered or para.list_number == groups[-1][-1].list_number + 1)
                ):
                    groups[-1].append(para)
                else:
                    groups.append([para])
            for group in groups:
                if len(group) == 1 and group[0].list_dash:
                    elements.append(Paragraph(runs=group[0].runs, page=group[0].page))
                    continue
                elements.append(
                    ListBlock(
                        ordered=group[0].list_ordered,
                        items=[p.item_runs for p in group],
                        page=group[0].page,
                        start=group[0].list_number if group[0].list_ordered else 1,
                    )
                )
            pending_list.clear()

        for item in stream:
            if isinstance(item, _Para) and item.kind == "list":
                pending_list.append(item)
                continue
            flush_list()
            if isinstance(item, PageMarker):
                elements.append(item)
            elif isinstance(item, _OutlineHeading):
                elements.append(
                    Heading(
                        level=item.entry.level,
                        runs=[TextRun(item.entry.title.strip())],
                        page=item.entry.page,
                        from_outline=True,
                    )
                )
            elif isinstance(item, ImageRef):
                elements.append(Figure(image=item, page=item.page))
            elif isinstance(item, TableData):
                elements.append(Table(rows=item.rows, page=item.page))
            elif isinstance(item, _Para):
                if item.kind == "heading":
                    runs = [TextRun(r.text, False, r.italic, r.superscript, r.monospace) for r in item.runs]
                    elements.append(
                        Heading(
                            level=item.level,
                            runs=_merge_adjacent(runs),
                            page=item.page,
                            from_outline=item.from_outline,
                        )
                    )
                elif not self._continue_previous(elements, item):
                    elements.append(Paragraph(runs=item.runs, page=item.page))
        flush_list()
        return elements

    @staticmethod
    def _continue_previous(elements: list[Element], para: _Para) -> bool:
        """Merge ``para`` into the previous paragraph if it continues across a page/column break."""
        markers: list[PageMarker] = []
        index = len(elements) - 1
        while index >= 0 and isinstance(elements[index], PageMarker):
            markers.append(elements[index])  # type: ignore[arg-type]
            index -= 1
        if index < 0 or not isinstance(elements[index], Paragraph):
            return False
        previous: Paragraph = elements[index]  # type: ignore[assignment]
        prev_text = previous.text
        text = para.text
        if not prev_text or not text:
            return False
        hyphenated = prev_text.endswith(("-", "­"))
        if prev_text.endswith(TERMINAL_PUNCT) and not hyphenated:
            return False
        if not _is_cased_lower(text[0]):
            return False
        runs = list(previous.runs)
        for marker in reversed(markers):
            runs.append(PageBreakRun(text="", page_number=marker.page_number))
        _append_with_join(runs, para.runs)
        previous.runs = _merge_adjacent(runs)
        del elements[index + 1 :]
        return True

    # -- chapters ----------------------------------------------------------------------------
    def segment(self, elements: list[Element], book_title: str) -> tuple[list[Chapter], str]:
        """Split elements into chapters at the most significant heading level that gives a
        plausible, balanced division; otherwise fall back to page-range sections."""
        headings = [e for e in elements if isinstance(e, Heading)]
        for heading in headings:
            heading.anchor = self._anchor()

        page_count = max((e.page for e in elements), default=0) + 1
        total_chars = sum(_element_chars(e) for e in elements)
        level_counts = Counter(h.level for h in headings)
        for level in sorted(lvl for lvl in level_counts if level_counts[lvl] >= 2):
            chapters = self._chapters_at(elements, level)
            if _plausible_split(chapters, total_chars, page_count):
                return chapters, "outline" if self.use_outline else "headings"
        return self._page_range_sections(elements, book_title, headings), "page-ranges"

    @staticmethod
    def _chapters_at(elements: list[Element], split_level: int) -> list[Chapter]:
        chapters: list[Chapter] = []
        current: Chapter | None = None
        front: list[Element] = []
        for element in elements:
            if isinstance(element, Heading) and element.level <= split_level:
                # A chapter holding only headings (e.g. a title spread over several lines)
                # is merged into the next one instead of standing alone.
                if current is not None and not any(
                    _element_chars(e) for e in current.elements if not isinstance(e, Heading)
                ):
                    current.elements.append(element)
                    continue
                # Page markers directly before the heading belong to the new chapter.
                previous = current.elements if current is not None else front
                carried: list[Element] = []
                while previous and isinstance(previous[-1], PageMarker):
                    carried.insert(0, previous.pop())
                current = Chapter(
                    title=element.text,
                    elements=[*carried, element],
                    anchor=element.anchor,
                    heading_level=split_level,
                    first_page=element.page,
                )
                chapters.append(current)
            elif current is None:
                front.append(element)
            else:
                current.elements.append(element)

        if any(not isinstance(e, PageMarker) for e in front):
            pages = [e.page for e in front if not isinstance(e, PageMarker)]
            chapters.insert(
                0,
                Chapter(
                    title=_page_range_title(min(pages), max(pages)),
                    elements=front,
                    heading_level=split_level,
                    first_page=min(pages),
                ),
            )
        elif chapters and front:
            chapters[0].elements[:0] = front  # leading page markers only

        for chapter in chapters:
            chapter.toc_children = _nest_headings(
                [
                    e
                    for e in chapter.elements
                    if isinstance(e, Heading) and (e.level > split_level or e.anchor != chapter.anchor)
                ]
            )
            chapter.last_page = max((e.page for e in chapter.elements if not isinstance(e, PageMarker)), default=0)
        return chapters

    @staticmethod
    def _page_range_sections(elements: list[Element], book_title: str, headings: list[Heading]) -> list[Chapter]:
        page_count = max((e.page for e in elements), default=0) + 1
        chunk = page_count if page_count <= SECTION_PAGES + 5 else SECTION_PAGES
        sections: dict[int, list[Element]] = {}
        for element in elements:
            page = element.page_number - 1 if isinstance(element, PageMarker) else element.page
            sections.setdefault(page // chunk, []).append(element)
        chapters: list[Chapter] = []
        for key in sorted(sections):
            content = sections[key]
            if not any(not isinstance(e, PageMarker) for e in content):
                if chapters:
                    chapters[-1].elements.extend(content)
                continue
            first, last = key * chunk, min(page_count, (key + 1) * chunk) - 1
            single = len(sections) == 1
            chapters.append(
                Chapter(
                    title=book_title if single else _page_range_title(first, last),
                    elements=content,
                    heading_level=min((h.level for h in headings), default=1),
                    toc_children=_nest_headings([e for e in content if isinstance(e, Heading)]),
                    first_page=first,
                    last_page=last,
                )
            )
        return chapters

    # -- entry point -------------------------------------------------------------------------
    def build_elements(self) -> list[Element]:
        if self.options.remove_headers_footers:
            remove_running_artifacts(self.pages)
        stream = self._page_items()
        paras = [item for item in stream if isinstance(item, _Para)]
        self._classify(paras)
        self._apply_outline(stream)
        elements = self._to_elements(stream)
        if not any(isinstance(e, (Paragraph, Heading, ListBlock, Figure, Table)) for e in elements):
            raise ConversionError(
                "No readable content could be extracted from this PDF. If it is a scanned document, "
                "enable OCR (requires Tesseract on the server)."
            )
        return elements


def _element_chars(element: Element) -> int:
    if isinstance(element, (Paragraph, Heading)):
        return len(element.text)
    if isinstance(element, ListBlock):
        return sum(len(run.text) for item in element.items for run in item)
    if isinstance(element, Table):
        return sum(len(cell) for row in element.rows for cell in row)
    if isinstance(element, Figure):
        return 200  # an image counts as some content
    return 0


def _plausible_split(chapters: list[Chapter], total_chars: int, page_count: int) -> bool:
    """Reject splits that are implausibly fine-grained or leave nearly everything in one chapter."""
    if len(chapters) < 2 or len(chapters) > max(12, 3 * page_count):
        return False
    if total_chars < BALANCE_MIN_CHARS:
        return True
    largest = max(sum(_element_chars(e) for e in chapter.elements) for chapter in chapters)
    return largest <= MAX_CHAPTER_SHARE * total_chars


def _page_range_title(first: int, last: int) -> str:
    return f"Page {first + 1}" if first == last else f"Pages {first + 1}–{last + 1}"


def _nest_headings(headings: list[Heading]) -> list[TocNode]:
    """Build a nested TOC tree from headings in document order."""
    roots: list[TocNode] = []
    stack: list[TocNode] = []
    for heading in headings:
        node = TocNode(title=heading.text, anchor=heading.anchor, level=heading.level)
        while stack and stack[-1].level >= node.level:
            stack.pop()
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)
    return roots


def build_book(
    pages: list[PageContent],
    outline: list[OutlineEntry],
    options: StructureOptions,
    metadata: BookMetadata,
    *,
    title_override: str = "",
    fallback_title: str = "",
) -> Book:
    """Reconstruct the book structure. ``metadata.title`` may be empty; it is resolved here."""
    builder = StructureBuilder(pages, outline, options)
    elements = builder.build_elements()
    metadata.title = title_override or metadata.title or builder.detected_title or fallback_title or "Untitled"
    chapters, source = builder.segment(elements, metadata.title)
    return Book(metadata=metadata, chapters=chapters, structure_source=source)
