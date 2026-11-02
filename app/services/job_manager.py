"""Background execution of conversion jobs.

Each conversion runs in its own process (``spawn`` start method), dispatched from a bounded
thread pool. Running conversions out of process means:

* PyMuPDF (which is not thread-safe) never runs concurrently inside the web process;
* a hard timeout can be enforced by terminating the process;
* a crash on a malformed PDF cannot take the web server down;
* all memory used by a conversion is returned to the OS when it ends.

Limitations: the queue lives in the web process, so queued jobs are lost if that process
restarts (they are reported as failed by the status endpoint/cleanup). For multi-host or
high-volume deployments replace this with a task queue such as Celery + Redis.
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.services.conversion_service import initial_stages, run_job_process
from app.utils.file_handler import TERMINAL_STATUSES, JobStorage
from app.utils.validators import ConversionOptions

logger = logging.getLogger(__name__)

KILL_GRACE_SECONDS = 15


class JobManager:
    def __init__(self, storage: JobStorage, settings: dict[str, Any], max_workers: int) -> None:
        self.storage = storage
        self.settings = settings
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="conversion")
        self._processes: dict[str, multiprocessing.process.BaseProcess] = {}
        self._lock = threading.Lock()
        self._context = multiprocessing.get_context("spawn")

    def submit(self, job_id: str, options: ConversionOptions) -> dict[str, Any]:
        state = self.storage.update_state(
            job_id,
            status="queued",
            stage=None,
            message="Waiting for a free conversion slot",
            options=options.to_dict(),
            output_filename=options.output_filename,
            stages=initial_stages(),
            warnings=[],
            validation=None,
            result=None,
            error_code=None,
            queued_at=time.time(),
        )
        self._executor.submit(self._run, job_id, options)
        return state

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            process = self._processes.get(job_id)
            return process is not None and process.is_alive()

    def cancel(self, job_id: str) -> None:
        """Ask a job to stop; terminate its process if this worker owns it."""
        self.storage.request_cancel(job_id)
        with self._lock:
            process = self._processes.get(job_id)
        if process is not None and process.is_alive():
            process.terminate()

    def _run(self, job_id: str, options: ConversionOptions) -> None:
        try:
            job_dir = self.storage.job_dir(job_id)
            if not job_dir.exists():
                return  # deleted while queued
            state = self.storage.read_state(job_id)
            if state.get("status") != "queued":
                return
            process = self._context.Process(
                target=run_job_process,
                args=(str(job_dir), options.to_dict(), self.settings),
                name=f"convert-{job_id[:8]}",
                daemon=True,
            )
            with self._lock:
                self._processes[job_id] = process
            process.start()
            process.join(self.settings["timeout"] + KILL_GRACE_SECONDS)
            if process.is_alive():
                logger.error("Job %s exceeded the timeout; terminating", job_id)
                process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join(5)
                self._mark_failed(job_id, "The conversion took too long and was stopped. Try a smaller PDF.", "timeout")
            elif process.exitcode != 0:
                logger.error("Conversion process for job %s exited with code %s", job_id, process.exitcode)
                self._mark_failed(job_id, "The conversion process stopped unexpectedly.", "conversion_failed")
            else:
                self._mark_failed(job_id, "The conversion ended without a result.", "conversion_failed")
        except Exception:
            logger.exception("Job runner failed for %s", job_id)
            self._mark_failed(job_id, "An unexpected error occurred while converting the PDF.", "conversion_failed")
        finally:
            with self._lock:
                self._processes.pop(job_id, None)

    def _mark_failed(self, job_id: str, message: str, code: str) -> None:
        """Record a failure unless the conversion process already recorded a final status."""
        try:
            state = self.storage.read_state(job_id)
        except Exception:
            return  # job deleted
        if state.get("status") in TERMINAL_STATUSES:
            return
        if (self.storage.job_dir(job_id) / "cancel").exists():
            message, code = "The conversion was cancelled.", "cancelled"
        stages = state.get("stages") or []
        for stage in stages:
            if stage.get("status") == "running":
                stage["status"] = "failed"
        self.storage.update_state(
            job_id,
            status="cancelled" if code == "cancelled" else "failed",
            message=message,
            error_code=code,
            stages=stages,
            finished_at=time.time(),
        )

    def shutdown(self) -> None:
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            if process.is_alive():
                process.terminate()
        self._executor.shutdown(wait=False, cancel_futures=True)
