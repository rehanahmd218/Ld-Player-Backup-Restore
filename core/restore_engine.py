"""
Restore Engine
==============
Handles single and bulk restore of LDPlayer instances.

Flow per instance:
  1. Check if target instance exists (via current instance list)
  2. If NOT exists → run ldconsole add (create empty instance)
  3. Run ldconsole restore
  4. Emit real-time signals for UI updates
"""
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import List

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from config.settings import AppSettings
from core.checksum import compute_sha256
from core.ldconsole import InstanceInfo, LDConsoleWrapper
from core.metadata_store import InstanceRecord, MetadataStore
import os
from utils.helpers import now_iso
from utils.logger import get_logger

logger = get_logger(__name__)


class RestoreStatus(str, Enum):
    PENDING = "pending"
    CREATING = "creating"
    RESTORING = "restoring"
    DONE = "done"
    FAILED = "failed"


@dataclass
class RestoreJob:
    instance_index: int
    instance_name: str
    backup_path: str
    status: RestoreStatus = RestoreStatus.PENDING
    checksum: str = ""
    error: str = ""
    duration_sec: float = 0.0


class RestoreWorkerSignals(QObject):
    job_started = pyqtSignal(int, str)              # index, name
    job_status_changed = pyqtSignal(int, str, str)  # index, status_val, message
    job_done = pyqtSignal(int, float)               # index, duration_sec
    job_failed = pyqtSignal(int, str)               # index, error


class RestoreWorker(QRunnable):
    # Lock to prevent race conditions when multiple workers try to add instances concurrently
    _add_lock = threading.Lock()

    def __init__(
        self,
        job: RestoreJob,
        ldconsole: LDConsoleWrapper,
        signals: RestoreWorkerSignals,
        add_lock: threading.Lock,
        store: MetadataStore,
    ):
        super().__init__()
        self.job = job
        self.ldconsole = ldconsole
        self.signals = signals
        self._add_lock = add_lock
        self.store = store
        self.setAutoDelete(True)

    def _emit(self, status: RestoreStatus, msg: str):
        # We always emit signals using the ORIGINAL job index so the UI row matches
        self.signals.job_status_changed.emit(self.job.instance_index, status.value, msg)

    def run(self):
        job = self.job
        start = time.time()
        self.signals.job_started.emit(job.instance_index, job.instance_name)
        self._emit(RestoreStatus.PENDING, "Starting restore…")

        try:
            # ---- 1. Verify Checksum --------------------------
            if self.job.checksum:
                self._emit(RestoreStatus.PENDING, "Verifying checksum…")
                actual_checksum = compute_sha256(job.backup_path)
                if not actual_checksum:
                    raise RuntimeError("Failed to compute checksum for backup file.")
                if actual_checksum != job.checksum:
                    # Write to mismatch file
                    mismatch_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "checksum_mismatches.txt")
                    with open(mismatch_file, "a", encoding="utf-8") as f:
                        f.write(f"[{now_iso()}] Mismatch for {job.instance_name} (Index {job.instance_index})\n")
                        f.write(f"  File: {job.backup_path}\n")
                        f.write(f"  Expected: {job.checksum}\n")
                        f.write(f"  Actual:   {actual_checksum}\n\n")
                    self.store.upsert_restore(
                        instance_index=job.instance_index,
                        instance_name=job.instance_name,
                        backup_path=job.backup_path,
                        checksum=actual_checksum,
                        timestamp=now_iso(),
                        status="checksum_failed",
                        file_size=os.path.getsize(job.backup_path) if os.path.exists(job.backup_path) else 0,
                        duration_sec=0.0
                    )
                    raise RuntimeError("Checksum mismatch! Restore aborted for safety.")

            # Fetch live list of instances to avoid stale data
            existing_indices = {i.index for i in self.ldconsole.list_instances()}
            target_index = job.instance_index

            if target_index not in existing_indices:
                # Need to create the instance first
                self._emit(RestoreStatus.CREATING, f"Instance not found — creating…")
                
                with self._add_lock:
                    before_indices = {i.index for i in self.ldconsole.list_instances()}
                    
                    ok, msg = self.ldconsole.add_instance(job.instance_name)
                    if not ok:
                        raise RuntimeError(f"Could not create instance: {msg}")
                    
                    # Determine the actual index assigned by LDConsole
                    after_instances = self.ldconsole.list_instances()
                    new_indices = [i.index for i in after_instances if i.index not in before_indices]
                    
                    if new_indices:
                        target_index = new_indices[0]
                        logger.info("Created new instance: got actual idx=%d (requested name=%s)", target_index, job.instance_name)
                    else:
                        logger.warning("Could not identify new index, attempting fallback to original idx %d", target_index)

                self._emit(RestoreStatus.CREATING, "Instance created")
            else:
                self._emit(RestoreStatus.RESTORING, "Instance exists — overwriting…")

            # Run restore using the TARGET index (which might be different from the job index if newly created)
            self._emit(RestoreStatus.RESTORING, "Restoring backup…")
            ok, msg = self.ldconsole.restore(target_index, job.backup_path)
            
            # If it failed because the player doesn't exist (e.g. deleted in background), try creating it now as a fallback
            if not ok and "player don't exist" in msg.lower():
                logger.warning("Restore failed with 'player don't exist' for idx=%d. Attempting fallback creation.", target_index)
                self._emit(RestoreStatus.CREATING, "Instance actually missing — creating…")
                with self._add_lock:
                    before_indices = {i.index for i in self.ldconsole.list_instances()}
                    add_ok, add_msg = self.ldconsole.add_instance(job.instance_name)
                    if not add_ok:
                        raise RuntimeError(f"Fallback creation failed: {add_msg}")
                    after_instances = self.ldconsole.list_instances()
                    new_indices = [i.index for i in after_instances if i.index not in before_indices]
                    if new_indices:
                        target_index = new_indices[0]
                        logger.info("Fallback created new instance: got actual idx=%d", target_index)
                
                self._emit(RestoreStatus.RESTORING, "Retrying restore on new instance…")
                ok, msg = self.ldconsole.restore(target_index, job.backup_path)

            if not ok:
                raise RuntimeError(f"ldconsole restore failed: {msg}")

            duration = time.time() - start
            job.status = RestoreStatus.DONE
            job.duration_sec = duration
            
            # Record success
            file_size = os.path.getsize(job.backup_path) if os.path.exists(job.backup_path) else 0
            self.store.upsert_restore(
                instance_index=job.instance_index,
                instance_name=job.instance_name,
                backup_path=job.backup_path,
                checksum=job.checksum or "",
                timestamp=now_iso(),
                status="completed",
                file_size=file_size,
                duration_sec=duration
            )

            self._emit(RestoreStatus.DONE, "Restore complete ✓")
            self.signals.job_done.emit(job.instance_index, duration)
            logger.info("Restore DONE: orig_idx=%d target_idx=%d %.1fs", job.instance_index, target_index, duration)

        except Exception as exc:
            duration = time.time() - start
            error_msg = str(exc)
            job.status = RestoreStatus.FAILED
            job.error = error_msg
            job.duration_sec = duration
            
            if "Checksum mismatch" not in error_msg:
                # We already recorded the checksum failure above
                file_size = os.path.getsize(job.backup_path) if os.path.exists(job.backup_path) else 0
                self.store.upsert_restore(
                    instance_index=job.instance_index,
                    instance_name=job.instance_name,
                    backup_path=job.backup_path,
                    checksum=job.checksum or "",
                    timestamp=now_iso(),
                    status="failed",
                    file_size=file_size,
                    duration_sec=duration
                )

            self._emit(RestoreStatus.FAILED, f"FAILED: {error_msg}")
            self.signals.job_failed.emit(job.instance_index, error_msg)
            logger.error("Restore FAILED: idx=%d  error=%s", job.instance_index, error_msg)


class RestoreManagerSignals(QObject):
    all_complete = pyqtSignal(int, int, float)   # success, failed, total_seconds
    queue_progress = pyqtSignal(int, int)         # done, total


class RestoreManager(QObject):
    def __init__(self, jobs: List[RestoreJob], ldconsole: LDConsoleWrapper, settings: AppSettings, store: MetadataStore, parent=None):
        super().__init__(parent)
        self._jobs = jobs
        self._ldconsole = ldconsole
        self._settings = settings
        self._store = store
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(max(1, settings.max_concurrency // 2))  # gentler for restore
        self._add_lock = threading.Lock()

        self._total = len(jobs)
        self._done = 0
        self._success = 0
        self._fail = 0
        self._start_time = 0.0

        self.signals = RestoreManagerSignals()
        self.worker_signals = RestoreWorkerSignals()
        self.worker_signals.job_done.connect(self._on_done)
        self.worker_signals.job_failed.connect(self._on_failed)

    def start(self):
        self._start_time = time.time()
        for job in self._jobs:
            w = RestoreWorker(job, self._ldconsole, self.worker_signals, self._add_lock, self._store)
            self._pool.start(w)

    def _on_done(self, idx: int, dur: float):
        self._done += 1
        self._success += 1
        self.signals.queue_progress.emit(self._done, self._total)
        self._check_complete()

    def _on_failed(self, idx: int, err: str):
        self._done += 1
        self._fail += 1
        self.signals.queue_progress.emit(self._done, self._total)
        self._check_complete()

    def _check_complete(self):
        if self._done >= self._total:
            total = time.time() - self._start_time
            self.signals.all_complete.emit(self._success, self._fail, total)
