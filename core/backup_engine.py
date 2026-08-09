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

Resume: queue state is persisted to queue_state.json.
On next run, already-done jobs are skipped automatically.
"""
import glob
import json
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

_QUEUE_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "queue_state.json"
)


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
    ):
        super().__init__()
        self.job = job
        self.ldconsole = ldconsole
        self.settings = settings
        self.store = store
        self.signals = signals
        self.setAutoDelete(True)

    def _emit_status(self, status: JobStatus, msg: str, pct: int = -1):
        self.signals.job_status_changed.emit(self.job.index, status.value, msg)
        if pct >= 0:
            self.signals.job_progress.emit(self.job.index, pct)

    def run(self):
        job = self.job
        start_time = time.time()
        job.started_at = now_iso()

        self.signals.job_started.emit(job.index, job.name)
        self._emit_status(JobStatus.RUNNING, "Starting…", 0)

        try:
            # ---- 1. Stop instance ----------------------------------------
            self._emit_status(JobStatus.STOPPING, "Stopping instance…", 5)
            stopped = self.ldconsole.stop_instance(job.index)
            if not stopped:
                raise RuntimeError("Could not stop the instance within timeout.")
            self._emit_status(JobStatus.STOPPING, "Instance stopped", 15)

            # ---- 2. Build destination path with timestamp ----------------
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest_dir = os.path.join(
                self.settings.backup_destination,
                f"{safe_filename(job.name)}_{job.index}",
            )
            os.makedirs(dest_dir, exist_ok=True)
            # Filename: InstanceName_YYYYMMDD_HHMMSS.ldbk
            safe_name = safe_filename(job.name)
            backup_file = os.path.join(dest_dir, f"{safe_name}_{ts}.ldbk")

            # ---- 3. Rename ALL existing .ldbk files → .old ---------------
            existing_backups = glob.glob(os.path.join(dest_dir, "*.ldbk"))
            for existing in existing_backups:
                old_path = existing + ".old"
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
                try:
                    os.rename(existing, old_path)
                    logger.debug("Renamed %s → .old", os.path.basename(existing))
                except OSError as e:
                    logger.warning("Could not rename existing backup: %s", e)

            # ---- 4. Run backup --------------------------------------------
            self._emit_status(JobStatus.BACKING_UP, "Backing up…", 20)
            ok, msg = self.ldconsole.backup(job.index, backup_file)
            if not ok:
                raise RuntimeError(f"ldconsole backup failed: {msg}")
            self._emit_status(JobStatus.BACKING_UP, "Backup file created", 75)

            # ---- 5. Verify backup file exists -----------------------------
            if not os.path.exists(backup_file):
                raise RuntimeError("Backup file not found after backup command succeeded.")

            file_size = os.path.getsize(backup_file)

            # ---- 6. Compute checksum -------------------------------------
            self._emit_status(JobStatus.VERIFYING, "Verifying checksum…", 80)
            checksum = compute_sha256(backup_file)
            if not checksum:
                raise RuntimeError("Checksum computation failed.")
            self._emit_status(JobStatus.VERIFYING, "Checksum OK", 95)

            # ---- 7. Record to DB -----------------------------------------
            duration = time.time() - start_time
            self.store.record_backup(
                instance_index=job.index,
                instance_name=job.name,
                backup_path=backup_file,
                checksum=checksum,
                timestamp=now_iso(),
                status="success",
                file_size=file_size,
                duration_sec=duration,
            )

            # ---- 8. Delete ALL .old files on success --------------------
            old_files = glob.glob(os.path.join(dest_dir, "*.old"))
            for old_f in old_files:
                try:
                    os.remove(old_f)
                    logger.debug("Deleted old backup: %s", os.path.basename(old_f))
                except OSError as e:
                    logger.warning("Could not delete .old for index %d: %s", job.index, e)

            # ---- Done ----------------------------------------------------
            job.status = JobStatus.DONE
            job.backup_path = backup_file
            job.duration_sec = duration
            job.completed_at = now_iso()
            self._emit_status(JobStatus.DONE, "Backup complete ✓", 100)
            self.signals.job_done.emit(job.index, backup_file, duration)
            logger.info("Backup DONE: index=%d  file=%s  %.1fs", job.index, backup_file, duration)

        except Exception as exc:
            duration = time.time() - start_time
            error_msg = str(exc)
            job.status = JobStatus.FAILED
            job.error = error_msg
            job.duration_sec = duration
            job.completed_at = now_iso()

            # Keep .old file on failure (safety net)
            self.store.record_backup(
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

        # Signals
        self.signals = BackupManagerSignals()
        self._worker_signals = WorkerSignals()
        self.signals.worker_signals = self._worker_signals

        # Wire worker signals so we can track completion
        self._worker_signals.job_done.connect(self._on_job_done)
        self._worker_signals.job_failed.connect(self._on_job_failed)

    # ------------------------------------------------------------------
    # Queue persistence (resume support)
    # ------------------------------------------------------------------

    def _save_queue(self):
        try:
            data = {
                "jobs": [j.to_dict() for j in self._jobs],
                "saved_at": now_iso(),
            }
            with open(_QUEUE_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Could not save queue state: %s", e)

    @staticmethod
    def load_resumable_jobs() -> Optional[List[BackupJob]]:
        """Return pending jobs from a previous interrupted run, or None."""
        if not os.path.exists(_QUEUE_STATE_FILE):
            return None
        try:
            with open(_QUEUE_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            all_jobs = [BackupJob.from_dict(d) for d in data.get("jobs", [])]
            pending = [j for j in all_jobs if j.status not in (JobStatus.DONE, JobStatus.SKIPPED)]
            if pending:
                logger.info("Resume: %d pending jobs found in queue_state.json", len(pending))
                return pending
        except Exception as e:
            logger.warning("Could not load queue state: %s", e)
        return None

    @staticmethod
    def clear_queue_state():
        if os.path.exists(_QUEUE_STATE_FILE):
            os.remove(_QUEUE_STATE_FILE)

    # ------------------------------------------------------------------
    # Run / Cancel
    # ------------------------------------------------------------------

    def start(self):
        if not self._jobs:
            logger.warning("No jobs to process.")
            self.signals.all_complete.emit(0, 0, 0.0)
            return

        self._start_time = time.time()
        self._save_queue()
        logger.info("Starting backup queue: %d jobs, concurrency=%d", self._total, self._settings.max_concurrency)

        for job in self._jobs:
            if self._cancelled:
                break
            if job.status in (JobStatus.DONE, JobStatus.SKIPPED):
                self._done_count += 1
                self._success_count += 1
                continue
            worker = BackupWorker(job, self._ldconsole, self._settings, self._store, self._worker_signals)
            self._pool.start(worker)

    def cancel(self):
        self._cancelled = True
        self._pool.clear()
        logger.info("Backup queue cancelled.")

    def wait_for_done(self):
        self._pool.waitForDone()

    # ------------------------------------------------------------------
    # Internal tracking
    # ------------------------------------------------------------------

    def _on_job_done(self, index: int, path: str, duration: float):
        self._done_count += 1
        self._success_count += 1
        self._save_queue()
        self.signals.queue_progress.emit(self._done_count, self._total)
        self._check_all_complete()

    def _on_job_failed(self, index: int, error: str):
        self._done_count += 1
        self._fail_count += 1
        self._save_queue()
        self.signals.queue_progress.emit(self._done_count, self._total)
        self._check_all_complete()

    def _check_all_complete(self):
        if self._done_count >= self._total:
            total_time = time.time() - self._start_time
            logger.info(
                "All backups complete: %d success, %d failed, %.1fs total",
                self._success_count, self._fail_count, total_time,
            )
            self.clear_queue_state()
            self.signals.all_complete.emit(self._success_count, self._fail_count, total_time)
