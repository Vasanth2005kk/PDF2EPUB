"""The conversion pipeline: PDF -> extracted pages -> book structure -> EPUB 3 -> validation.

``run_job_process`` is the entry point of the per-job subprocess started by the job manager.
Progress is reported by rewriting the job's ``job.json`` so any web worker can serve it.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import pymupdf

from app.exceptions import AppError, ConversionCancelled, ConversionError, ConversionTimeoutError
from app.services.content_processor import StructureOptions, build_book
from app.services.epub_generator import generate_epub
from app.services.epub_validator import validate_epub
from app.services.models import BookMetadata, OutlineEntry, PageContent
from app.services.ocr_service import OcrError, get_ocr_status, ocr_page, resolve_ocr_language
from app.services.pdf_analyzer import (
    MIN_TEXT_CHARS_PER_PAGE,
    clean_metadata_title,
    open_pdf,
    page_image_coverage,
    parse_pdf_date,
)
from app.services.pdf_extractor import (
    ImageSettings,
    ImageStore,
    extract_page_images,
    extract_tables,
    extract_text_blocks,
    render_cover,
    render_page_image,
)
from app.utils.file_handler import (
    CANCEL_MARKER,
    OUTPUT_NAME,
    SOURCE_NAME,
    STATE_NAME,
    WORK_DIR_NAME,
    read_state_file,
    write_state_file,
)
from app.utils.validators import ConversionOptions

logger = logging.getLogger(__name__)

STAGES: list[tuple[str, str]] = [
    ("analyze", "Analyzing PDF"),
    ("text", "Extracting text"),
    ("ocr", "OCR for scanned pages"),
    ("images", "Extracting images"),
    ("structure", "Reconstructing structure"),
    ("epub", "Generating EPUB"),
    ("validate", "Validating EPUB"),
]
WRITE_INTERVAL_SECONDS = 0.3
VECTOR_PAGE_DRAWINGS = 50  # pages without text but with this many drawings may be outlined text


def initial_stages() -> list[dict[str, Any]]:
    return [
        {"key": key, "label": label, "status": "pending", "current": 0, "total": 0, "detail": ""}
        for key, label in STAGES
    ]


class ProgressReporter:
    """Records real pipeline progress in ``job.json`` and enforces cancellation and the deadline."""

    def __init__(self, job_dir: Path, deadline: float) -> None:
        self.job_dir = job_dir
        self.state_path = job_dir / STATE_NAME
        self.deadline = deadline
        self.state = read_state_file(self.state_path)
        self.state["stages"] = initial_stages()
        self.state["warnings"] = []
        self._last_write = 0.0

    def _stage(self, key: str) -> dict[str, Any]:
        return next(stage for stage in self.state["stages"] if stage["key"] == key)

    def write(self, force: bool = True) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < WRITE_INTERVAL_SECONDS:
            return
        self.state["updated_at"] = time.time()
        write_state_file(self.state_path, self.state)
        self._last_write = now

    def check(self) -> None:
        if (self.job_dir / CANCEL_MARKER).exists() or not self.job_dir.exists():
            raise ConversionCancelled("The conversion was cancelled.")
        if time.monotonic() > self.deadline:
            raise ConversionTimeoutError("The conversion took too long and was stopped. Try a smaller PDF.")

    def begin(self) -> None:
        self.state.update(status="processing", started_at=time.time(), message="Conversion in progress")
        self.write()

    def start(self, key: str, total: int = 0, detail: str = "") -> None:
        self.check()
        stage = self._stage(key)
        stage.update(status="running", total=total, current=0, detail=detail)
        self.state["stage"] = key
        self.write()

    def advance(self, key: str, current: int, detail: str | None = None) -> None:
        self.check()
        stage = self._stage(key)
        stage["current"] = current
        if detail is not None:
            stage["detail"] = detail
        self.write(force=False)

    def finish(self, key: str, detail: str = "") -> None:
        stage = self._stage(key)
        stage.update(status="done", current=stage["total"] or stage["current"], detail=detail or stage["detail"])
        self.write()

    def skip(self, key: str, detail: str) -> None:
        self._stage(key).update(status="skipped", detail=detail)
        self.write()

    def warn(self, message: str) -> None:
        if message not in self.state["warnings"] and len(self.state["warnings"]) < 50:
            self.state["warnings"].append(message)

    def fail(self, message: str, code: str = "conversion_failed") -> None:
        current = self.state.get("stage")
        for stage in self.state["stages"]:
            if stage["key"] == current and stage["status"] == "running":
                stage["status"] = "failed"
        status = "cancelled" if code == "cancelled" else "failed"
        self.state.update(status=status, message=message, error_code=code, finished_at=time.time())
        self.write()

    def complete(self, **result: Any) -> None:
        self.state.update(status="completed", message="EPUB conversion completed successfully", **result)
        self.state["finished_at"] = time.time()
        self.write()


def _pages_label(count: int) -> str:
    return f"{count} page" + ("" if count == 1 else "s")


def _split_list(value: str, pattern: str) -> list[str]:
    return [part.strip() for part in re.split(pattern, value or "") if part.strip()]


def _fallback_title(original_filename: str) -> str:
    stem = Path(original_filename or "").stem
    return re.sub(r"[_]+", " ", stem).strip()


def run_conversion(job_dir: Path, options: ConversionOptions, settings: dict[str, Any]) -> None:
    """Run the full pipeline for one job, recording progress and the outcome in ``job.json``."""
    deadline = time.monotonic() + float(settings["timeout"])
    reporter = ProgressReporter(job_dir, deadline)
    work_dir = job_dir / WORK_DIR_NAME
    image_dir = work_dir / "images"
    output_path = job_dir / OUTPUT_NAME
    reporter.begin()
    try:
        if output_path.exists():
            output_path.unlink()
        shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir()
        _convert(job_dir, options, settings, reporter, image_dir, output_path)
    except AppError as exc:
        logger.info("Job %s failed: %s", job_dir.name, exc.message)
        reporter.fail(exc.message, exc.code)
    except MemoryError:
        logger.exception("Job %s ran out of memory", job_dir.name)
        reporter.fail("The server ran out of memory while converting this PDF. Try a smaller file.")
    except OSError:
        logger.exception("Job %s storage failure", job_dir.name)
        if job_dir.exists():
            reporter.fail("A server storage error occurred during conversion.", "storage_error")
    except Exception:
        logger.exception("Job %s failed unexpectedly", job_dir.name)
        reporter.fail("An unexpected error occurred while converting the PDF.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _convert(
    job_dir: Path,
    options: ConversionOptions,
    settings: dict[str, Any],
    reporter: ProgressReporter,
    image_dir: Path,
    output_path: Path,
) -> None:
    reporter.start("analyze")
    with open_pdf(job_dir / SOURCE_NAME, max_pages=settings["max_pages"]) as doc:
        page_count = doc.page_count
        meta = doc.metadata or {}
        try:
            outline = [
                OutlineEntry(level=level, title=title, page=page - 1)
                for level, title, page in doc.get_toc(simple=True)
                if page >= 1
            ]
        except Exception:
            outline = []
        reporter.finish("analyze", f"{_pages_label(page_count)}" + (f", {len(outline)} bookmarks" if outline else ""))

        # ---- text ---------------------------------------------------------------------------
        pages: list[PageContent] = []
        reporter.start("text", total=page_count)
        text_failures = 0
        for index in range(page_count):
            page = doc.load_page(index)
            content = PageContent(index=index, width=page.rect.width, height=page.rect.height)
            try:
                blocks, chars = extract_text_blocks(page, index)
            except Exception as exc:
                logger.warning("Text extraction failed on page %d: %s", index + 1, exc)
                blocks, chars = [], 0
                text_failures += 1
            if options.extract_tables and chars:
                content.tables = extract_tables(page, index)
                if content.tables:
                    blocks = [b for b in blocks if not _inside_any(b.bbox, [t.bbox for t in content.tables])]
            content.blocks, content.char_count = blocks, chars
            content.image_coverage = page_image_coverage(page)
            if chars < MIN_TEXT_CHARS_PER_PAGE:
                content.needs_ocr = content.image_coverage > 0.3 or _looks_like_vector_text(page)
            pages.append(content)
            reporter.advance("text", index + 1, f"Page {index + 1} of {page_count}")
        if text_failures:
            reporter.warn(f"Text could not be extracted from {_pages_label(text_failures)}.")
        total_chars = sum(p.char_count for p in pages)
        reporter.finish("text", f"{total_chars:,} characters from {_pages_label(page_count)}")

        # ---- OCR ----------------------------------------------------------------------------
        scanned = [p for p in pages if p.needs_ocr]
        if not scanned:
            reporter.skip("ocr", "No scanned pages detected")
        elif not options.enable_ocr:
            reporter.skip("ocr", f"OCR disabled ({_pages_label(len(scanned))} without text)")
            reporter.warn(
                f"{_pages_label(len(scanned))} appear to be scanned. Enable OCR to extract their text; "
                "they are included as images instead."
            )
        else:
            status = get_ocr_status(settings.get("tesseract_cmd"))
            if not status.available:
                reporter.skip("ocr", "Tesseract not available")
                reporter.warn(f"{status.message} Scanned pages are included as images instead of text.")
            else:
                lang, lang_warning = resolve_ocr_language(options.language, status)
                if lang_warning:
                    reporter.warn(lang_warning)
                reporter.start("ocr", total=len(scanned), detail=f"Tesseract ({lang})")
                failed = 0
                for done, content in enumerate(scanned, start=1):
                    reporter.advance("ocr", done - 1, f"Page {content.index + 1} ({done} of {len(scanned)})")
                    try:
                        blocks = ocr_page(
                            doc.load_page(content.index),
                            content.index,
                            dpi=settings["ocr_dpi"],
                            lang=lang,
                            timeout=settings["ocr_timeout"],
                        )
                    except OcrError as exc:
                        logger.warning("OCR failed on page %d: %s", content.index + 1, exc)
                        failed += 1
                        continue
                    if blocks:
                        content.blocks = blocks
                        content.tables = []
                        content.ocr_applied = True
                    reporter.advance("ocr", done)
                recognised = sum(1 for p in scanned if p.ocr_applied)
                if failed:
                    reporter.warn(f"OCR failed on {_pages_label(failed)}; those pages are included as images.")
                reporter.finish("ocr", f"Text recognised on {recognised} of {_pages_label(len(scanned))}")

        # ---- images -------------------------------------------------------------------------
        store = ImageStore(image_dir)
        unrecovered = [p for p in pages if p.needs_ocr and not p.ocr_applied]
        if options.extract_images:
            image_settings = ImageSettings(
                preserve_original=options.preserve_original_images,
                quality=options.image_quality,
                max_pixels=settings["max_image_pixels"],
            )
            reporter.start("images", total=page_count)
            failures = 0
            for content in pages:
                page = doc.load_page(content.index)
                images, warnings = extract_page_images(
                    doc, page, content.index, store, image_settings, skip_page_scans=content.ocr_applied
                )
                failures += sum(1 for w in warnings if "could not" in w)
                for warning in warnings:
                    reporter.warn(warning)
                content.images = images
                page_area = content.width * content.height or 1.0
                has_page_image = any(
                    (img.bbox[2] - img.bbox[0]) * (img.bbox[3] - img.bbox[1]) / page_area > 0.5 for img in images
                )
                if content in unrecovered and not has_page_image:
                    content.images.append(render_page_image(page, store, content.index))
                reporter.advance(
                    "images", content.index + 1, f"Page {content.index + 1} of {page_count}, {store.counter} images"
                )
            reporter.finish("images", f"{store.counter} unique images extracted")
        else:
            reporter.skip("images", "Image extraction disabled")
            if unrecovered:
                reporter.warn(
                    f"{_pages_label(len(unrecovered))} without extractable text were omitted because "
                    "image extraction is disabled."
                )

        # ---- structure ----------------------------------------------------------------------
        reporter.start("structure")
        created = parse_pdf_date(meta.get("creationDate"))
        metadata = BookMetadata(
            title=clean_metadata_title(meta.get("title")),
            language=options.language,
            identifier=f"urn:uuid:{uuid.uuid4()}",
            authors=[options.author] if options.author else _split_list(meta.get("author", ""), r";"),
            description=(meta.get("subject") or "").strip()[:2000],
            subjects=_split_list(meta.get("keywords", ""), r"[,;]")[:20],
            date=created[:10] if created else "",
        )
        state = reporter.state
        book = build_book(
            pages,
            outline,
            StructureOptions(
                detect_headings=options.detect_headings,
                remove_headers_footers=options.remove_headers_footers,
            ),
            metadata,
            title_override=options.title,
            fallback_title=_fallback_title(state.get("original_filename", "")),
        )
        source_label = {
            "outline": "PDF bookmarks",
            "headings": "detected headings",
            "page-ranges": "page ranges (no reliable headings found)",
        }[book.structure_source]
        reporter.finish("structure", f"{len(book.chapters)} sections from {source_label}")
        if book.structure_source == "page-ranges" and len(book.chapters) > 1:
            reporter.warn("No reliable chapter headings were found; the book is divided into page-range sections.")

        cover = None
        if options.generate_cover:
            try:
                cover = render_cover(doc)
            except Exception as exc:
                logger.warning("Cover rendering failed: %s", exc)
                reporter.warn("The cover image could not be generated.")

    # The PDF is closed from here on.
    del pages

    # ---- EPUB ---------------------------------------------------------------------------
    reporter.start("epub", total=len(book.chapters))
    result = generate_epub(
        book,
        output_path,
        image_dir,
        generate_toc=options.generate_toc,
        cover_image=cover,
        progress=lambda done, total: reporter.advance("epub", done, f"Chapter {done} of {total}"),
    )
    reporter.finish("epub", f"{result.document_count} documents, {result.image_count} images")

    # ---- validation ---------------------------------------------------------------------
    reporter.start("validate")
    report = validate_epub(
        output_path,
        epubcheck_path=settings.get("epubcheck_path"),
        epubcheck_timeout=settings["epubcheck_timeout"],
    )
    if not report.structural_valid:
        logger.error("Structural validation failed for job %s: %s", job_dir.name, report.structural_errors)
        output_path.unlink(missing_ok=True)
        raise ConversionError("The generated EPUB failed validation and was discarded.", code="validation_failed")
    if report.epubcheck_ran and not report.epubcheck_valid:
        reporter.warn("EPUBCheck reported problems with the generated EPUB; most readers should still open it.")
    reporter.finish("validate", report.summary)

    reporter.complete(
        output_filename=options.output_filename,
        epub_size=output_path.stat().st_size,
        result={
            "title": book.metadata.title,
            "authors": book.metadata.authors,
            "language": book.metadata.language,
            "chapters": result.chapter_count,
            "documents": result.document_count,
            "images": result.image_count,
            "toc_entries": result.toc_entries,
            "structure_source": book.structure_source,
        },
        validation=report.to_dict(),
    )


def _inside_any(bbox: tuple[float, float, float, float], regions: list[tuple[float, float, float, float]]) -> bool:
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in regions)


def _looks_like_vector_text(page: pymupdf.Page) -> bool:
    try:
        return len(page.get_drawings()) >= VECTOR_PAGE_DRAWINGS
    except Exception:
        return False


def run_job_process(job_dir: str, options: dict[str, Any], settings: dict[str, Any]) -> None:
    """Subprocess entry point (must be importable for the ``spawn`` start method)."""
    logging.basicConfig(
        level=settings.get("log_level", "INFO"),
        format="%(asctime)s [%(levelname)s] conversion[%(process)d] %(name)s: %(message)s",
    )
    pymupdf.TOOLS.mupdf_display_errors(False)
    run_conversion(Path(job_dir), ConversionOptions.from_dict(options), settings)
