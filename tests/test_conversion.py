"""The full conversion pipeline, run synchronously on generated PDFs."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from app.services.conversion_service import run_conversion
from app.utils.validators import ConversionOptions
from tests.conftest import HAS_EPUBCHECK, pipeline_settings, requires_ocr


def _convert(job_dir: Path, **options) -> dict:
    run_conversion(job_dir, ConversionOptions(**options), pipeline_settings())
    return json.loads((job_dir / "job.json").read_text())


def _stage(state: dict, key: str) -> dict:
    return next(s for s in state["stages"] if s["key"] == key)


def _xhtml(job_dir: Path) -> str:
    with zipfile.ZipFile(job_dir / "output.epub") as archive:
        return "".join(archive.read(n).decode() for n in sorted(archive.namelist()) if n.startswith("EPUB/text/"))


def test_book_conversion(make_pdf, job_dir_for):
    job = job_dir_for(make_pdf("book_pdf"))
    state = _convert(job, output_filename="book.epub")
    assert state["status"] == "completed", state.get("message")
    assert state["result"]["chapters"] == 3
    assert state["result"]["structure_source"] == "headings"
    assert state["result"]["title"] == "The Test Book"
    assert state["epub_size"] == (job / "output.epub").stat().st_size
    assert all(s["status"] in {"done", "skipped"} for s in state["stages"])
    assert _stage(state, "text")["current"] == 4  # real per-page progress
    assert _stage(state, "ocr")["status"] == "skipped"
    validation = state["validation"]
    assert validation["structural_valid"]
    if HAS_EPUBCHECK:
        assert validation["epubcheck_ran"] and validation["epubcheck_valid"], validation["epubcheck_messages"]
    else:
        assert "full validation not performed" in validation["summary"]
    assert not (job / "work").exists()  # temporary files removed


def test_images_conversion(make_pdf, job_dir_for):
    job = job_dir_for(make_pdf("images_pdf"))
    state = _convert(job, generate_cover=False)
    assert state["status"] == "completed"
    assert state["result"]["images"] == 3
    with zipfile.ZipFile(job / "output.epub") as archive:
        images = [n for n in archive.namelist() if n.startswith("EPUB/images/")]
    assert len(images) == 3
    assert _xhtml(job).count("<img") == 4


def test_image_extraction_disabled(make_pdf, job_dir_for):
    job = job_dir_for(make_pdf("images_pdf"))
    state = _convert(job, extract_images=False, generate_cover=False)
    assert state["status"] == "completed"
    assert _stage(state, "images")["status"] == "skipped"
    assert "<img" not in _xhtml(job)


def test_title_and_author_overrides(make_pdf, job_dir_for):
    job = job_dir_for(make_pdf("simple_text_pdf"))
    state = _convert(job, title="Custom Title", author="Custom Author", language="ta")
    assert state["result"]["title"] == "Custom Title"
    assert state["result"]["authors"] == ["Custom Author"]
    with zipfile.ZipFile(job / "output.epub") as archive:
        opf = archive.read("EPUB/content.opf").decode()
    assert "<dc:language>ta</dc:language>" in opf and "Custom Author" in opf


def test_unicode_tamil_conversion(make_pdf, job_dir_for):
    job = job_dir_for(make_pdf("tamil_pdf"))
    state = _convert(job, language="ta")
    assert state["status"] == "completed"
    assert "தமிழ் ஒரு செம்மொழி" in _xhtml(job)


@requires_ocr
def test_scanned_pdf_with_ocr(make_pdf, job_dir_for):
    job = job_dir_for(make_pdf("scanned_pdf"))
    state = _convert(job)
    assert state["status"] == "completed"
    assert _stage(state, "ocr")["status"] == "done"
    content = _xhtml(job)
    assert "scanned from paper" in content
    assert "<img" not in content  # the scan is replaced by its recognised text


def test_scanned_pdf_without_ocr_keeps_page_image(make_pdf, job_dir_for):
    job = job_dir_for(make_pdf("scanned_pdf"))
    state = _convert(job, enable_ocr=False)
    assert state["status"] == "completed"
    assert _stage(state, "ocr")["status"] == "skipped"
    assert any("Enable OCR" in w for w in state["warnings"])
    assert "<img" in _xhtml(job)


def test_blank_pdf_fails_cleanly(make_pdf, job_dir_for):
    job = job_dir_for(make_pdf("blank_pdf"))
    state = _convert(job)
    assert state["status"] == "failed"
    assert "No readable content" in state["message"]
    assert not (job / "output.epub").exists()


def test_encrypted_pdf_fails_cleanly(make_pdf, job_dir_for):
    state = _convert(job_dir_for(make_pdf("encrypted_pdf")))
    assert state["status"] == "failed"
    assert state["error_code"] == "password_protected"


def test_cancellation(make_pdf, job_dir_for):
    job = job_dir_for(make_pdf("book_pdf"))
    (job / "cancel").touch()
    state = _convert(job)
    assert state["status"] == "cancelled"


def test_timeout(make_pdf, job_dir_for):
    job = job_dir_for(make_pdf("book_pdf"))
    run_conversion(job, ConversionOptions(), pipeline_settings(timeout=0))
    state = json.loads((job / "job.json").read_text())
    assert state["status"] == "failed"
    assert state["error_code"] == "timeout"
    assert "Traceback" not in state["message"]


def test_page_range_sections_reported(make_pdf, job_dir_for):
    job = job_dir_for(make_pdf("large_pdf", pages=25))
    state = _convert(job)
    assert state["status"] == "completed"
    assert state["result"]["structure_source"] == "page-ranges"
    assert any("page-range sections" in w for w in state["warnings"])
