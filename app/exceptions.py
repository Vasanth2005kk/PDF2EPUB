"""Application exceptions.

Every exception that may reach a user carries a safe, human-readable ``message``.
Internal details (paths, tracebacks) are only ever logged, never returned.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for expected, user-facing errors."""

    status_code: int = 400
    code: str = "error"

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class InvalidFileError(AppError):
    """The uploaded file is not an acceptable PDF."""

    code = "invalid_file"


class EncryptedPDFError(InvalidFileError):
    code = "password_protected"


class CorruptedPDFError(InvalidFileError):
    code = "corrupted_pdf"


class EmptyPDFError(InvalidFileError):
    code = "empty_pdf"


class PDFTooLargeError(InvalidFileError):
    status_code = 413
    code = "too_large"


class JobNotFoundError(AppError):
    status_code = 404
    code = "job_not_found"

    def __init__(self, message: str = "Conversion job not found or it has expired.") -> None:
        super().__init__(message)


class JobStateError(AppError):
    """The requested action is not allowed in the job's current state."""

    status_code = 409
    code = "invalid_job_state"


class StorageError(AppError):
    status_code = 507
    code = "storage_error"


class ConversionError(AppError):
    """Raised inside the conversion pipeline with a user-facing message."""

    status_code = 500
    code = "conversion_failed"


class ConversionCancelled(ConversionError):
    code = "cancelled"


class ConversionTimeoutError(ConversionError):
    code = "timeout"
