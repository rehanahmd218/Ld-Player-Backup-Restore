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
            dest_dir = self.settings.backup_destination
            os.makedirs(dest_dir, exist_ok=True)
            safe_name = "".join(c for c in job.name if c.isalnum() or c in "-_")
            final_filename = f"{job.index}_{safe_name}_{ts}.ldbk"
            final_backup_file = os.path.join(dest_dir, final_filename)
            
            # Use a temporary extension during the backup process
            temp_filename = f"{job.index}_{safe_name}_{ts}.tmp.ldbk"
            backup_file = os.path.join(dest_dir, temp_filename)

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

            # ---- 7. Delete previous good backups for this instance --------
            existing_backups = glob.glob(os.path.join(dest_dir, f"{job.index}_*.ldbk"))
            for existing in existing_backups:
                if existing != backup_file:  # Don't delete our temp file yet
                    try:
                        os.remove(existing)
                    except OSError as e:
                        logger.warning("Could not delete old backup: %s", e)
                        
            # ---- 8. Rename temporary backup to final ----------------------
            try:
                os.rename(backup_file, final_backup_file)
            except OSError as e:
                raise RuntimeError(f"Failed to finalize backup file: {e}")

            # ---- 9. Record to DB -----------------------------------------
            duration = time.time() - start_time
            self.store.upsert_backup(
                instance_index=job.index,
                instance_name=job.name,
                backup_path=final_backup_file,
                checksum=checksum,
                timestamp=now_iso(),
                status="success",
                file_size=file_size,
                duration_sec=duration,
            )

            # ---- Done ----------------------------------------------------
            job.status = JobStatus.DONE
            job.backup_path = final_backup_file
            job.duration_sec = duration
            job.completed_at = now_iso()
            self._emit_status(JobStatus.DONE, "Backup complete ✓", 100)
            self.signals.job_done.emit(job.index, final_backup_file, duration)
            logger.info("Backup DONE: index=%d  file=%s  %.1fs", job.index, final_backup_file, duration)

        except Exception as exc:
            duration = time.time() - start_time
            error_msg = str(exc)
            job.status = JobStatus.FAILED
            job.error = error_msg
            job.duration_sec = duration
            job.completed_at = now_iso()

            # Rollback: delete partial temporary file
            if os.path.exists(backup_file):
                try:
                    os.remove(backup_file)
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

        # Signals
        self.signals = BackupManagerSignals()
        self._worker_signals = WorkerSignals()
        self.signals.worker_signals = self._worker_signals

        # Wire worker signals so we can track completion
        self._worker_signals.job_done.connect(self._on_job_done)
        self._worker_signals.job_failed.connect(self._on_job_failed)

    # ------------------------------------------------------------------

    # Run / Cancel
    @staticmethod
    def cleanup_stale_backups(dest_dir: str):
        """Clean up any temporary backup files left over from a crash."""
        if not os.path.isdir(dest_dir):
            return
            
        # Kill any lingering ldconsole.exe processes that might be locking the .tmp files
        import subprocess
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "ldconsole.exe"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            logger.debug("Cleanup: Killed lingering ldconsole.exe processes.")
        except Exception as e:
            logger.warning("Cleanup: Failed to kill ldconsole.exe: %s", e)
            
        # Clean up the new .tmp.ldbk files
        temp_files = glob.glob(os.path.join(dest_dir, "*.tmp.ldbk"))
        for temp_f in temp_files:
            try:
                os.remove(temp_f)
                logger.info("Cleanup: Deleted partial/interrupted backup %s", os.path.basename(temp_f))
            except OSError as e:
                logger.warning("Cleanup: Could not delete %s: %s", temp_f, e)
                
        # Also clean up any legacy .old files just in case
        old_files = glob.glob(os.path.join(dest_dir, "*.old"))
        for old_f in old_files:
            try:
                os.rename(old_f, old_f[:-4])
                logger.info("Cleanup: Restored legacy good backup %s", os.path.basename(old_f[:-4]))
            except OSError as e:
                logger.warning("Cleanup: Could not restore legacy %s: %s", old_f, e)

    # ------------------------------------------------------------------

    def start(self):
        if not self._jobs:
            logger.warning("No jobs to process.")
            self.signals.all_complete.emit(0, 0, 0.0)
            return

        self._start_time = time.time()
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
