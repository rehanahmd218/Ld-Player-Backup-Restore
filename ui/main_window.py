"""
Main Application Window
=======================
Hosts all tabs, wires signals between components, and manages
the LDConsole startup check.
"""
import os
import subprocess
import sys
from typing import List, Optional

from PyQt5.QtCore import QThread, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
)

from config.settings import AppSettings, load_settings, save_settings
from core.ldconsole import InstanceInfo, LDConsoleWrapper
from core.metadata_store import MetadataStore
from ui.backup_tab import BackupTab
from ui.create_instances_tab import CreateInstancesTab
from ui.dashboard_tab import DashboardTab
from ui.logs_tab import LogsTab
from ui.restore_tab import RestoreTab
from ui.settings_tab import SettingsTab
from utils.logger import get_logger, setup_logging

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Background thread: full startup cleanup (keeps UI responsive)
# ---------------------------------------------------------------------------
_STALE_PROCESSES = ["ldconsole.exe", "dnplayer.exe", "7za.exe"]
# dnplayer.exe is LDPlayer's emulator process.
# 7za.exe (7-Zip) is the actual archiver LDPlayer uses to write .ldbk files
# — it holds the exclusive write lock until it finishes or is killed.


class StartupCleanupThread(QThread):
    """
    Runs entirely off the main thread:
      1. Kills all ldconsole.exe + dnplayer.exe processes.
      2. Waits briefly for the OS to release file handles.
      3. Deletes all files from the <backup_dest>_temp sibling folder.
    Emits `done(killed, deleted)` when finished regardless of errors.
    """
    done = pyqtSignal(int, int)   # (processes_killed, temp_files_deleted)

    def __init__(self, dest_dir: str, parent=None):
        super().__init__(parent)
        self._dest_dir = dest_dir

    @staticmethod
    def _get_pids(image_name: str):
        """Return all PIDs for the given image name via tasklist CSV output."""
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=10
            )
            pids = []
            for line in result.stdout.splitlines():
                parts = line.strip().replace('"', '').split(",")
                if len(parts) >= 2 and parts[0].lower() == image_name.lower():
                    try:
                        pids.append(int(parts[1]))
                    except ValueError:
                        pass
            return pids
        except Exception:
            return []

    @staticmethod
    def _kill_pid(pid: int) -> bool:
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=5
            )
            return True
        except Exception:
            return False

    def run(self):
        import time

        killed = 0
        deleted = 0

        def _kill_all_stale() -> int:
            """Kill every listed stale process; return count killed."""
            count = 0
            for proc in _STALE_PROCESSES:
                for pid in self._get_pids(proc):
                    if self._kill_pid(pid):
                        count += 1
            return count

        def _try_delete(files: list) -> list:
            """Attempt deletion with retries. Returns list of files still locked."""
            still_locked = []
            for temp_f in files:
                removed = False
                for _ in range(6):                 # 6 attempts × 1.5 s = 9 s max
                    try:
                        os.remove(temp_f)
                        removed = True
                        break
                    except PermissionError:
                        time.sleep(1.5)            # still locked — wait and retry
                    except OSError:
                        removed = True             # already gone
                        break
                if not removed:
                    still_locked.append(temp_f)
            return still_locked

        def _list_temp_files() -> list:
            """Return all files inside the _temp sibling folder."""
            temp_dir = self._dest_dir + "_temp"
            if not os.path.isdir(temp_dir):
                return []
            return [
                os.path.join(temp_dir, f)
                for f in os.listdir(temp_dir)
                if os.path.isfile(os.path.join(temp_dir, f))
            ]

        # ── Round 1: kill everything, wait, delete ────────────────────────────
        killed += _kill_all_stale()
        if killed > 0:
            time.sleep(2.5)    # let OS flush all write buffers

        remaining = []
        temp_files = _list_temp_files()
        if temp_files:
            still_locked = _try_delete(temp_files)
            deleted += len(temp_files) - len(still_locked)
            remaining = still_locked

        # ── Round 2: some 7za instances are slower — kill survivors + retry ───
        if remaining:
            killed += _kill_all_stale()
            time.sleep(2.0)
            still_locked2 = _try_delete(remaining)
            deleted += len(remaining) - len(still_locked2)
            for f in still_locked2:
                import logging
                logging.getLogger(__name__).warning(
                    "Startup cleanup: could not delete locked file after 2 rounds: %s", f
                )

        self.done.emit(killed, deleted)



# ---------------------------------------------------------------------------
# Background thread for loading instances (keeps UI responsive)
# ---------------------------------------------------------------------------
class InstanceLoaderThread(QThread):
    instances_loaded = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, ldconsole: LDConsoleWrapper, parent=None):
        super().__init__(parent)
        self._ldconsole = ldconsole

    def run(self):
        try:
            instances = self._ldconsole.list_instances()
            self.instances_loaded.emit(instances)
        except Exception as e:
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._settings: AppSettings = load_settings()
        self._ldconsole: Optional[LDConsoleWrapper] = None
        self._store = MetadataStore()
        self._instances: List[InstanceInfo] = []
        self._loader_thread: Optional[InstanceLoaderThread] = None
        self._killer_thread: Optional[StartupCleanupThread] = None

        self._setup_logging()
        self._init_ui()
        self._apply_settings(self._settings)
        self._kill_stale_ldconsole()   # non-blocking; chains into startup check

    # ------------------------------------------------------------------
    def _setup_logging(self):
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        bridge = setup_logging(log_dir, self._settings.log_level)
        self._log_bridge = bridge

    # ------------------------------------------------------------------
    def _init_ui(self):
        self.setWindowTitle("LDPlayer 9 — Backup & Restore Tool")
        self.setMinimumSize(1100, 720)
        self.resize(1280, 820)

        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.North)
        self._tabs.setDocumentMode(True)
        self.setCentralWidget(self._tabs)

        # Create all tabs
        self._tab_dashboard = DashboardTab()
        self._tab_backup    = BackupTab()
        self._tab_restore   = RestoreTab()
        self._tab_create    = CreateInstancesTab()
        self._tab_logs      = LogsTab()
        self._tab_settings  = SettingsTab()

        self._tabs.addTab(self._tab_dashboard, "  📊 Dashboard  ")
        self._tabs.addTab(self._tab_backup,    "  💾 Backup     ")
        self._tabs.addTab(self._tab_restore,   "  ⬇ Restore    ")
        self._tabs.addTab(self._tab_create,    "  ➕ Create     ")
        self._tabs.addTab(self._tab_logs,      "  📋 Logs       ")
        self._tabs.addTab(self._tab_settings,  "  ⚙ Settings   ")

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._lbl_status = QLabel("Initialising…")
        self._lbl_instances_count = QLabel("")
        self._status_bar.addWidget(self._lbl_status, 1)
        self._status_bar.addPermanentWidget(self._lbl_instances_count)

        # Connect log bridge to log viewer
        self._log_bridge.log_emitted.connect(self._tab_logs.get_viewer().append_log)

        # Dashboard quick-actions
        self._tab_dashboard._btn_refresh_instances.clicked.connect(self._load_instances)
        self._tab_dashboard._btn_go_backup.clicked.connect(
            lambda: self._tabs.setCurrentWidget(self._tab_backup))
        self._tab_dashboard._btn_go_restore.clicked.connect(
            lambda: self._tabs.setCurrentWidget(self._tab_restore))

        # Backup tab → Refresh button → reload instances
        self._tab_backup.refresh_requested.connect(self._load_instances)

        # Backup completion → auto-refresh Dashboard + Restore tab
        self._tab_backup.backup_session_complete.connect(self._on_backup_complete)

        # Create tab completion → refresh instance list everywhere
        self._tab_create.instances_created.connect(self._load_instances)

        # Settings changes
        self._tab_settings.settings_changed.connect(self._on_settings_changed)

    # ------------------------------------------------------------------
    def _apply_settings(self, s: AppSettings):
        self._settings = s
        self._tab_settings.load_settings(s)
        self._tab_dashboard.setup(s, self._store)
        # Pass None for ldconsole until it's confirmed valid
        self._tab_backup.setup(None, s, self._store)
        self._tab_restore.setup(None, s, self._store)
        self._tab_create.setup(None, s)

        if os.path.isfile(s.ldconsole_path):
            self._ldconsole = LDConsoleWrapper(s.ldconsole_path)
            self._tab_backup.setup(self._ldconsole, s, self._store)
            self._tab_restore.setup(self._ldconsole, s, self._store)
            self._tab_create.setup(self._ldconsole, s)

    @pyqtSlot(AppSettings)
    def _on_settings_changed(self, s: AppSettings):
        self._apply_settings(s)
        if self._ldconsole:
            self._load_instances()
        logger.info("Settings saved and applied.")

    # ------------------------------------------------------------------
    # Backup session complete → update Dashboard immediately (Fix #4)
    # ------------------------------------------------------------------
    @pyqtSlot(int, int, float)
    def _on_backup_complete(self, success: int, failed: int, duration: float):
        self._tab_dashboard.refresh_stats(success, failed)
        self._tab_dashboard.refresh_records()   # refresh backup count card
        self._tab_restore.refresh_records()     # refresh restore table too

    # ------------------------------------------------------------------
    # Kill stale ldconsole.exe processes on startup (non-blocking)
    # ------------------------------------------------------------------
    def _kill_stale_ldconsole(self):
        """Show a 'please wait' popup, then spawn a background thread that kills
        stale processes AND cleans temp files — entirely off the main thread."""
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QDialog, QLabel, QVBoxLayout

        # Build a non-blocking 'please wait' popup
        self._cleanup_dlg = QDialog(self)
        self._cleanup_dlg.setWindowTitle("Starting Up")
        self._cleanup_dlg.setWindowFlags(
            Qt.Dialog | Qt.WindowTitleHint | Qt.CustomizeWindowHint |
            Qt.WindowStaysOnTopHint
        )
        self._cleanup_dlg.setFixedSize(340, 100)
        layout = QVBoxLayout(self._cleanup_dlg)
        layout.setContentsMargins(20, 16, 20, 16)
        lbl = QLabel(
            "⏳  Please wait — clearing previous session…\n"
            "Terminating stale processes and removing temp files."
        )
        lbl.setWordWrap(True)
        layout.addWidget(lbl)
        self._cleanup_dlg.show()          # show() not exec_() — non-blocking!

        self._lbl_status.setText("Cleaning up stale processes…")
        dest_dir = self._settings.backup_destination
        self._killer_thread = StartupCleanupThread(dest_dir, self)
        self._killer_thread.done.connect(self._on_killer_done)
        self._killer_thread.start()

    @pyqtSlot(int, int)
    def _on_killer_done(self, killed: int, deleted: int):
        # Close the popup
        if hasattr(self, "_cleanup_dlg") and self._cleanup_dlg:
            self._cleanup_dlg.accept()
            self._cleanup_dlg = None
        # All I/O already done in the background thread — just log and continue.
        if killed > 0:
            logger.info("Startup: terminated %d stale process(es) (ldconsole + dnplayer + 7za).", killed)
        if deleted > 0:
            logger.info("Startup: deleted %d temp backup file(s) from _temp folder.", deleted)
        # Now proceed with the normal startup check on the main thread.
        self._check_ldconsole_on_startup()

    # ------------------------------------------------------------------
    # LDConsole startup check
    # ------------------------------------------------------------------
    def _check_ldconsole_on_startup(self):
        path = self._settings.ldconsole_path
        if os.path.isfile(path):
            self._ldconsole = LDConsoleWrapper(path)
            self._lbl_status.setText(f"ldconsole.exe found at: {path}")
            self._load_instances()
        else:
            self._show_ldconsole_missing_dialog()

    def _show_ldconsole_missing_dialog(self):
        msg = QMessageBox(self)
        msg.setWindowTitle("LDConsole Not Found")
        msg.setIcon(QMessageBox.Warning)
        msg.setText(
            "<b>ldconsole.exe was not found.</b><br><br>"
            "You can either:<br>"
            "• <b>Browse</b> to select ldconsole.exe manually<br>"
            "• <b>Go to Settings</b> to type or paste the path<br><br>"
            "<small>The tool cannot list or backup instances without ldconsole.exe.</small>"
        )
        btn_browse = msg.addButton("Browse…", QMessageBox.AcceptRole)
        btn_settings = msg.addButton("Go to Settings", QMessageBox.RejectRole)
        msg.addButton("Dismiss", QMessageBox.DestructiveRole)
        msg.exec_()

        clicked = msg.clickedButton()
        if clicked == btn_browse:
            from PyQt5.QtWidgets import QFileDialog
            path, _ = QFileDialog.getOpenFileName(
                self, "Select ldconsole.exe", "", "Executable (*.exe)"
            )
            if path and os.path.isfile(path):
                self._settings.ldconsole_path = path
                save_settings(self._settings)
                self._ldconsole = LDConsoleWrapper(path)
                self._apply_settings(self._settings)
                self._load_instances()
        elif clicked == btn_settings:
            self._tabs.setCurrentWidget(self._tab_settings)

        self._lbl_status.setText("⚠️ ldconsole.exe not configured — go to Settings tab.")

    # ------------------------------------------------------------------
    # Instance loading
    # ------------------------------------------------------------------
    def _load_instances(self):
        if not self._ldconsole:
            self._lbl_status.setText("⚠️ ldconsole.exe not configured.")
            return
        self._lbl_status.setText("Loading instances…")
        self._loader_thread = InstanceLoaderThread(self._ldconsole, self)
        self._loader_thread.instances_loaded.connect(self._on_instances_loaded)
        self._loader_thread.error.connect(self._on_instances_error)
        self._loader_thread.start()

    @pyqtSlot(list)
    def _on_instances_loaded(self, instances: List[InstanceInfo]):
        self._instances = instances
        self._tab_dashboard.load_instances(instances)
        self._tab_backup.load_instances(instances)
        self._tab_restore.load_instances(instances)
        self._tab_create.update_instances(instances)
        self._tab_backup.check_resume()

        count = len(instances)
        self._lbl_status.setText(f"Ready — {count} instance(s) loaded")
        self._lbl_instances_count.setText(f"{count} instances")
        logger.info("Loaded %d LDPlayer instance(s).", count)

    @pyqtSlot(str)
    def _on_instances_error(self, error: str):
        self._lbl_status.setText(f"❌ Failed to load instances: {error}")
        logger.error("Failed to load instances: %s", error)

    # ------------------------------------------------------------------
    @staticmethod
    def load_theme() -> str:
        qss_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "styles", "theme.qss"
        )
        if os.path.exists(qss_path):
            with open(qss_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""
