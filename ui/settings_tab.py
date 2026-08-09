"""
Settings Tab
============
All user-configurable settings with live validation and save-to-disk.
"""
import os

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config.settings import AppSettings, save_settings
from notifications.discord_notifier import test_webhook


class SettingsTab(QWidget):
    settings_changed = pyqtSignal(AppSettings)  # emitted when user saves

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings: AppSettings = AppSettings()
        self._init_ui()

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Settings")
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #e6edf3;")
        root.addWidget(title)

        # ----- LDConsole Path -----------------------------------------
        paths_group = QGroupBox("LDPlayer Paths")
        pg_layout = QVBoxLayout(paths_group)

        # ldconsole.exe
        ldc_row = QHBoxLayout()
        ldc_row.addWidget(QLabel("ldconsole.exe path:"))
        self._ldc_path = QLineEdit()
        self._ldc_path.setPlaceholderText(r"C:\LDPlayer\LDPlayer9\ldconsole.exe")
        ldc_row.addWidget(self._ldc_path)
        btn_browse_ldc = QPushButton("Browse…")
        btn_browse_ldc.clicked.connect(self._browse_ldconsole)
        ldc_row.addWidget(btn_browse_ldc)
        btn_test_ldc = QPushButton("Test")
        btn_test_ldc.clicked.connect(self._test_ldconsole)
        ldc_row.addWidget(btn_test_ldc)
        pg_layout.addLayout(ldc_row)

        self._lbl_ldc_status = QLabel("")
        pg_layout.addWidget(self._lbl_ldc_status)

        # Backup destination
        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("Backup destination:"))
        self._dest_path = QLineEdit()
        self._dest_path.setPlaceholderText("D:\\Backups\\LDPlayer")
        dest_row.addWidget(self._dest_path)
        btn_browse_dest = QPushButton("Browse…")
        btn_browse_dest.clicked.connect(self._browse_destination)
        dest_row.addWidget(btn_browse_dest)
        pg_layout.addLayout(dest_row)

        # Restore path
        rest_row = QHBoxLayout()
        rest_row.addWidget(QLabel("Restore path:"))
        self._rest_path = QLineEdit()
        self._rest_path.setPlaceholderText("D:\\Backups\\LDPlayer")
        rest_row.addWidget(self._rest_path)
        btn_browse_rest = QPushButton("Browse…")
        btn_browse_rest.clicked.connect(self._browse_restore)
        rest_row.addWidget(btn_browse_rest)
        pg_layout.addLayout(rest_row)

        root.addWidget(paths_group)

        # ----- Concurrency -------------------------------------------
        conc_group = QGroupBox("Batch & Concurrency")
        cl = QVBoxLayout(conc_group)

        # (Removed batch size)

        w_row = QHBoxLayout()
        w_row.addWidget(QLabel("Max concurrent workers:"))
        self._spin_workers = QSpinBox()
        self._spin_workers.setRange(1, 50)
        self._spin_workers.setValue(5)
        self._spin_workers.setToolTip("Max parallel backup operations at once")
        w_row.addWidget(self._spin_workers)
        w_row.addStretch()
        cl.addLayout(w_row)

        note = QLabel("💡 Tip: Start low (3–5) and increase based on your disk performance.")
        note.setObjectName("label_muted")
        note.setWordWrap(True)
        cl.addWidget(note)

        root.addWidget(conc_group)

        # ----- Discord -----------------------------------------------
        disc_group = QGroupBox("Discord Notifications (Webhook)")
        dl = QVBoxLayout(disc_group)

        wh_row = QHBoxLayout()
        wh_row.addWidget(QLabel("Webhook URL:"))
        self._discord_url = QLineEdit()
        self._discord_url.setPlaceholderText("https://discord.com/api/webhooks/…")
        wh_row.addWidget(self._discord_url)
        btn_test_wh = QPushButton("Test")
        btn_test_wh.clicked.connect(self._test_discord)
        wh_row.addWidget(btn_test_wh)
        dl.addLayout(wh_row)

        self._chk_notify_failure = QCheckBox("Send alert on individual instance failure")
        self._chk_notify_complete = QCheckBox("Send summary when a full session completes")
        self._chk_notify_failure.setChecked(True)
        self._chk_notify_complete.setChecked(True)
        dl.addWidget(self._chk_notify_failure)
        dl.addWidget(self._chk_notify_complete)

        root.addWidget(disc_group)

        # ----- Retention ---------------------------------------------
        ret_group = QGroupBox("Backup Retention")
        rl = QVBoxLayout(ret_group)
        info = QLabel(
            "When a new backup starts, the existing backup.ldbk is renamed to backup.ldbk.old.\n"
            "On success, the .old file is deleted.\n"
            "On failure, the .old file is kept as a safety net."
        )
        info.setObjectName("label_muted")
        info.setWordWrap(True)
        rl.addWidget(info)
        root.addWidget(ret_group)

        root.addStretch()

        # Save button
        btn_save = QPushButton("💾  Save Settings")
        btn_save.setObjectName("btn_primary")
        btn_save.setMinimumWidth(160)
        btn_save.clicked.connect(self._save)
        root.addWidget(btn_save)

    # ------------------------------------------------------------------
    def load_settings(self, s: AppSettings):
        self._settings = s
        self._ldc_path.setText(s.ldconsole_path)
        self._dest_path.setText(s.backup_destination)
        self._rest_path.setText(s.restore_path)
        self._spin_workers.setValue(s.max_concurrency)
        self._discord_url.setText(s.discord_webhook_url)
        self._chk_notify_failure.setChecked(s.notify_on_failure)
        self._chk_notify_complete.setChecked(s.notify_on_session_complete)
        self._validate_ldconsole_path(s.ldconsole_path, silent=True)

    def _browse_ldconsole(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select ldconsole.exe", "", "Executable (*.exe)"
        )
        if path:
            self._ldc_path.setText(path)
            self._validate_ldconsole_path(path)

    def _browse_destination(self):
        path = QFileDialog.getExistingDirectory(self, "Select Backup Destination")
        if path:
            self._dest_path.setText(path)

    def _browse_restore(self):
        path = QFileDialog.getExistingDirectory(self, "Select Restore Path")
        if path:
            self._rest_path.setText(path)

    def _validate_ldconsole_path(self, path: str, silent: bool = False) -> bool:
        if os.path.isfile(path):
            self._lbl_ldc_status.setText("✅ ldconsole.exe found")
            self._lbl_ldc_status.setObjectName("label_success")
            self._lbl_ldc_status.setStyleSheet("color: #3fb950; font-size: 12px;")
            return True
        else:
            self._lbl_ldc_status.setText("⚠️ File not found at this path")
            self._lbl_ldc_status.setStyleSheet("color: #d29922; font-size: 12px;")
            return False

    def _test_ldconsole(self):
        path = self._ldc_path.text().strip()
        if not self._validate_ldconsole_path(path):
            QMessageBox.warning(self, "Not Found", f"ldconsole.exe not found at:\n{path}")

    def _test_discord(self):
        url = self._discord_url.text().strip()
        if not url:
            QMessageBox.warning(self, "No URL", "Enter a Discord webhook URL first.")
            return
        ok = test_webhook(url)
        if ok:
            QMessageBox.information(self, "Success", "✅ Test message sent to Discord!")
        else:
            QMessageBox.critical(self, "Failed", "❌ Failed to send. Check the webhook URL and your network.")

    def _save(self):
        path = self._ldc_path.text().strip()
        if path and not os.path.isfile(path):
            confirm = QMessageBox.question(
                self,
                "Path Not Found",
                f"ldconsole.exe not found at:\n{path}\n\nSave anyway?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm == QMessageBox.No:
                return

        self._settings.ldconsole_path = path
        self._settings.backup_destination = self._dest_path.text().strip()
        self._settings.restore_path = self._rest_path.text().strip()
        self._settings.max_concurrency = self._spin_workers.value()
        self._settings.discord_webhook_url = self._discord_url.text().strip()
        self._settings.notify_on_failure = self._chk_notify_failure.isChecked()
        self._settings.notify_on_session_complete = self._chk_notify_complete.isChecked()

        save_settings(self._settings)
        self.settings_changed.emit(self._settings)
        QMessageBox.information(self, "Saved", "Settings saved successfully.")
