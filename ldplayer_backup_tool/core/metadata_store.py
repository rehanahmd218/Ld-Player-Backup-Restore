"""
SQLite metadata store.
Tracks every backup record: which file belongs to which instance,
timestamps, checksums, and status.
"""
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "backup_metadata.db")


@dataclass
class BackupRecord:
    id: int
    instance_index: int
    instance_name: str
    backup_path: str
    checksum: str
    timestamp: str
    status: str  # 'success' | 'failed' | 'pending'
    file_size: int = 0
    duration_sec: float = 0.0


class MetadataStore:
    def __init__(self, db_path: str = _DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS backups (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance_index  INTEGER NOT NULL,
                    instance_name   TEXT NOT NULL,
                    backup_path     TEXT NOT NULL,
                    checksum        TEXT DEFAULT '',
                    timestamp       TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    file_size       INTEGER DEFAULT 0,
                    duration_sec    REAL DEFAULT 0.0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_instance ON backups(instance_index)")

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def record_backup(
        self,
        instance_index: int,
        instance_name: str,
        backup_path: str,
        checksum: str,
        timestamp: str,
        status: str,
        file_size: int = 0,
        duration_sec: float = 0.0,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO backups
                   (instance_index, instance_name, backup_path, checksum, timestamp, status, file_size, duration_sec)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (instance_index, instance_name, backup_path, checksum, timestamp, status, file_size, duration_sec),
            )
            return cur.lastrowid

    def update_status(self, record_id: int, status: str, checksum: str = "", file_size: int = 0, duration_sec: float = 0.0):
        with self._conn() as conn:
            conn.execute(
                "UPDATE backups SET status=?, checksum=?, file_size=?, duration_sec=? WHERE id=?",
                (status, checksum, file_size, duration_sec, record_id),
            )

    def delete_record(self, record_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM backups WHERE id=?", (record_id,))

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_latest(self, instance_index: int) -> Optional[BackupRecord]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM backups WHERE instance_index=? AND status='success' ORDER BY timestamp DESC LIMIT 1",
                (instance_index,),
            ).fetchone()
            return self._row_to_record(row) if row else None

    def get_all_latest(self) -> List[BackupRecord]:
        """Return the latest successful backup for each instance."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT b.* FROM backups b
                INNER JOIN (
                    SELECT instance_index, MAX(timestamp) AS mt
                    FROM backups WHERE status='success'
                    GROUP BY instance_index
                ) x ON b.instance_index = x.instance_index AND b.timestamp = x.mt
                ORDER BY b.instance_index
            """).fetchall()
            return [self._row_to_record(r) for r in rows]

    def get_all(self, limit: int = 500) -> List[BackupRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM backups ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    @staticmethod
    def _row_to_record(row) -> BackupRecord:
        return BackupRecord(
            id=row["id"],
            instance_index=row["instance_index"],
            instance_name=row["instance_name"],
            backup_path=row["backup_path"],
            checksum=row["checksum"],
            timestamp=row["timestamp"],
            status=row["status"],
            file_size=row["file_size"],
            duration_sec=row["duration_sec"],
        )
