"""
Progress Panel
==============
Scrollable panel of per-instance progress rows.
Each row shows:  index | name | status badge | progress bar | message | elapsed

Used in both the Backup and Restore tabs.
"""
import time
from typing import Dict

from PyQt5.QtCore import Qt, pyqtSlot
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

# Status → (label text, hex colour)
_STATUS_STYLE: Dict[str, tuple] = {
    "pending":    ("Pending",     "#484f58", "#21262d"),
    "running":    ("Running",     "#e6edf3", "#1f6feb"),
    "stopping":   ("Stopping…",  "#e6edf3", "#9e6a03"),
    "backing_up": ("Backing Up", "#e6edf3", "#1f6feb"),
    "verifying":  ("Verifying",  "#e6edf3", "#8957e5"),
    "creating":   ("Creating",   "#e6edf3", "#9e6a03"),
    "restoring":  ("Restoring",  "#e6edf3", "#1f6feb"),
    "done":       ("Done ✓",     "#e6edf3", "#238636"),
    "failed":     ("Failed ✗",   "#e6edf3", "#da3633"),
    "skipped":    ("Skipped",    "#8b949e", "#21262d"),
}


class _InstanceRow(QFrame):
    """Single row widget representing one instance."""

    def __init__(self, index: int, name: str, parent=None):
        super().__init__(parent)
        self.index = index
        self._start_time = None
        self._done = False

        self.setObjectName("card")
        self.setFixedHeight(72)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)

        # Index badge
        lbl_idx = QLabel(f"#{index}")
        lbl_idx.setFixedWidth(40)
        lbl_idx.setAlignment(Qt.AlignCenter)
        lbl_idx.setStyleSheet(
            "color: #8b949e; font-size: 11px; font-weight: 700;"
            "background-color: #21262d; border-radius: 4px; padding: 2px 4px;"
        )
        layout.addWidget(lbl_idx)

        # Name
        self._lbl_name = QLabel(name)
        self._lbl_name.setFixedWidth(200)
        self._lbl_name.setStyleSheet("color: #c9d1d9; font-weight: 600;")
        self._lbl_name.setToolTip(name)
        layout.addWidget(self._lbl_name)

        # Status badge
        self._lbl_status = QLabel("Pending")
        self._lbl_status.setFixedWidth(90)
        self._lbl_status.setAlignment(Qt.AlignCenter)
        self._set_status_badge("pending")
        layout.addWidget(self._lbl_status)

        # Progress bar + message stacked vertically
        vbox = QVBoxLayout()
        vbox.setSpacing(4)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFixedHeight(6)
        self._progress.setTextVisible(False)
        vbox.addWidget(self._progress)

        self._lbl_msg = QLabel("Waiting in queue…")
        self._lbl_msg.setStyleSheet("color: #8b949e; font-size: 11px;")
        vbox.addWidget(self._lbl_msg)

        layout.addLayout(vbox)

        # Elapsed
        self._lbl_elapsed = QLabel("")
        self._lbl_elapsed.setFixedWidth(60)
        self._lbl_elapsed.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._lbl_elapsed.setStyleSheet("color: #484f58; font-size: 11px;")
        layout.addWidget(self._lbl_elapsed)

    def _set_status_badge(self, status_key: str):
        txt, fg, bg = _STATUS_STYLE.get(status_key, ("Unknown", "#c9d1d9", "#21262d"))
        self._lbl_status.setText(txt)
        self._lbl_status.setStyleSheet(
            f"color: {fg}; background-color: {bg}; border-radius: 4px;"
            f"padding: 2px 8px; font-size: 11px; font-weight: 700;"
        )

    def mark_started(self):
        self._start_time = time.time()

    def update_status(self, status_key: str, message: str):
        self._set_status_badge(status_key)
        self._lbl_msg.setText(message)
        if self._start_time:
            elapsed = time.time() - self._start_time
            mins = int(elapsed // 60)
            secs = int(elapsed % 60)
            self._lbl_elapsed.setText(f"{mins:02d}:{secs:02d}")

    def update_progress(self, percent: int):
        self._progress.setValue(percent)
        # Color the bar based on percent
        if percent == 100:
            self._progress.setStyleSheet(
                "QProgressBar::chunk { background-color: #238636; border-radius: 4px; }"
            )
        elif percent > 0:
            self._progress.setStyleSheet(
                "QProgressBar::chunk { background-color: #1f6feb; border-radius: 4px; }"
            )

    def mark_done(self, duration_sec: float):
        self._done = True
        mins = int(duration_sec // 60)
        secs = int(duration_sec % 60)
        self._lbl_elapsed.setText(f"{mins:02d}:{secs:02d}")
        self._lbl_elapsed.setStyleSheet("color: #3fb950; font-size: 11px; font-weight: 600;")

    def mark_failed(self):
        self._progress.setStyleSheet(
            "QProgressBar::chunk { background-color: #da3633; border-radius: 4px; }"
        )
        self._lbl_elapsed.setStyleSheet("color: #f85149; font-size: 11px;")


class ProgressPanel(QWidget):
    """
    Scrollable panel holding one _InstanceRow per job.
    Call setup_jobs() before a run, then connect signals.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: Dict[int, _InstanceRow] = {}
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._container = QWidget()
        self._vbox = QVBoxLayout(self._container)
        self._vbox.setSpacing(4)
        self._vbox.setAlignment(Qt.AlignTop)
        self._vbox.setContentsMargins(0, 0, 0, 0)

        scroll.setWidget(self._container)
        layout.addWidget(scroll)

    def setup_jobs(self, jobs):
        """Populate rows from a list of BackupJob or RestoreJob objects."""
        # Clear existing rows
        for i in reversed(range(self._vbox.count())):
            w = self._vbox.itemAt(i).widget()
            if w:
                w.deleteLater()
        self._rows.clear()

        for job in jobs:
            idx = job.index if hasattr(job, "index") else job.instance_index
            name = job.name if hasattr(job, "name") else job.instance_name
            row = _InstanceRow(idx, name)
            self._rows[idx] = row
            self._vbox.addWidget(row)

    def clear(self):
        for i in reversed(range(self._vbox.count())):
            w = self._vbox.itemAt(i).widget()
            if w:
                w.deleteLater()
        self._rows.clear()

    # ------------------------------------------------------------------
    # Slots (connected to engine signals)
    # ------------------------------------------------------------------

    @pyqtSlot(int, str)
    def on_job_started(self, index: int, name: str):
        if index in self._rows:
            self._rows[index].mark_started()

    @pyqtSlot(int, str, str)
    def on_status_changed(self, index: int, status_val: str, message: str):
        if index in self._rows:
            self._rows[index].update_status(status_val, message)

    @pyqtSlot(int, int)
    def on_progress(self, index: int, percent: int):
        if index in self._rows:
            self._rows[index].update_progress(percent)

    @pyqtSlot(int, str, float)
    def on_job_done(self, index: int, path: str, duration: float):
        if index in self._rows:
            self._rows[index].mark_done(duration)

    @pyqtSlot(int, float)
    def on_restore_done(self, index: int, duration: float):
        if index in self._rows:
            self._rows[index].mark_done(duration)

    @pyqtSlot(int, str)
    def on_job_failed(self, index: int, error: str):
        if index in self._rows:
            self._rows[index].mark_failed()
