"""
Dashboard Tab
=============
Overview of the backup system: instance count, last run stats, backup drive usage,
and quick-action buttons.
"""
import os
import shutil
from typing import List, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config.settings import AppSettings
from core.ldconsole import InstanceInfo
from core.metadata_store import MetadataStore
from utils.helpers import format_bytes, now_display


class _StatCard(QFrame):
    """A simple metric card with a large number and a label."""

    def __init__(self, title: str, value: str = "—", color: str = "#58a6ff", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setMinimumHeight(100)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        self._lbl_value = QLabel(value)
        self._lbl_value.setAlignment(Qt.AlignCenter)
        self._lbl_value.setStyleSheet(f"font-size: 32px; font-weight: 800; color: {color};")

        self._lbl_title = QLabel(title)
        self._lbl_title.setAlignment(Qt.AlignCenter)
        self._lbl_title.setStyleSheet("font-size: 12px; color: #8b949e; letter-spacing: 0.5px;")

        layout.addWidget(self._lbl_value)
        layout.addWidget(self._lbl_title)

    def set_value(self, value: str):
        self._lbl_value.setText(value)


class DashboardTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings: Optional[AppSettings] = None
        self._store: Optional[MetadataStore] = None
        self._instances: List[InstanceInfo] = []
        self._init_ui()

        # Auto-refresh drive stats every 30s
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_drive)
        self._timer.start(30_000)

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(16)
        root.setContentsMargins(16, 16, 16, 16)

        # Title
        title = QLabel("Dashboard")
        title.setStyleSheet("font-size: 22px; font-weight: 800; color: #e6edf3;")
        root.addWidget(title)

        sub = QLabel(f"LDPlayer 9 Backup & Restore Tool  •  {now_display()}")
        sub.setObjectName("label_muted")
        root.addWidget(sub)

        # Stat cards row
        grid = QGridLayout()
        grid.setSpacing(12)

        self._card_instances = _StatCard("TOTAL INSTANCES", "—", "#58a6ff")
        self._card_backed_up = _StatCard("BACKED UP", "—", "#3fb950")
        self._card_failed = _StatCard("LAST RUN FAILURES", "—", "#f85149")
        self._card_uptime = _StatCard("LAST RUN", "—", "#d29922")

        grid.addWidget(self._card_instances, 0, 0)
        grid.addWidget(self._card_backed_up, 0, 1)
        grid.addWidget(self._card_failed, 0, 2)
        grid.addWidget(self._card_uptime, 0, 3)
        root.addLayout(grid)

        # Backup drive usage
        drive_group = QGroupBox("Backup Drive")
        drive_layout = QVBoxLayout(drive_group)

        path_row = QHBoxLayout()
        self._lbl_drive_path = QLabel("No backup destination configured.")
        self._lbl_drive_path.setStyleSheet("color: #8b949e;")
        path_row.addWidget(self._lbl_drive_path)
        path_row.addStretch()
        self._lbl_drive_free = QLabel("")
        self._lbl_drive_free.setObjectName("label_muted")
        path_row.addWidget(self._lbl_drive_free)
        drive_layout.addLayout(path_row)

        self._drive_bar = QProgressBar()
        self._drive_bar.setRange(0, 100)
        self._drive_bar.setValue(0)
        self._drive_bar.setFixedHeight(8)
        self._drive_bar.setTextVisible(False)
        drive_layout.addWidget(self._drive_bar)

        root.addWidget(drive_group)

        # Quick actions
        qa_group = QGroupBox("Quick Actions")
        qa_layout = QHBoxLayout(qa_group)
        qa_layout.setSpacing(12)

        self._btn_refresh_instances = QPushButton("↻  Refresh Instances")
        self._btn_refresh_instances.setObjectName("btn_primary")
        qa_layout.addWidget(self._btn_refresh_instances)

        self._btn_go_backup = QPushButton("▶  Start Backup")
        self._btn_go_backup.setObjectName("btn_success")
        qa_layout.addWidget(self._btn_go_backup)

        self._btn_go_restore = QPushButton("⬇  Restore")
        qa_layout.addWidget(self._btn_go_restore)

        qa_layout.addStretch()
        root.addWidget(qa_group)

        # Recent backups table placeholder
        recent_group = QGroupBox("Recent Backup Records")
        recent_layout = QVBoxLayout(recent_group)
        self._lbl_recent = QLabel("No backup records yet.")
        self._lbl_recent.setObjectName("label_muted")
        self._lbl_recent.setAlignment(Qt.AlignCenter)
        recent_layout.addWidget(self._lbl_recent)
        root.addWidget(recent_group, 1)

    # ------------------------------------------------------------------
    def setup(self, settings: AppSettings, store: MetadataStore):
        self._settings = settings
        self._store = store
        self._refresh_drive()
        self.refresh_records()

    def load_instances(self, instances: List[InstanceInfo]):
        self._instances = instances
        self._card_instances.set_value(str(len(instances)))

    def refresh_stats(self, success: int, failed: int):
        self._card_backed_up.set_value(str(success))
        self._card_failed.set_value(str(failed))
        self._card_uptime.set_value(now_display()[:10])

    # ------------------------------------------------------------------
    def _refresh_drive(self):
        if not self._settings or not self._settings.backup_destination:
            return
        dest = self._settings.backup_destination
        self._lbl_drive_path.setText(dest)
        try:
            total, used, free = shutil.disk_usage(dest)
            pct = int(100 * used / total) if total else 0
            self._drive_bar.setValue(pct)
            self._lbl_drive_free.setText(
                f"{format_bytes(free)} free of {format_bytes(total)}  ({pct}% used)"
            )
            # Color bar red when >90% used
            if pct > 90:
                self._drive_bar.setStyleSheet(
                    "QProgressBar::chunk { background-color: #da3633; border-radius: 4px; }"
                )
            elif pct > 75:
                self._drive_bar.setStyleSheet(
                    "QProgressBar::chunk { background-color: #d29922; border-radius: 4px; }"
                )
            else:
                self._drive_bar.setStyleSheet("")
        except Exception:
            self._lbl_drive_free.setText("Could not read drive info.")

    def refresh_records(self):
        """Public — called by MainWindow after backup/restore completes."""
        if not self._store:
            return
        records = self._store.get_all_latest()
        if records:
            self._lbl_recent.setText(
                f"{len(records)} instance(s) have a current backup on record."
            )
            self._card_backed_up.set_value(str(len(records)))
