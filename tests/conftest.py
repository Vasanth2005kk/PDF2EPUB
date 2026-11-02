from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from app import create_app
from app.services.epub_validator import epubcheck_available
from app.services.ocr_service import get_ocr_status
from app.utils.file_handler import write_state_file
from tests import pdf_factory

EPUBCHECK_PATH = os.environ.get("EPUBCHECK_PATH") or None
HAS_EPUBCHECK = epubcheck_available(EPUBCHECK_PATH)
HAS_OCR = get_ocr_status(None).available

requires_ocr = pytest.mark.skipif(not HAS_OCR, reason="Tesseract is not installed")
requires_epubcheck = pytest.mark.skipif(not HAS_EPUBCHECK, reason="EPUBCheck not available (set EPUBCHECK_PATH)")


@pytest.fixture(scope="session")
def pdf_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("pdfs")


@pytest.fixture(scope="session")
def make_pdf(pdf_dir: Path):
    """Return a factory: ``make_pdf("book_pdf")`` creates (once) and returns the sample PDF path."""
    cache: dict[str, Path] = {}

    def factory(name: str, **kwargs: Any) -> Path:
        key = name + repr(sorted(kwargs.items()))
        if key not in cache:
            path = pdf_dir / f"{name}_{len(cache)}.pdf"
            result = getattr(pdf_factory, name)(path, **kwargs)
            if result is None:
                pytest.skip(f"{name} cannot be generated on this system (missing font)")
            cache[key] = result
        return cache[key]

    return factory


@pytest.fixture
def app(tmp_path: Path):
    app = create_app(
        "testing",
        overrides={
            "STORAGE_DIR": tmp_path / "storage",
            "EPUBCHECK_PATH": EPUBCHECK_PATH,
            "MAX_UPLOAD_MB": 5,
            "CONVERSION_TIMEOUT_SECONDS": 120,
        },
    )
    yield app
    app.extensions["job_manager"].shutdown()


@pytest.fixture
def client(app):
    return app.test_client()


def pipeline_settings(**overrides: Any) -> dict[str, Any]:
    settings = {
        "timeout": 120,
        "max_pages": 2000,
        "max_image_pixels": 60_000_000,
        "ocr_dpi": 300,
        "ocr_timeout": 60,
        "tesseract_cmd": None,
        "epubcheck_path": EPUBCHECK_PATH,
        "epubcheck_timeout": 120,
        "log_level": "INFO",
    }
    settings.update(overrides)
    return settings


@pytest.fixture
def job_dir_for(tmp_path: Path):
    """Create a job directory containing ``source.pdf`` and an initial ``job.json``."""

    def factory(pdf: Path, name: str = "job") -> Path:
        job_dir = tmp_path / "jobs" / name
        job_dir.mkdir(parents=True)
        shutil.copy(pdf, job_dir / "source.pdf")
        write_state_file(
            job_dir / "job.json", {"job_id": name, "status": "queued", "original_filename": "sample_document.pdf"}
        )
        return job_dir

    return factory
