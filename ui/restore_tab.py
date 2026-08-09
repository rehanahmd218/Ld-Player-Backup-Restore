"""
Restore Tab
===========
• Table of all latest backups from the metadata DB
• Filter by instance name or index range
• Multi-select with helpers (Select All / Clear / Select Visible)
• Single or bulk restore with real-time progress
• Auto-creates missing instances before restoring
"""
from typing import List, Optional

from PyQt5.QtCore import Qt, pyqtSlot
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config.settings import AppSettings
from core.ldconsole import InstanceInfo, LDConsoleWrapper
from core.metadata_store import BackupRecord, MetadataStore
from core.restore_engine import RestoreJob, RestoreManager
from notifications.discord_notifier import send_session_summary
from ui.widgets.progress_panel import ProgressPanel
from utils.helpers import format_bytes, format_duration, parse_index_range


class RestoreTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._ldconsole: Optional[LDConsoleWrapper] = None
        self._settings: Optional[AppSettings] = None
        self._store: Optional[MetadataStore] = None
        self._instances: List[InstanceInfo] = []
        self._records: List[BackupRecord] = []
        self._manager: Optional[RestoreManager] = None
        self._init_ui()

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        # Header
        hdr = QHBoxLayout()
        title = QLabel("Restore Instances")
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #e6edf3;")
        hdr.addWidget(title)
        hdr.addStretch()
        btn_refresh = QPushButton("↻  Refresh")
        btn_refresh.clicked.connect(self._load_records)
        hdr.addWidget(btn_refresh)
        root.addLayout(hdr)

        sub = QLabel(
            "Select one or more backups. If the instance no longer exists, it will be created automatically."
        )
        sub.setStyleSheet("color: #8b949e; font-size: 12px;")
        root.addWidget(sub)

        # --- Backup list with filters ---------------------------------
        tbl_group = QGroupBox("Available Backups")
        tl = QVBoxLayout(tbl_group)
        tl.setSpacing(6)

        # Filter bar
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Index range:"))
        self._range_input = QLineEdit()
        self._range_input.setPlaceholderText("e.g. 0-50")
        self._range_input.setMaximumWidth(160)
        filter_row.addWidget(self._range_input)

        filter_row.addWidget(QLabel("Name filter:"))
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("e.g. emulator")
        self._name_input.setMaximumWidth(160)
        filter_row.addWidget(self._name_input)

        btn_apply = QPushButton("Apply Filter")
        btn_apply.clicked.connect(self._apply_filter)
        filter_row.addWidget(btn_apply)

        filter_row.addStretch()

        self._lbl_sel = QLabel("0 selected")
        self._lbl_sel.setStyleSheet("color: #8b949e; font-size: 12px;")
        filter_row.addWidget(self._lbl_sel)
        tl.addLayout(filter_row)

        # Selection helpers
        sel_row = QHBoxLayout()
        for label, slot in [
            ("✅ Select All",       self._table_select_all),
            ("☐ Clear Selection",   self._table_clear),
            ("👁 Hide Unselected", self._hide_unselected),
        ]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            sel_row.addWidget(b)
        sel_row.addStretch()
        tl.addLayout(sel_row)

        # Table
        self._table = QTableWidget()
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(["✓", "Index", "Name", "Date", "Size", "File"])
        self._table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self._table.setColumnWidth(0, 40)
        self._table.setColumnWidth(1, 65)
        self._table.setColumnWidth(3, 140)
        self._table.setColumnWidth(4, 80)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        self._table.itemChanged.connect(self._on_item_changed)
        tl.addWidget(self._table)

        root.addWidget(tbl_group, 2)

        # Progress
        prog_group = QGroupBox("Restore Progress")
        pl = QVBoxLayout(prog_group)

        self._lbl_progress_text = QLabel("Select backups above and click Start Restore.")
        self._lbl_progress_text.setStyleSheet("color: #8b949e;")
        pl.addWidget(self._lbl_progress_text)

        self._overall_bar = QProgressBar()
        self._overall_bar.setRange(0, 100)
        self._overall_bar.setValue(0)
        self._overall_bar.setFixedHeight(10)
        self._overall_bar.setTextVisible(False)
        pl.addWidget(self._overall_bar)

        root.addWidget(prog_group)

        self._progress_panel = ProgressPanel()
        self._progress_panel.setMinimumHeight(100)
        root.addWidget(self._progress_panel, 1)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._btn_start = QPushButton("▶  Start Restore")
        self._btn_start.setObjectName("btn_success")
        self._btn_start.setMinimumWidth(160)
        self._btn_start.clicked.connect(self._on_start)
        btn_row.addWidget(self._btn_start)
        self._btn_cancel = QPushButton("■  Cancel")
        self._btn_cancel.setObjectName("btn_danger")
        self._btn_cancel.setEnabled(False)
        btn_row.addWidget(self._btn_cancel)
        root.addLayout(btn_row)

    # ------------------------------------------------------------------
    def setup(self, ldconsole: Optional[LDConsoleWrapper], settings: AppSettings, store: MetadataStore):
        self._ldconsole = ldconsole
        self._settings = settings
        self._store = store
        self._load_records()

    def load_instances(self, instances: List[InstanceInfo]):
        self._instances = instances

    def refresh_records(self):
        """Called externally (e.g., after a backup completes)."""
        self._load_records()

    # ------------------------------------------------------------------

    def _load_records(self):
        if not self._store:
            return
        self._records = self._store.get_all_latest()
        self._populate_table(self._records)

    def _populate_table(self, records: List[BackupRecord]):
        self._table.setSortingEnabled(False)
        self._table.blockSignals(True)
        self._table.setRowCount(0)

        for rec in records:
            row = self._table.rowCount()
            self._table.insertRow(row)

            # Checkbox
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Unchecked)
            chk.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 0, chk)

            # Index
            idx_item = QTableWidgetItem(str(rec.instance_index))
            idx_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 1, idx_item)

            # Name
            self._table.setItem(row, 2, QTableWidgetItem(rec.instance_name))

            # Timestamp
            ts_item = QTableWidgetItem(rec.timestamp.replace("T", " ").replace("Z", ""))
            ts_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 3, ts_item)

            # Size
            size_str = format_bytes(rec.file_size) if rec.file_size else "—"
            s_item = QTableWidgetItem(size_str)
            s_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 4, s_item)

            # Filename only (not full path)
            self._table.setItem(row, 5, QTableWidgetItem(rec.backup_path))
            # Store full record index for retrieval
            self._table.item(row, 1).setData(Qt.UserRole, len(records) - self._table.rowCount() + row)

        self._table.blockSignals(False)
        self._table.setSortingEnabled(True)
        self._update_sel_count()

    # ------------------------------------------------------------------
    # Filter
    # ------------------------------------------------------------------

    def _apply_filter(self):
        range_text = self._range_input.text().strip()
        name_text = self._name_input.text().strip().lower()

        allowed_indices = None
        if range_text:
            try:
                allowed_indices = set(parse_index_range(range_text))
            except ValueError as e:
                QMessageBox.warning(self, "Invalid Range", str(e))
                return

        for row in range(self._table.rowCount()):
            idx_item = self._table.item(row, 1)
            name_item = self._table.item(row, 2)
            if not idx_item or not name_item:
                continue
            try:
                idx = int(idx_item.text())
            except ValueError:
                continue
            name = name_item.text().lower()
            visible = True
            if allowed_indices is not None and idx not in allowed_indices:
                visible = False
            if name_text and name_text not in name:
                visible = False
            self._table.setRowHidden(row, not visible)

        self._update_sel_count()

    def _set_rows(self, state, visible_only=False):
        self._table.blockSignals(True)
        for row in range(self._table.rowCount()):
            if visible_only and self._table.isRowHidden(row):
                continue
            item = self._table.item(row, 0)
            if item:
                item.setCheckState(state)
        self._table.blockSignals(False)
        self._update_sel_count()

    def _table_select_all(self):     self._set_rows(Qt.Checked)
    def _table_clear(self):          self._set_rows(Qt.Unchecked)

    def _hide_unselected(self):
        for row in range(self._table.rowCount()):
            chk = self._table.item(row, 0)
            if chk and chk.checkState() != Qt.Checked:
                self._table.setRowHidden(row, True)
        self._update_sel_count()

    def _update_sel_count(self):
        checked = sum(
            1 for row in range(self._table.rowCount())
            if not self._table.isRowHidden(row)
            and self._table.item(row, 0)
            and self._table.item(row, 0).checkState() == Qt.Checked
        )
        visible = sum(1 for row in range(self._table.rowCount()) if not self._table.isRowHidden(row))
        self._lbl_sel.setText(f"{checked}/{visible} selected")

    @pyqtSlot(QTableWidgetItem)
    def _on_item_changed(self, item):
        if item.column() == 0:
            self._update_sel_count()

    def _get_checked_records(self) -> List[BackupRecord]:
        result = []
        seen = set()
        for row in range(self._table.rowCount()):
            if self._table.isRowHidden(row):
                continue
            chk = self._table.item(row, 0)
            idx_item = self._table.item(row, 1)
            if chk and chk.checkState() == Qt.Checked and idx_item:
                try:
                    idx = int(idx_item.text())
                except ValueError:
                    continue
                if idx not in seen:
                    seen.add(idx)
                    # Find matching record
                    for rec in self._records:
                        if rec.instance_index == idx:
                            result.append(rec)
                            break
        return result

    # ------------------------------------------------------------------
    # Start / Cancel
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_start(self):
        if not self._ldconsole or not self._settings:
            QMessageBox.warning(self, "Not Ready", "Configure the tool in Settings first.")
            return

        selected = self._get_checked_records()
        if not selected:
            QMessageBox.information(self, "Nothing Selected", "Check at least one backup to restore.")
            return

        confirm = QMessageBox.question(
            self, "Confirm Restore",
            f"Restore {len(selected)} instance(s)?\n\nThis will overwrite their current state.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        jobs = [
            RestoreJob(
                instance_index=r.instance_index,
                instance_name=r.instance_name,
                backup_path=r.backup_path,
            )
            for r in selected
        ]

        self._progress_panel.setup_jobs(jobs)
        self._overall_bar.setValue(0)
        self._lbl_progress_text.setText(f"Restoring {len(jobs)} instance(s)…")
        self._btn_start.setEnabled(False)
        self._btn_cancel.setEnabled(True)

        self._manager = RestoreManager(jobs, self._ldconsole, self._settings)
        ws = self._manager.worker_signals
        ws.job_started.connect(self._progress_panel.on_job_started)
        ws.job_status_changed.connect(self._progress_panel.on_status_changed)
        ws.job_done.connect(self._progress_panel.on_restore_done)
        ws.job_failed.connect(self._progress_panel.on_job_failed)
        self._manager.signals.queue_progress.connect(self._on_queue_progress)
        self._manager.signals.all_complete.connect(self._on_all_complete)
        self._manager.start(self._instances)

    @pyqtSlot(int, int)
    def _on_queue_progress(self, done: int, total: int):
        pct = int(100 * done / total) if total else 0
        self._overall_bar.setValue(pct)
        self._lbl_progress_text.setText(f"Restoring… {done}/{total} done")

    @pyqtSlot(int, int, float)
    def _on_all_complete(self, success: int, failed: int, duration: float):
        self._overall_bar.setValue(100)
        self._btn_start.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._lbl_progress_text.setText(
            f"Done — {success} restored, {failed} failed — {format_duration(duration)}"
        )
        if self._settings.notify_on_session_complete and self._settings.discord_webhook_url:
            send_session_summary(
                self._settings.discord_webhook_url,
                operation="Restore",
                success_count=success,
                fail_count=failed,
                total_count=success + failed,
                duration_sec=duration,
            )
        QMessageBox.information(self, "Restore Complete",
                                f"{success} instance(s) restored.\n{failed} failed.")
