"""Removal of expired job directories."""

from __future__ import annotations

import logging
import threading
import time

from app.utils.file_handler import ACTIVE_STATUSES, STATE_NAME, JobStorage, read_state_file

logger = logging.getLogger(__name__)

# A job that has been "processing" for this long past its timeout is considered orphaned
# (for example, the server restarted mid-conversion).
ORPHAN_GRACE_SECONDS = 600


def cleanup_expired_jobs(storage: JobStorage, ttl_seconds: int, conversion_timeout: int) -> int:
    """Delete jobs not updated within ``ttl_seconds``. Active jobs are kept until clearly orphaned."""
    now = time.time()
    removed = 0
    for job_id in storage.iter_job_ids():
        job_dir = storage.job_dir(job_id)
        try:
            state = read_state_file(job_dir / STATE_NAME)
            last_update = float(state.get("updated_at") or state.get("created_at") or 0)
            status = state.get("status")
        except Exception:
            # Unreadable/missing state (e.g. an interrupted upload): fall back to the directory mtime.
            try:
                last_update = job_dir.stat().st_mtime
            except OSError:
                continue
            status = None

        age = now - last_update
        if status in ACTIVE_STATUSES and age < conversion_timeout + ORPHAN_GRACE_SECONDS:
            continue
        if age >= ttl_seconds and storage.delete_job(job_id):
            removed += 1
            logger.info("Removed expired job %s (age %.0fs)", job_id, age)
    return removed


class CleanupThread(threading.Thread):
    """Daemon thread that periodically removes expired jobs."""

    def __init__(self, storage: JobStorage, ttl_seconds: int, interval_seconds: int, conversion_timeout: int) -> None:
        super().__init__(name="job-cleanup", daemon=True)
        self.storage = storage
        self.ttl_seconds = ttl_seconds
        self.interval_seconds = max(30, interval_seconds)
        self.conversion_timeout = conversion_timeout
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                cleanup_expired_jobs(self.storage, self.ttl_seconds, self.conversion_timeout)
            except Exception:
                logger.exception("Job cleanup failed")

    def stop(self) -> None:
        self._stop_event.set()
