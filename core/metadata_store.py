"""
SQLite metadata store.
Tracks backup and restore states for instances.
Maintains exactly ONE record per instance_index per operation type.
"""
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
import threading
from typing import List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "metadata.db")


@dataclass
class InstanceRecord:
    instance_index: int
    instance_name: str
    backup_path: str
    checksum: str
    timestamp: str
    status: str  # 'pending', 'completed', 'failed', 'missing', 'checksum_failed', 'cancelled'
    file_size: int = 0
    duration_sec: float = 0.0


class MetadataStore:
    def __init__(self, db_path: str = _DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_db(self):
        with self._conn() as conn:
            # Table for Backups
            conn.execute("""
                CREATE TABLE IF NOT EXISTS backup_state (
                    instance_index  INTEGER PRIMARY KEY,
                    instance_name   TEXT NOT NULL,
                    backup_path     TEXT NOT NULL,
                    checksum        TEXT DEFAULT '',
                    timestamp       TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    file_size       INTEGER DEFAULT 0,
                    duration_sec    REAL DEFAULT 0.0
                )
            """)
            # Table for Restores
            conn.execute("""
                CREATE TABLE IF NOT EXISTS restore_state (
                    instance_index  INTEGER PRIMARY KEY,
                    instance_name   TEXT NOT NULL,
                    backup_path     TEXT NOT NULL,
                    checksum        TEXT DEFAULT '',
                    timestamp       TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    file_size       INTEGER DEFAULT 0,
                    duration_sec    REAL DEFAULT 0.0
                )
            """)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def upsert_backup(
        self,
        instance_index: int,
        instance_name: str,
        backup_path: str,
        checksum: str,
        timestamp: str,
        status: str,
        file_size: int = 0,
        duration_sec: float = 0.0,
    ):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO backup_state
                   (instance_index, instance_name, backup_path, checksum, timestamp, status, file_size, duration_sec)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(instance_index) DO UPDATE SET
                   instance_name=excluded.instance_name,
                   backup_path=excluded.backup_path,
                   checksum=excluded.checksum,
                   timestamp=excluded.timestamp,
                   status=excluded.status,
                   file_size=excluded.file_size,
                   duration_sec=excluded.duration_sec
                """,
                (instance_index, instance_name, backup_path, checksum, timestamp, status, file_size, duration_sec),
            )

    def upsert_restore(
        self,
        instance_index: int,
        instance_name: str,
        backup_path: str,
        checksum: str,
        timestamp: str,
        status: str,
        file_size: int = 0,
        duration_sec: float = 0.0,
    ):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO restore_state
                   (instance_index, instance_name, backup_path, checksum, timestamp, status, file_size, duration_sec)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(instance_index) DO UPDATE SET
                   instance_name=excluded.instance_name,
                   backup_path=excluded.backup_path,
                   checksum=excluded.checksum,
                   timestamp=excluded.timestamp,
                   status=excluded.status,
                   file_size=excluded.file_size,
                   duration_sec=excluded.duration_sec
                """,
                (instance_index, instance_name, backup_path, checksum, timestamp, status, file_size, duration_sec),
            )

    def upsert_backups_batch(self, records: List[dict]):
        if not records:
            return
        with self._conn() as conn:
            conn.executemany(
                """INSERT INTO backup_state
                   (instance_index, instance_name, backup_path, checksum, timestamp, status, file_size, duration_sec)
                   VALUES (:instance_index, :instance_name, :backup_path, :checksum, :timestamp, :status, :file_size, :duration_sec)
                   ON CONFLICT(instance_index) DO UPDATE SET
                   instance_name=excluded.instance_name,
                   backup_path=excluded.backup_path,
                   checksum=excluded.checksum,
                   timestamp=excluded.timestamp,
                   status=excluded.status,
                   file_size=excluded.file_size,
                   duration_sec=excluded.duration_sec
                """,
                records,
            )

    def upsert_restores_batch(self, records: List[dict]):
        if not records:
            return
        with self._conn() as conn:
            conn.executemany(
                """INSERT INTO restore_state
                   (instance_index, instance_name, backup_path, checksum, timestamp, status, file_size, duration_sec)
                   VALUES (:instance_index, :instance_name, :backup_path, :checksum, :timestamp, :status, :file_size, :duration_sec)
                   ON CONFLICT(instance_index) DO UPDATE SET
                   instance_name=excluded.instance_name,
                   backup_path=excluded.backup_path,
                   checksum=excluded.checksum,
                   timestamp=excluded.timestamp,
                   status=excluded.status,
                   file_size=excluded.file_size,
                   duration_sec=excluded.duration_sec
                """,
                records,
            )

    def cancel_pending_backups(self):
        with self._conn() as conn:
            conn.execute("UPDATE backup_state SET status='cancelled' WHERE status='pending'")

    def cancel_pending_restores(self):
        with self._conn() as conn:
            conn.execute("UPDATE restore_state SET status='cancelled' WHERE status='pending'")

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_pending_backups(self) -> List[InstanceRecord]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM backup_state WHERE status='pending' ORDER BY instance_index").fetchall()
            return [self._row_to_record(r) for r in rows]

    def get_pending_restores(self) -> List[InstanceRecord]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM restore_state WHERE status='pending' ORDER BY instance_index").fetchall()
            return [self._row_to_record(r) for r in rows]

    def get_latest_backup(self, instance_index: int) -> Optional[InstanceRecord]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM backup_state WHERE instance_index=?", (instance_index,)).fetchone()
            return self._row_to_record(row) if row else None

    def get_latest_restore(self, instance_index: int) -> Optional[InstanceRecord]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM restore_state WHERE instance_index=?", (instance_index,)).fetchone()
            return self._row_to_record(row) if row else None

    def get_latest_backup_map(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM backup_state").fetchall()
            return {r["instance_index"]: self._row_to_record(r) for r in rows}

    def get_latest_restore_map(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM restore_state").fetchall()
            return {r["instance_index"]: self._row_to_record(r) for r in rows}

    def get_all_latest(self) -> List[InstanceRecord]:
        """Return all backup records"""
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM backup_state ORDER BY instance_index").fetchall()
            return [self._row_to_record(r) for r in rows]

    @staticmethod
    def _row_to_record(row) -> InstanceRecord:
        return InstanceRecord(
            instance_index=row["instance_index"],
            instance_name=row["instance_name"],
            backup_path=row["backup_path"],
            checksum=row["checksum"],
            timestamp=row["timestamp"],
            status=row["status"],
            file_size=row["file_size"],
            duration_sec=row["duration_sec"]
        )
