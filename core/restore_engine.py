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
        cancel_event,                       # threading.Event
        instance_snapshot: dict,            # pre-fetched {name: index} from RestoreManager
    ):
        super().__init__()
        self.job = job
        self.ldconsole = ldconsole
        self.signals = signals
        self._add_lock = add_lock
        self.store = store
        self._cancel = cancel_event
        self._snapshot = instance_snapshot  # shared read-only snapshot; no lock needed
        self.setAutoDelete(True)

    def _emit(self, status: RestoreStatus, msg: str):
        # We always emit signals using the ORIGINAL job index so the UI row matches
        self.signals.job_status_changed.emit(self.job.instance_index, status.value, msg)

    def run(self):
        # Skip immediately if cancelled before this worker even started
        if self._cancel.is_set():
            self._emit(RestoreStatus.FAILED, "Cancelled")
            self.signals.job_failed.emit(self.job.instance_index, "Cancelled")
            return
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

            # ---- 2. Resolve instance by NAME using the pre-fetched snapshot ----------
            # No subprocess call needed here — the manager fetched the list once before
            # any workers started.
            instance_name = job.instance_name
            target_index = self._snapshot.get(instance_name, -1)

            if target_index == -1:
                # Instance not in the snapshot — need to create it
                self._emit(RestoreStatus.CREATING, "Instance not found — creating…")

                with self._add_lock:
                    add_ok, add_msg = self.ldconsole.add_instance(instance_name)
                    if not add_ok:
                        raise RuntimeError(f"Could not create instance: {add_msg}")
                    # Re-query once after creation to find the newly assigned index
                    target_index = self.ldconsole.get_index_by_name(instance_name)
                    if target_index == -1:
                        raise RuntimeError(f"Created instance '{instance_name}' but could not find its index.")
                    logger.info("Created new instance '%s' → index %d", instance_name, target_index)

                self._emit(RestoreStatus.CREATING, "Instance created")
            else:
                self._emit(RestoreStatus.RESTORING, "Instance exists — overwriting…")

            # ---- 3. Stop instance if running and wait 2.5s for stabilization --------
            if self.ldconsole.is_running(target_index):
                self._emit(RestoreStatus.RESTORING, "Stopping instance before restore…")
                stopped = self.ldconsole.stop_instance(target_index)
                if not stopped:
                    raise RuntimeError(f"Could not stop instance {target_index} prior to restore.")
                self._emit(RestoreStatus.RESTORING, "Waiting for process to stabilize…")
                time.sleep(2.5)

            # ---- 4. Restore directly by the resolved index -----------------------
            self._emit(RestoreStatus.RESTORING, "Restoring backup…")
            ok, msg = self.ldconsole.restore(target_index, job.backup_path)

            # Fallback: if ldconsole says the player doesn't exist, create and retry once
            if not ok and ("player don't exist" in msg.lower() or "not found" in msg.lower()):
                logger.warning("Restore failed for '%s' (idx=%d). Attempting fallback creation.", instance_name, target_index)
                self._emit(RestoreStatus.CREATING, "Instance actually missing — creating…")
                with self._add_lock:
                    add_ok, add_msg = self.ldconsole.add_instance(instance_name)
                    if not add_ok:
                        raise RuntimeError(f"Fallback creation failed: {add_msg}")
                    target_index = self.ldconsole.get_index_by_name(instance_name)
                    if target_index == -1:
                        raise RuntimeError(f"Fallback: created '{instance_name}' but could not find its index.")
                    logger.info("Fallback created '%s' → index %d", instance_name, target_index)

                time.sleep(2.5)
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
            logger.info("Restore DONE: name='%s' orig_idx=%d  %.1fs", job.instance_name, job.instance_index, duration)

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
        self._cancel_event = __import__("threading").Event()

        self.signals = RestoreManagerSignals()
        self.worker_signals = RestoreWorkerSignals()
        self.worker_signals.job_done.connect(self._on_done)
        self.worker_signals.job_failed.connect(self._on_failed)

    def start(self):
        self._start_time = time.time()

        # Fetch the live instance list ONCE for the entire session.
        # Each worker receives this snapshot and does a plain dict lookup
        # instead of calling list_instances() individually.
        logger.info("Fetching live instance list for restore session…")
        live = self._ldconsole.list_instances()
        snapshot = {i.name: i.index for i in live}
        logger.info("Snapshot ready: %d instances", len(snapshot))

        for job in self._jobs:
            w = RestoreWorker(
                job, self._ldconsole, self.worker_signals,
                self._add_lock, self._store, self._cancel_event,
                instance_snapshot=snapshot,
            )
            self._pool.start(w)

    def cancel(self):
        """Stop the queue and kill any active ldconsole subprocess."""
        self._cancel_event.set()
        self._pool.clear()
        self._ldconsole.kill_current()
        logger.info("Restore queue cancelled.")

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
