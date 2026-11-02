"""Environment-driven application configuration."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {value!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Base configuration. Every value can be overridden through environment variables."""

    SECRET_KEY: str = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

    # Storage: every job gets its own directory below STORAGE_DIR/jobs.
    STORAGE_DIR: Path = Path(os.environ.get("STORAGE_DIR") or BASE_DIR / "storage")

    # Upload and processing limits.
    MAX_UPLOAD_MB: int = _env_int("MAX_UPLOAD_MB", 100)
    MAX_CONTENT_LENGTH: int = MAX_UPLOAD_MB * 1024 * 1024
    MAX_PAGES: int = _env_int("MAX_PAGES", 2000)
    CONVERSION_TIMEOUT_SECONDS: int = _env_int("CONVERSION_TIMEOUT_SECONDS", 900)
    MAX_CONCURRENT_JOBS: int = _env_int("MAX_CONCURRENT_JOBS", 2)
    MAX_IMAGE_PIXELS: int = _env_int("MAX_IMAGE_PIXELS", 60_000_000)

    # OCR.
    OCR_DPI: int = _env_int("OCR_DPI", 300)
    OCR_PAGE_TIMEOUT_SECONDS: int = _env_int("OCR_PAGE_TIMEOUT_SECONDS", 120)
    TESSERACT_CMD: str | None = os.environ.get("TESSERACT_CMD") or None

    # Optional EPUBCheck validation: path to epubcheck.jar or an `epubcheck` executable.
    EPUBCHECK_PATH: str | None = os.environ.get("EPUBCHECK_PATH") or None
    EPUBCHECK_TIMEOUT_SECONDS: int = _env_int("EPUBCHECK_TIMEOUT_SECONDS", 180)

    # Temporary file cleanup.
    JOB_TTL_SECONDS: int = _env_int("JOB_TTL_SECONDS", 3600)
    CLEANUP_INTERVAL_SECONDS: int = _env_int("CLEANUP_INTERVAL_SECONDS", 300)
    ENABLE_BACKGROUND_CLEANUP: bool = _env_bool("ENABLE_BACKGROUND_CLEANUP", True)

    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

    JSON_SORT_KEYS = False
    TESTING = False


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


class TestingConfig(Config):
    TESTING = True
    ENABLE_BACKGROUND_CLEANUP = False
    CONVERSION_TIMEOUT_SECONDS = 300


CONFIGS: dict[str, type[Config]] = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}


def get_config(name: str | None = None) -> type[Config]:
    """Return the configuration class for ``name`` (defaults to ``FLASK_ENV``/``APP_ENV``)."""
    key = (name or os.environ.get("APP_ENV") or "production").lower()
    if key not in CONFIGS:
        raise ValueError(f"Unknown configuration {key!r}; expected one of {sorted(CONFIGS)}")
    return CONFIGS[key]
