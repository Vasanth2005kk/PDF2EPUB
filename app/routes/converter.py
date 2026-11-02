"""HTTP endpoints: the single-page UI and the JSON conversion API."""

from __future__ import annotations

import logging
import time
from typing import Any

from flask import Blueprint, current_app, jsonify, render_template, request, send_file, url_for

from app.exceptions import AppError, InvalidFileError, JobNotFoundError, JobStateError
from app.services.epub_validator import epubcheck_available
from app.services.job_manager import JobManager
from app.services.ocr_service import get_ocr_status
from app.services.pdf_analyzer import analyze_pdf
from app.utils.file_handler import ACTIVE_STATUSES, JobStorage
from app.utils.validators import (
    parse_conversion_options,
    sanitize_output_filename,
    validate_mime_type,
    validate_upload_name,
)

logger = logging.getLogger(__name__)

bp = Blueprint("converter", __name__)

# Fields of job.json that are safe to expose to the browser.
PUBLIC_FIELDS = (
    "job_id", "status", "stage", "stages", "message", "warnings", "original_filename", "file_size",
    "output_filename", "epub_size", "validation", "result", "error_code", "created_at", "updated_at",
)  # fmt: skip


def _storage() -> JobStorage:
    return current_app.extensions["job_storage"]


def _jobs() -> JobManager:
    return current_app.extensions["job_manager"]


def _ok(payload: dict[str, Any], status: int = 200):
    return jsonify({"success": True, **payload}), status


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    public = {key: state.get(key) for key in PUBLIC_FIELDS}
    if state.get("status") == "completed":
        public["filename"] = state.get("output_filename")
        public["download_url"] = url_for("converter.download", job_id=state["job_id"])
    return public


def _detect_orphan(job_id: str, state: dict[str, Any]) -> dict[str, Any]:
    """A job that stopped reporting progress (e.g. the server restarted) is reported as failed."""
    if state.get("status") not in ACTIVE_STATUSES:
        return state
    timeout = current_app.config["CONVERSION_TIMEOUT_SECONDS"]
    if time.time() - float(state.get("updated_at") or 0) > timeout + 60 and not _jobs().is_running(job_id):
        state = _storage().update_state(
            job_id,
            status="failed",
            message="The conversion was interrupted. Please try again.",
            error_code="interrupted",
        )
    return state


@bp.get("/")
def index():
    config = current_app.config
    ocr = get_ocr_status(config["TESSERACT_CMD"])
    return render_template(
        "index.html",
        max_upload_mb=config["MAX_UPLOAD_MB"],
        max_pages=config["MAX_PAGES"],
        ocr_available=ocr.available,
        ocr_message=ocr.message,
    )


@bp.get("/api/health")
def health():
    config = current_app.config
    ocr = get_ocr_status(config["TESSERACT_CMD"])
    return _ok(
        {
            "message": "Service is running",
            "ocr": {"available": ocr.available, "version": ocr.version, "languages": list(ocr.languages)},
            "epubcheck_available": epubcheck_available(config["EPUBCHECK_PATH"]),
            "limits": {"max_upload_mb": config["MAX_UPLOAD_MB"], "max_pages": config["MAX_PAGES"]},
        }
    )


@bp.post("/api/upload")
def upload():
    file = request.files.get("file")
    if file is None:
        raise InvalidFileError("No file was uploaded. Send the PDF in a form field named 'file'.")
    original_name = validate_upload_name(file.filename)
    validate_mime_type(file.mimetype)

    config = current_app.config
    storage = _storage()
    job_id, pdf_path = storage.create_job(file.stream, config["MAX_CONTENT_LENGTH"])
    try:
        ocr = get_ocr_status(config["TESSERACT_CMD"])
        preview = analyze_pdf(pdf_path, max_pages=config["MAX_PAGES"], ocr_available=ocr.available)
        now = time.time()
        state = {
            "job_id": job_id,
            "status": "uploaded",
            "stage": None,
            "message": "PDF uploaded and analyzed",
            "original_filename": original_name,
            "file_size": pdf_path.stat().st_size,
            "output_filename": sanitize_output_filename(None, original_name),
            "preview": preview,
            "warnings": [],
            "created_at": now,
            "updated_at": now,
        }
        storage.write_state(job_id, state)
    except Exception:
        storage.delete_job(job_id)
        raise

    logger.info("Uploaded job %s (%d bytes, %d pages)", job_id, state["file_size"], preview["page_count"])
    return _ok(
        {
            "message": "PDF uploaded successfully",
            "job_id": job_id,
            "filename": original_name,
            "file_size": state["file_size"],
            "default_output_filename": state["output_filename"],
            "preview": preview,
        },
        201,
    )


@bp.get("/api/preview/<job_id>")
def preview(job_id: str):
    state = _storage().read_state(job_id)
    return _ok(
        {
            "message": "PDF preview",
            "job_id": job_id,
            "filename": state.get("original_filename"),
            "file_size": state.get("file_size"),
            "default_output_filename": sanitize_output_filename(None, state.get("original_filename") or ""),
            "status": state.get("status"),
            "preview": state.get("preview"),
        }
    )


@bp.post("/api/convert/<job_id>")
def convert(job_id: str):
    storage = _storage()
    state = _detect_orphan(job_id, storage.read_state(job_id))
    if state.get("status") in ACTIVE_STATUSES:
        raise JobStateError("This PDF is already being converted.")
    if not storage.source_path(job_id).is_file():
        raise JobNotFoundError("The uploaded PDF is no longer available. Please upload it again.")

    payload = request.get_json(silent=True)
    if request.data and payload is None:
        raise AppError("Conversion settings must be valid JSON.")
    options = parse_conversion_options(payload, state.get("original_filename") or "converted-book.pdf")
    state = _jobs().submit(job_id, options)
    logger.info("Queued conversion for job %s", job_id)
    return _ok(
        {
            "message": "Conversion started",
            "job_id": job_id,
            "status": state["status"],
            "status_url": url_for("converter.status", job_id=job_id),
        },
        202,
    )


@bp.get("/api/status/<job_id>")
def status(job_id: str):
    state = _detect_orphan(job_id, _storage().read_state(job_id))
    payload = _public_state(state)
    if state.get("status") == "completed":
        payload["message"] = "EPUB conversion completed successfully"
    return _ok(payload)


@bp.get("/api/download/<job_id>")
def download(job_id: str):
    storage = _storage()
    state = storage.read_state(job_id)
    output = storage.output_path(job_id)
    if state.get("status") != "completed":
        raise JobStateError("The EPUB is not ready yet.")
    if not output.is_file():
        raise JobNotFoundError("The converted EPUB is no longer available. Please convert the PDF again.")
    response = send_file(
        output,
        mimetype="application/epub+zip",
        as_attachment=True,
        download_name=state.get("output_filename") or "converted-book.epub",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.delete("/api/job/<job_id>")
def delete_job(job_id: str):
    storage = _storage()
    state = storage.read_state(job_id)  # 404 for unknown jobs
    if state.get("status") in ACTIVE_STATUSES:
        _jobs().cancel(job_id)
    storage.delete_job(job_id)
    logger.info("Deleted job %s", job_id)
    return _ok({"message": "Job files deleted", "job_id": job_id})
