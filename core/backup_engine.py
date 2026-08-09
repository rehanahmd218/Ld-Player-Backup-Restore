"""
Backup Engine
=============
Manages batch backup of LDPlayer instances using a QThreadPool.

Flow per instance:
  1. Stop instance (if running)
  2. Rename existing backup to .old (safety net)
  3. Run ldconsole backup
  4. Compute SHA-256 checksum
  5. Record to metadata DB
  6. Delete .old file on success (or keep on failure)
  7. Emit signals → UI updates in real-time
"""
import glob
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from config.settings import AppSettings
from core.checksum import compute_sha256
from core.ldconsole import InstanceInfo, LDConsoleWrapper
from core.metadata_store import MetadataStore
from utils.helpers import now_iso, safe_filename
from utils.logger import get_logger

logger = get_logger(__name__)


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    STOPPING = "stopping"
    BACKING_UP = "backing_up"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class BackupJob:
    index: int
    name: str
    status: JobStatus = JobStatus.PENDING
    backup_path: str = ""
    error: str = ""
    started_at: str = ""
    completed_at: str = ""
    duration_sec: float = 0.0
    increment: int = 0   # assigned by BackupManager before the worker starts

    def to_dict(self):
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict):
        d = dict(d)
        d["status"] = JobStatus(d.get("status", "pending"))
        return cls(**d)


# ---------------------------------------------------------------------------
# Per-worker signals (QRunnable can't inherit QObject, so we separate them)
# ---------------------------------------------------------------------------
class WorkerSignals(QObject):
    job_started = pyqtSignal(int, str)                  # index, name
    job_status_changed = pyqtSignal(int, str, str)      # index, status_enum_val, message
    job_progress = pyqtSignal(int, int)                 # index, percent (0-100)
    job_done = pyqtSignal(int, str, float)              # index, backup_path, duration_sec
    job_failed = pyqtSignal(int, str)                   # index, error_message


# ---------------------------------------------------------------------------
# Individual backup worker (runs in QThreadPool thread)
# ---------------------------------------------------------------------------
class BackupWorker(QRunnable):
    def __init__(
        self,
        job: BackupJob,
        ldconsole: LDConsoleWrapper,
        settings: AppSettings,
        store: MetadataStore,
        signals: WorkerSignals,
        cancel_event,           # threading.Event — set when session is cancelled
    ):
        super().__init__()
        self.job = job
        self.ldconsole = ldconsole
        self.settings = settings
        self.store = store
        self.signals = signals
        self._cancel = cancel_event
        self.setAutoDelete(True)

    def _emit_status(self, status: JobStatus, msg: str, pct: int = -1):
        self.signals.job_status_changed.emit(self.job.index, status.value, msg)
        if pct >= 0:
            self.signals.job_progress.emit(self.job.index, pct)

    def run(self):
        # Skip immediately if cancelled before this worker even started
        if self._cancel.is_set():
            self._emit_status(JobStatus.SKIPPED, "Cancelled before start", 0)
            self.signals.job_failed.emit(self.job.index, "Cancelled")
            return
        job = self.job
        start_time = time.time()
        job.started_at = now_iso()

        self.signals.job_started.emit(job.index, job.name)
        self._emit_status(JobStatus.RUNNING, "Starting…", 0)

        # Resolve paths up front so the except block can reference temp_file
        dest_dir = self.settings.backup_destination
        temp_dir = dest_dir + "_temp"   # sibling folder, same drive → rename is atomic
        temp_file: str = ""
        final_file: str = ""

        try:
            # ---- 1. Stop instance ----------------------------------------
            self._emit_status(JobStatus.STOPPING, "Stopping instance…", 5)
            stopped = self.ldconsole.stop_instance(job.index)
            if not stopped:
                raise RuntimeError("Could not stop the instance within timeout.")
            self._emit_status(JobStatus.STOPPING, "Instance stopped", 15)

            # ---- 2. Build paths ------------------------------------------
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            os.makedirs(dest_dir, exist_ok=True)
            os.makedirs(temp_dir, exist_ok=True)

            safe_name = "".join(c for c in job.name if c.isalnum() or c in "-_")

            # Increment is pre-assigned by BackupManager before any worker starts,
            # so it is stable and unique even across concurrent workers.
            # Format: {increment}_{name}_{index}_{timestamp}.ldbk
            filename = f"{job.increment}_{safe_name}_{job.index}_{ts}.ldbk"
            temp_file  = os.path.join(temp_dir, filename)   # 7za writes here
            final_file = os.path.join(dest_dir, filename)   # promoted here on success

            # ---- 3. Run backup → into temp folder ------------------------
            self._emit_status(JobStatus.BACKING_UP, "Backing up…", 20)
            ok, msg = self.ldconsole.backup(job.index, temp_file)
            if not ok:
                raise RuntimeError(f"ldconsole backup failed: {msg}")
            self._emit_status(JobStatus.BACKING_UP, "Backup file created", 75)

            # ---- 4. Verify temp file exists ------------------------------
            if not os.path.exists(temp_file):
                raise RuntimeError("Backup file not found after backup command succeeded.")

            file_size = os.path.getsize(temp_file)

            # ---- 5. Compute checksum ------------------------------------
            self._emit_status(JobStatus.VERIFYING, "Verifying checksum…", 80)
            checksum = compute_sha256(temp_file)
            if not checksum:
                raise RuntimeError("Checksum computation failed.")
            self._emit_status(JobStatus.VERIFYING, "Checksum OK", 95)

            # ---- 6. Delete previous backups for this instance -----------
            # Match the new naming pattern: {increment}_{name}_{index}_{timestamp}.ldbk
            existing_backups = glob.glob(os.path.join(dest_dir, f"*_{safe_name}_{job.index}_*.ldbk"))
            for existing in existing_backups:
                try:
                    os.remove(existing)
                except OSError as e:
                    logger.warning("Could not delete old backup: %s", e)

            # ---- 7. Atomically promote temp → final (same drive = rename)
            # Status is set to DONE only AFTER this succeeds.
            self._emit_status(JobStatus.VERIFYING, "Finalising backup…", 98)
            try:
                os.replace(temp_file, final_file)
            except OSError as e:
                raise RuntimeError(f"Failed to move backup to final location: {e}")

            # ---- 8. Record to DB ----------------------------------------
            duration = time.time() - start_time
            self.store.upsert_backup(
                instance_index=job.index,
                instance_name=job.name,
                backup_path=final_file,
                checksum=checksum,
                timestamp=now_iso(),
                status="success",
                file_size=file_size,
                duration_sec=duration,
            )

            # ---- Done — only reached after successful move ---------------
            job.status = JobStatus.DONE
            job.backup_path = final_file
            job.duration_sec = duration
            job.completed_at = now_iso()
            self._emit_status(JobStatus.DONE, "Backup complete ✓", 100)
            self.signals.job_done.emit(job.index, final_file, duration)
            logger.info("Backup DONE: index=%d  file=%s  %.1fs", job.index, final_file, duration)

        except Exception as exc:
            duration = time.time() - start_time
            error_msg = str(exc)
            job.status = JobStatus.FAILED
            job.error = error_msg
            job.duration_sec = duration
            job.completed_at = now_iso()

            # Rollback: delete partial file from the temp folder
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

            self.store.upsert_backup(
                instance_index=job.index,
                instance_name=job.name,
                backup_path=job.backup_path or "",
                checksum="",
                timestamp=now_iso(),
                status="failed",
                file_size=0,
                duration_sec=duration,
            )

            self._emit_status(JobStatus.FAILED, f"FAILED: {error_msg}", 0)
            self.signals.job_failed.emit(job.index, error_msg)
            logger.error("Backup FAILED: index=%d  error=%s", job.index, error_msg)


# ---------------------------------------------------------------------------
# Backup Manager — orchestrates the queue and thread pool
# ---------------------------------------------------------------------------
class BackupManagerSignals(QObject):
    all_complete = pyqtSignal(int, int, float)   # success_count, fail_count, total_seconds
    queue_progress = pyqtSignal(int, int)         # done, total
    worker_signals: WorkerSignals                 # re-exposed for UI connections


class BackupManager(QObject):
    """
    Manages a pool of BackupWorkers.

    Signals flow:
        BackupManager.signals.worker_signals.* → UI slots for per-instance updates
        BackupManager.signals.all_complete     → UI slot for session summary
        BackupManager.signals.queue_progress   → overall progress bar
    """

    def __init__(
        self,
        jobs: List[BackupJob],
        ldconsole: LDConsoleWrapper,
        settings: AppSettings,
        store: MetadataStore,
        parent=None,
    ):
        super().__init__(parent)
        self._jobs = jobs
        self._ldconsole = ldconsole
        self._settings = settings
        self._store = store

        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(settings.max_concurrency)

        self._done_count = 0
        self._success_count = 0
        self._fail_count = 0
        self._start_time: float = 0.0
        self._total = len(jobs)
        self._cancelled = False
        self._cancel_event = __import__("threading").Event()

        # Signals
        self.signals = BackupManagerSignals()
        self._worker_signals = WorkerSignals()
        self.signals.worker_signals = self._worker_signals

        # Wire worker signals so we can track completion
        self._worker_signals.job_done.connect(self._on_job_done)
        self._worker_signals.job_failed.connect(self._on_job_failed)

    # ------------------------------------------------------------------

    @staticmethod
    def cleanup_stale_backups(dest_dir: str) -> bool:
        """
        Wipes the _temp sibling folder that holds in-progress backup files.
        Returns True if the folder is clean (or didn't exist).
        Returns False if any file is still locked by another process.
        """
        temp_dir = dest_dir + "_temp"
        if not os.path.isdir(temp_dir):
            return True

        all_clear = True
        for fname in os.listdir(temp_dir):
            fpath = os.path.join(temp_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                os.remove(fpath)
                logger.info("Cleanup: Deleted temp backup %s", fname)
            except OSError as e:
                logger.debug("Cleanup: Could not delete %s: %s", fpath, e)
                all_clear = False

        return all_clear

    # ------------------------------------------------------------------

    def start(self):
        if not self._jobs:
            logger.warning("No jobs to process.")
            self.signals.all_complete.emit(0, 0, 0.0)
            return

        self._start_time = time.time()
        logger.info("Starting backup queue: %d jobs, concurrency=%d", self._total, self._settings.max_concurrency)

        dest_dir = self._settings.backup_destination
        os.makedirs(dest_dir, exist_ok=True)

        # Count existing .ldbk files once before any workers start.
        # Each job gets a pre-assigned, unique increment: base+1, base+2, …
        # This is safe even with concurrent workers since increments are assigned here,
        # not computed inside the thread.
        base_count = len(glob.glob(os.path.join(dest_dir, "*.ldbk")))
        logger.info("Backup session: %d existing files in dest → increments start at %d", base_count, base_count + 1)

        job_number = 0
        for job in self._jobs:
            if self._cancelled:
                break
            if job.status in (JobStatus.DONE, JobStatus.SKIPPED):
                self._done_count += 1
                self._success_count += 1
                continue
            job_number += 1
            job.increment = base_count + job_number
            worker = BackupWorker(job, self._ldconsole, self._settings, self._store, self._worker_signals, self._cancel_event)
            self._pool.start(worker)

    def cancel(self):
        """Stop the queue immediately and kill any active ldconsole/7za subprocess."""
        self._cancelled = True
        self._cancel_event.set()
        self._pool.clear()               # drop queued (not yet started) workers
        self._ldconsole.kill_current()   # abort the subprocess blocking a worker thread
        logger.info("Backup queue cancelled.")

    def wait_for_done(self):
        self._pool.waitForDone()

    # ------------------------------------------------------------------
    # Internal tracking
    # ------------------------------------------------------------------

    def _on_job_done(self, index: int, path: str, duration: float):
        self._done_count += 1
        self._success_count += 1
        self.signals.queue_progress.emit(self._done_count, self._total)
        self._check_all_complete()

    def _on_job_failed(self, index: int, error: str):
        self._done_count += 1
        self._fail_count += 1
        self.signals.queue_progress.emit(self._done_count, self._total)
        self._check_all_complete()

    def _check_all_complete(self):
        if self._done_count >= self._total:
            total_time = time.time() - self._start_time
            logger.info(
                "All backups complete: %d success, %d failed, %.1fs total",
                self._success_count, self._fail_count, total_time,
            )
            self.signals.all_complete.emit(self._success_count, self._fail_count, total_time)
