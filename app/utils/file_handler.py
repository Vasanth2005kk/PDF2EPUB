"""Job storage on disk.

Layout (one directory per job, named by a server-generated UUID)::

    STORAGE_DIR/jobs/<job_id>/
        source.pdf     uploaded PDF (never executed, never served back)
        job.json       job state and progress, written atomically
        work/          intermediate files (extracted images, ...)
        output.epub    generated eBook

Job state lives on disk rather than in process memory so that every web worker
(and the conversion subprocess) sees the same status.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import IO, Any

from app.exceptions import JobNotFoundError, PDFTooLargeError, StorageError
from app.utils.validators import is_valid_job_id

logger = logging.getLogger(__name__)

SOURCE_NAME = "source.pdf"
STATE_NAME = "job.json"
OUTPUT_NAME = "output.epub"
WORK_DIR_NAME = "work"
CANCEL_MARKER = "cancel"

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_STATUSES = {"queued", "processing"}

_state_lock = threading.Lock()


def new_job_id() -> str:
    return uuid.uuid4().hex


class JobStorage:
    """Filesystem access for conversion jobs. All paths are derived from validated job IDs."""

    def __init__(self, storage_dir: Path | str) -> None:
        self.root = Path(storage_dir).resolve() / "jobs"

    def ensure_root(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.exception("Cannot create storage directory %s", self.root)
            raise StorageError("Server storage is not available.") from exc

    # ----- paths -------------------------------------------------------------------------
    def job_dir(self, job_id: str) -> Path:
        if not is_valid_job_id(job_id):
            raise JobNotFoundError()
        path = (self.root / job_id).resolve()
        # Defence in depth: the regex already prevents traversal.
        if path.parent != self.root:
            raise JobNotFoundError()
        return path

    def existing_job_dir(self, job_id: str) -> Path:
        path = self.job_dir(job_id)
        if not (path / STATE_NAME).is_file():
            raise JobNotFoundError()
        return path

    def source_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / SOURCE_NAME

    def output_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / OUTPUT_NAME

    # ----- creation ----------------------------------------------------------------------
    def create_job(self, stream: IO[bytes], max_bytes: int) -> tuple[str, Path]:
        """Create a job directory and stream the upload into it. Returns ``(job_id, pdf_path)``."""
        self.ensure_root()
        job_id = new_job_id()
        job_dir = self.job_dir(job_id)
        try:
            job_dir.mkdir(mode=0o750)
            target = job_dir / SOURCE_NAME
            written = 0
            with open(target, "wb") as handle:
                while chunk := stream.read(1024 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise _UploadTooLarge()
                    handle.write(chunk)
        except _UploadTooLarge as exc:
            self.delete_job(job_id)
            raise PDFTooLargeError(f"The file exceeds the {max_bytes // (1024 * 1024)} MB upload limit.") from exc
        except OSError as exc:
            logger.exception("Failed to store upload for job %s", job_id)
            self.delete_job(job_id)
            raise StorageError("The file could not be saved on the server (storage unavailable or full).") from exc
        return job_id, target

    # ----- state -------------------------------------------------------------------------
    def read_state(self, job_id: str) -> dict[str, Any]:
        path = self.existing_job_dir(job_id) / STATE_NAME
        return read_state_file(path)

    def write_state(self, job_id: str, state: dict[str, Any]) -> None:
        write_state_file(self.job_dir(job_id) / STATE_NAME, state)

    def update_state(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with _state_lock:
            state = self.read_state(job_id)
            state.update(changes)
            state["updated_at"] = time.time()
            self.write_state(job_id, state)
            return state

    # ----- deletion ----------------------------------------------------------------------
    def request_cancel(self, job_id: str) -> None:
        with contextlib.suppress(OSError):
            (self.job_dir(job_id) / CANCEL_MARKER).touch()

    def delete_job(self, job_id: str) -> bool:
        path = self.job_dir(job_id)
        if not path.exists():
            return False
        shutil.rmtree(path, ignore_errors=True)
        return not path.exists()

    def iter_job_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return [entry.name for entry in self.root.iterdir() if entry.is_dir() and is_valid_job_id(entry.name)]


class _UploadTooLarge(Exception):
    pass


def read_state_file(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise JobNotFoundError() from exc
    except (OSError, json.JSONDecodeError) as exc:
        logger.exception("Unreadable job state %s", path)
        raise StorageError("Job state could not be read.") from exc


def write_state_file(path: Path, state: dict[str, Any]) -> None:
    """Atomically replace ``path`` with ``state`` (readers never observe a partial file)."""
    directory = path.parent
    if not directory.is_dir():
        # The job was deleted while still running; nothing to record.
        return
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
        os.replace(tmp_name, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
