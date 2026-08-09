"""
Backup Tab
==========
• Instance table with per-row checkboxes for precise selection
• Range/name filter that narrows visible rows
• Select All / Clear / Invert / Select Visible helpers
• Refresh instances button
• Crash-resume banner
• Overall progress bar + per-instance live progress panel
"""
import os
from typing import List, Optional

from PyQt5.QtCore import Qt, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from config.settings import AppSettings, save_settings
from core.backup_engine import BackupJob, BackupManager, JobStatus
from core.ldconsole import InstanceInfo, LDConsoleWrapper
from core.metadata_store import MetadataStore
from notifications.discord_notifier import send_failure_alert, send_session_summary
from ui.widgets.progress_panel import ProgressPanel
from utils.helpers import format_duration, now_iso, parse_index_range


class BackupTab(QWidget):
    # Emitted on session complete so Dashboard can auto-refresh
    backup_session_complete = pyqtSignal(int, int, float)  # success, failed, duration

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ldconsole: Optional[LDConsoleWrapper] = None
        self._settings: Optional[AppSettings] = None
        self._store: Optional[MetadataStore] = None
        self._all_instances: List[InstanceInfo] = []
        self._manager: Optional[BackupManager] = None
        self._current_jobs: List[BackupJob] = []
        self._failed_names: List[str] = []
        self._init_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        # --- Header ---------------------------------------------------
        hdr = QHBoxLayout()
        title = QLabel("Backup Instances")
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #e6edf3;")
        hdr.addWidget(title)
        hdr.addStretch()

        self._lbl_count = QLabel("No instances loaded")
        self._lbl_count.setStyleSheet("color: #8b949e; font-size: 13px;")
        hdr.addWidget(self._lbl_count)

        self._btn_refresh = QPushButton("↻  Refresh")
        self._btn_refresh.clicked.connect(self._on_refresh_requested)
        hdr.addWidget(self._btn_refresh)
        root.addLayout(hdr)

        # --- Resume banner (hidden by default) -----------------------
        self._resume_frame = QFrame()
        self._resume_frame.setObjectName("card_accent")
        self._resume_frame.setVisible(False)
        rl = QHBoxLayout(self._resume_frame)
        rl.setContentsMargins(12, 8, 12, 8)
        self._lbl_resume = QLabel("⚡ A previous run was interrupted. Resume from where it left off?")
        self._lbl_resume.setStyleSheet("color: #79c0ff; font-weight: 600;")
        rl.addWidget(self._lbl_resume)
        rl.addStretch()
        btn_resume = QPushButton("Resume Run")
        btn_resume.setObjectName("btn_primary")
        btn_resume.clicked.connect(self._on_resume)
        rl.addWidget(btn_resume)
        btn_discard = QPushButton("Discard")
        btn_discard.clicked.connect(self._on_discard_resume)
        rl.addWidget(btn_discard)
        root.addWidget(self._resume_frame)

        # --- Instance table group -------------------------------------
        table_group = QGroupBox("Select Instances to Backup")
        tg = QVBoxLayout(table_group)
        tg.setSpacing(6)

        # Filter row
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Index range:"))
        self._range_input = QLineEdit()
        self._range_input.setPlaceholderText("e.g. 0-100  or  0,5,10-20")
        self._range_input.setMaximumWidth(180)
        filter_row.addWidget(self._range_input)

        filter_row.addWidget(QLabel("Name filter:"))
        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("e.g. emulator")
        self._name_input.setMaximumWidth(180)
        filter_row.addWidget(self._name_input)

        btn_apply = QPushButton("Apply Filter")
        btn_apply.clicked.connect(self._apply_filter)
        filter_row.addWidget(btn_apply)

        filter_row.addStretch()
        self._lbl_selected = QLabel("0 selected")
        self._lbl_selected.setStyleSheet("color: #8b949e; font-size: 12px;")
        filter_row.addWidget(self._lbl_selected)
        tg.addLayout(filter_row)

        # Selection helper buttons
        sel_row = QHBoxLayout()
        for label, slot in [
            ("✅ Select All",         self._select_all),
            ("☐ Clear Selection",    self._clear_selection),
            ("👁 Hide Unselected",   self._hide_unselected),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            sel_row.addWidget(btn)
        sel_row.addStretch()
        tg.addLayout(sel_row)

        # Table
        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(["✓", "Index", "Name", "Status", "Last Backup"])
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._table.horizontalHeader().setDefaultSectionSize(100)
        self._table.setColumnWidth(0, 40)
        self._table.setColumnWidth(1, 65)
        self._table.setColumnWidth(3, 90)
        self._table.setColumnWidth(4, 140)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        self._table.setSortingEnabled(True)
        self._table.itemChanged.connect(self._on_item_changed)
        tg.addWidget(self._table)

        root.addWidget(table_group)

        # --- Concurrency ----------------------------------------------
        conc_row = QHBoxLayout()
        conc_row.addWidget(QLabel("Max concurrent workers:"))
        self._spin_workers = QSpinBox()
        self._spin_workers.setRange(1, 50)
        self._spin_workers.setValue(5)
        self._spin_workers.setFixedWidth(70)
        self._spin_workers.setToolTip("How many instances to back up in parallel")
        conc_row.addWidget(self._spin_workers)
        conc_row.addStretch()
        root.addLayout(conc_row)

        # --- Overall progress -----------------------------------------
        prog_group = QGroupBox("Progress")
        pl = QVBoxLayout(prog_group)

        self._lbl_status = QLabel("Choose instances above and click Start Backup.")
        self._lbl_status.setStyleSheet("color: #8b949e;")
        pl.addWidget(self._lbl_status)

        self._overall_bar = QProgressBar()
        self._overall_bar.setRange(0, 100)
        self._overall_bar.setValue(0)
        self._overall_bar.setFixedHeight(10)
        self._overall_bar.setTextVisible(False)
        pl.addWidget(self._overall_bar)

        stats_row = QHBoxLayout()
        self._lbl_success = QLabel("✅ Success: 0")
        self._lbl_success.setStyleSheet("color: #3fb950; font-weight: 600;")
        self._lbl_failed = QLabel("❌ Failed: 0")
        self._lbl_failed.setStyleSheet("color: #f85149; font-weight: 600;")
        self._lbl_time = QLabel("")
        self._lbl_time.setStyleSheet("color: #484f58; font-size: 12px;")
        stats_row.addWidget(self._lbl_success)
        stats_row.addWidget(self._lbl_failed)
        stats_row.addStretch()
        stats_row.addWidget(self._lbl_time)
        pl.addLayout(stats_row)

        root.addWidget(prog_group)

        # --- Per-instance progress panel ------------------------------
        self._progress_panel = ProgressPanel()
        self._progress_panel.setMinimumHeight(120)
        root.addWidget(self._progress_panel, 1)

        # --- Action buttons ------------------------------------------
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._btn_start = QPushButton("▶  Start Backup")
        self._btn_start.setObjectName("btn_primary")
        self._btn_start.setMinimumWidth(160)
        self._btn_start.clicked.connect(self._on_start)
        btn_row.addWidget(self._btn_start)

        self._btn_cancel = QPushButton("■  Cancel")
        self._btn_cancel.setObjectName("btn_danger")
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self._on_cancel)
        btn_row.addWidget(self._btn_cancel)
        root.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Public API called by MainWindow
    # ------------------------------------------------------------------

    def setup(self, ldconsole: Optional[LDConsoleWrapper], settings: AppSettings, store: MetadataStore):
        self._ldconsole = ldconsole
        self._settings = settings
        self._store = store
        self._spin_workers.setValue(settings.max_concurrency)

    def load_instances(self, instances: List[InstanceInfo]):
        self._all_instances = instances
        self._populate_table(instances)

    def check_resume(self):
        if self._settings and self._settings.backup_destination:
            BackupManager.cleanup_stale_backups(self._settings.backup_destination)
            
        if not self._store: return
        pending = self._store.get_pending_backups()
        if pending:
            self._resume_frame.setVisible(True)
            self._lbl_resume.setText(
                f"⚡ Previous run was interrupted — {len(pending)} jobs still pending. Resume?"
            )

    # Forwarded from MainWindow so the Refresh button can trigger instance reload
    refresh_requested = pyqtSignal()

    def _on_refresh_requested(self):
        self.refresh_requested.emit()

    # ------------------------------------------------------------------
    # Table population
    # ------------------------------------------------------------------

    def _populate_table(self, instances: List[InstanceInfo]):
        self._table.setSortingEnabled(False)
        self._table.blockSignals(True)
        self._table.setRowCount(0)

        for inst in instances:
            row = self._table.rowCount()
            self._table.insertRow(row)

            # Col 0 — checkbox
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Checked)
            chk.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 0, chk)

            # Col 1 — index
            idx_item = QTableWidgetItem(str(inst.index))
            idx_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 1, idx_item)

            # Col 2 — name
            self._table.setItem(row, 2, QTableWidgetItem(inst.name))

            # Col 3 — running status
            status_txt = "🟢 Running" if inst.is_running else "⚫ Stopped"
            s_item = QTableWidgetItem(status_txt)
            s_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 3, s_item)

            # Col 4 — last backup date from DB
            last_txt = "—"
            if self._store:
                rec = self._store.get_latest_backup(inst.index)
                if rec:
                    last_txt = rec.timestamp[:10]
            lb_item = QTableWidgetItem(last_txt)
            lb_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 4, lb_item)

        self._table.blockSignals(False)
        self._table.setSortingEnabled(True)
        self._update_selected_count()
        self._lbl_count.setText(f"{len(instances)} instances loaded")

    # ------------------------------------------------------------------
    # Filter helpers
    # ------------------------------------------------------------------

    def _apply_filter(self):
        range_text = self._range_input.text().strip()
        name_text = self._name_input.text().strip().lower()

        allowed_indices = None
        if range_text:
            try:
                allowed_indices = set(parse_index_range(range_text))
            except ValueError as e:
                QMessageBox.warning(self, "Invalid Range", f"Range format error:\n{e}")
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

        self._update_selected_count()

    def _set_all_rows(self, state: Qt.CheckState, visible_only: bool = False):
        self._table.blockSignals(True)
        for row in range(self._table.rowCount()):
            if visible_only and self._table.isRowHidden(row):
                continue
            item = self._table.item(row, 0)
            if item:
                item.setCheckState(state)
        self._table.blockSignals(False)
        self._update_selected_count()

    def _select_all(self):       self._set_all_rows(Qt.Checked)
    def _clear_selection(self):  self._set_all_rows(Qt.Unchecked)

    def _hide_unselected(self):
        for row in range(self._table.rowCount()):
            chk = self._table.item(row, 0)
            if chk and chk.checkState() != Qt.Checked:
                self._table.setRowHidden(row, True)
        self._update_selected_count()

    def _update_selected_count(self):
        checked = sum(
            1 for row in range(self._table.rowCount())
            if not self._table.isRowHidden(row)
            and self._table.item(row, 0)
            and self._table.item(row, 0).checkState() == Qt.Checked
        )
        total_visible = sum(
            1 for row in range(self._table.rowCount())
            if not self._table.isRowHidden(row)
        )
        self._lbl_selected.setText(f"{checked}/{total_visible} selected")

    @pyqtSlot(QTableWidgetItem)
    def _on_item_changed(self, item):
        if item.column() == 0:
            self._update_selected_count()

    # ------------------------------------------------------------------
    # Build jobs from checked rows
    # ------------------------------------------------------------------

    def _get_checked_jobs(self) -> List[BackupJob]:
        jobs = []
        for row in range(self._table.rowCount()):
            if self._table.isRowHidden(row):
                continue
            chk = self._table.item(row, 0)
            idx_item = self._table.item(row, 1)
            name_item = self._table.item(row, 2)
            if chk and chk.checkState() == Qt.Checked and idx_item and name_item:
                try:
                    jobs.append(BackupJob(index=int(idx_item.text()), name=name_item.text()))
                except ValueError:
                    pass
        return jobs

    # ------------------------------------------------------------------
    # Start / Cancel
    # ------------------------------------------------------------------

    def _validate(self) -> bool:
        if not self._ldconsole:
            QMessageBox.warning(self, "Not Ready", "Configure ldconsole.exe path in Settings.")
            return False
        if not self._settings or not self._settings.backup_destination:
            QMessageBox.warning(self, "No Destination", "Set a backup destination folder in Settings.")
            return False
        return True

    def _start_run(self, jobs: List[BackupJob]):
        if not jobs:
            QMessageBox.information(self, "Nothing Selected", "No instances are checked for backup.")
            return

        self._current_jobs = jobs
        self._failed_names = []

        self._settings.max_concurrency = self._spin_workers.value()
        save_settings(self._settings)

        self._progress_panel.setup_jobs(jobs)
        self._overall_bar.setValue(0)
        self._lbl_success.setText("✅ Success: 0")
        self._lbl_failed.setText("❌ Failed: 0")
        self._lbl_status.setText(f"Backing up {len(jobs)} instance(s)…")
        self._btn_start.setEnabled(False)
        self._btn_cancel.setEnabled(True)

        self._manager = BackupManager(jobs, self._ldconsole, self._settings, self._store)
        ws = self._manager.signals.worker_signals

        ws.job_started.connect(self._progress_panel.on_job_started)
        ws.job_status_changed.connect(self._progress_panel.on_status_changed)
        ws.job_progress.connect(self._progress_panel.on_progress)
        ws.job_done.connect(self._progress_panel.on_job_done)
        ws.job_failed.connect(self._progress_panel.on_job_failed)
        ws.job_failed.connect(self._on_worker_failed)

        self._manager.signals.queue_progress.connect(self._on_queue_progress)
        self._manager.signals.all_complete.connect(self._on_all_complete)
        self._manager.start()

    @pyqtSlot()
    def _on_start(self):
        if not self._validate():
            return
        jobs = self._get_checked_jobs()
        if not jobs:
            QMessageBox.information(self, "Nothing Selected",
                                    "Check at least one instance in the table.")
            return

        if self._store:
            pending_records = self._store.get_pending_backups()
            if pending_records:
                reply = QMessageBox.question(
                    self, "Resume Previous Session?",
                    f"Found {len(pending_records)} pending backup(s) from a previous interrupted run.\n\n"
                    "Click 'Yes' to resume those along with your current selection.\n"
                    "Click 'No' to discard the old run and start over with only the current selection.",
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel
                )
                if reply == QMessageBox.Cancel:
                    return
                if reply == QMessageBox.No:
                    self._store.cancel_pending_backups()
                    self._resume_frame.setVisible(False)
                else:
                    self._resume_frame.setVisible(False)
                    job_indices = {j.index for j in jobs}
                    for rec in pending_records:
                        if rec.instance_index not in job_indices:
                            jobs.append(BackupJob(index=rec.instance_index, name=rec.instance_name))

            for job in jobs:
                self._store.upsert_backup(
                    instance_index=job.index,
                    instance_name=job.name,
                    backup_path="",
                    checksum="",
                    timestamp=now_iso(),
                    status="pending"
                )

        self._start_run(jobs)

    @pyqtSlot()
    def _on_cancel(self):
        if self._manager:
            self._manager.cancel()
        self._btn_start.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._lbl_status.setText("Cancelled.")

    @pyqtSlot()
    def _on_resume(self):
        if not self._validate():
            return
        if self._store:
            pending_records = self._store.get_pending_backups()
            if pending_records:
                self._resume_frame.setVisible(False)
                jobs = self._get_checked_jobs()
                job_indices = {j.index for j in jobs}
                for rec in pending_records:
                    if rec.instance_index not in job_indices:
                        jobs.append(BackupJob(index=rec.instance_index, name=rec.instance_name))

                for job in jobs:
                    self._store.upsert_backup(
                        instance_index=job.index,
                        instance_name=job.name,
                        backup_path="",
                        checksum="",
                        timestamp=now_iso(),
                        status="pending"
                    )

                self._start_run(jobs)

    @pyqtSlot()
    def _on_discard_resume(self):
        if self._store:
            self._store.cancel_pending_backups()
        self._resume_frame.setVisible(False)

    @pyqtSlot(int, str)
    def _on_worker_failed(self, index: int, error: str):
        job = next((j for j in self._current_jobs if j.index == index), None)
        if job:
            self._failed_names.append(f"{job.name} (#{index})")
            if self._settings.notify_on_failure and self._settings.discord_webhook_url:
                send_failure_alert(
                    self._settings.discord_webhook_url,
                    operation="Backup",
                    instance_index=index,
                    instance_name=job.name,
                    error=error,
                )

    @pyqtSlot(int, int)
    def _on_queue_progress(self, done: int, total: int):
        pct = int(100 * done / total) if total else 0
        self._overall_bar.setValue(pct)
        succeeded = done - len(self._failed_names)
        self._lbl_success.setText(f"✅ Success: {succeeded}")
        self._lbl_failed.setText(f"❌ Failed: {len(self._failed_names)}")
        self._lbl_status.setText(f"Processing… {done}/{total} complete")

    @pyqtSlot(int, int, float)
    def _on_all_complete(self, success: int, failed: int, duration: float):
        self._overall_bar.setValue(100)
        self._btn_start.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        total = success + failed
        self._lbl_status.setText(
            f"All done!  {success}/{total} succeeded  —  {format_duration(duration)}"
        )
        self._lbl_time.setText(f"⏱ {format_duration(duration)}")

        # Tell dashboard to refresh
        self.backup_session_complete.emit(success, failed, duration)

        if self._settings.notify_on_session_complete and self._settings.discord_webhook_url:
            send_session_summary(
                self._settings.discord_webhook_url,
                operation="Backup",
                success_count=success,
                fail_count=failed,
                total_count=total,
                duration_sec=duration,
                failed_names=self._failed_names or None,
            )

        if failed > 0:
            QMessageBox.warning(self, "Backup Complete with Errors",
                f"{success} succeeded, {failed} failed.\nCheck the Logs tab for details.")
        else:
            QMessageBox.information(self, "Backup Complete",
                f"All {success} instance(s) backed up successfully in {format_duration(duration)}!")
