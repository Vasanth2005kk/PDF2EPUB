"""PDF to EPUB converter - Flask application factory."""

from __future__ import annotations

import atexit
import logging
from typing import Any

import click
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from app.config import Config, get_config
from app.exceptions import AppError
from app.services.job_manager import JobManager
from app.utils.cleanup import CleanupThread, cleanup_expired_jobs
from app.utils.file_handler import JobStorage

logger = logging.getLogger(__name__)

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def _configure_logging(level: str) -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root.setLevel(level)


def create_app(config: type[Config] | str | None = None, overrides: dict[str, Any] | None = None) -> Flask:
    load_dotenv()
    app = Flask(__name__)
    config_class = config if isinstance(config, type) else get_config(config)
    app.config.from_object(config_class)
    if overrides:
        app.config.update(overrides)
        if "MAX_UPLOAD_MB" in overrides and "MAX_CONTENT_LENGTH" not in overrides:
            app.config["MAX_CONTENT_LENGTH"] = overrides["MAX_UPLOAD_MB"] * 1024 * 1024
    _configure_logging(app.config["LOG_LEVEL"])

    storage = JobStorage(app.config["STORAGE_DIR"])
    storage.ensure_root()
    app.extensions["job_storage"] = storage
    manager = JobManager(storage, conversion_settings(app.config), app.config["MAX_CONCURRENT_JOBS"])
    app.extensions["job_manager"] = manager
    atexit.register(manager.shutdown)

    if app.config["ENABLE_BACKGROUND_CLEANUP"]:
        cleaner = CleanupThread(
            storage,
            app.config["JOB_TTL_SECONDS"],
            app.config["CLEANUP_INTERVAL_SECONDS"],
            app.config["CONVERSION_TIMEOUT_SECONDS"],
        )
        cleaner.start()
        app.extensions["cleanup_thread"] = cleaner

    from app.routes import converter_bp

    app.register_blueprint(converter_bp)
    _register_error_handlers(app)
    _register_security_headers(app)
    _register_cli(app)
    return app


def conversion_settings(config: Any) -> dict[str, Any]:
    """Plain, picklable settings passed to conversion subprocesses."""
    return {
        "timeout": config["CONVERSION_TIMEOUT_SECONDS"],
        "max_pages": config["MAX_PAGES"],
        "max_image_pixels": config["MAX_IMAGE_PIXELS"],
        "ocr_dpi": config["OCR_DPI"],
        "ocr_timeout": config["OCR_PAGE_TIMEOUT_SECONDS"],
        "tesseract_cmd": config["TESSERACT_CMD"],
        "epubcheck_path": config["EPUBCHECK_PATH"],
        "epubcheck_timeout": config["EPUBCHECK_TIMEOUT_SECONDS"],
        "log_level": config["LOG_LEVEL"],
    }


def _error(message: str, status: int, code: str):
    return jsonify({"success": False, "message": message, "code": code}), status


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(AppError)
    def handle_app_error(exc: AppError):
        if exc.status_code >= 500:
            logger.error("%s: %s", exc.code, exc.message)
        return _error(exc.message, exc.status_code, exc.code)

    @app.errorhandler(RequestEntityTooLarge)
    def handle_too_large(exc: RequestEntityTooLarge):
        return _error(f"The file exceeds the {app.config['MAX_UPLOAD_MB']} MB upload limit.", 413, "too_large")

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        messages = {
            404: "The requested resource was not found.",
            405: "This method is not allowed for the requested URL.",
        }
        return _error(messages.get(exc.code or 500, exc.description or exc.name), exc.code or 500, "http_error")

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.path)
        return _error("An internal server error occurred. Please try again.", 500, "internal_error")


def _register_security_headers(app: Flask) -> None:
    @app.after_request
    def add_headers(response):
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def _register_cli(app: Flask) -> None:
    @app.cli.command("cleanup")
    @click.option("--ttl", type=int, default=None, help="Maximum job age in seconds (defaults to JOB_TTL_SECONDS).")
    def cleanup_command(ttl: int | None) -> None:
        """Delete expired conversion jobs from storage."""
        removed = cleanup_expired_jobs(
            app.extensions["job_storage"],
            ttl if ttl is not None else app.config["JOB_TTL_SECONDS"],
            app.config["CONVERSION_TIMEOUT_SECONDS"],
        )
        click.echo(f"Removed {removed} expired job(s).")
