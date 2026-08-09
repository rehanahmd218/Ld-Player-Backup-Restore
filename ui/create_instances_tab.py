"""
Create Instances Tab
====================
Lets you bulk-create LDPlayer instances with a custom naming pattern.

• Name prefix + start number + count → previews names live
• Sequential creation via QThread (one at a time for stability)
• Real-time per-instance status list
• Optional: copy from an existing instance instead of blank add
"""
from typing import List, Optional

from PyQt5.QtCore import QThread, Qt, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config.settings import AppSettings
from core.ldconsole import InstanceInfo, LDConsoleWrapper
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Worker thread — creates instances one by one (sequential)
# ---------------------------------------------------------------------------
class CreateWorker(QThread):
    instance_created = pyqtSignal(str, bool, str)   # name, success, message
    all_done = pyqtSignal(int, int)                  # success_count, fail_count

    def __init__(self, names: List[str], ldconsole: LDConsoleWrapper,
                 copy_from_index: int = -1, resolution: str = "", cpu: int = 0, memory: int = 0, root: int = -1, adb: int = -1, parent=None):
        super().__init__(parent)
        self._names = names
        self._ldconsole = ldconsole
        self._copy_from = copy_from_index
        self._resolution = resolution
        self._cpu = cpu
        self._memory = memory
        self._root = root
        self._adb = adb
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        success = 0
        failed = 0
        for name in self._names:
            if self._cancelled:
                break
            try:
                if self._copy_from >= 0:
                    ok, msg = self._ldconsole.copy_instance(self._copy_from, name)
                else:
                    ok, msg = self._ldconsole.add_instance(name)

                if ok:
                    if self._resolution or self._cpu > 0 or self._memory > 0 or self._root >= 0 or self._adb >= 0:
                        mod_ok, mod_msg = self._ldconsole.modify_instance(
                            name, self._resolution, self._cpu, self._memory, self._root, self._adb
                        )
                        if not mod_ok:
                            logger.error("Failed to modify %s: %s", name, mod_msg)
                            msg = f"Created, but modify failed: {mod_msg}"
                    success += 1
                    logger.info("Created instance: %s", name)
                else:
                    failed += 1
                    logger.error("Failed to create %s: %s", name, msg)
                self.instance_created.emit(name, ok, msg or "")
            except Exception as e:
                failed += 1
                self.instance_created.emit(name, False, str(e))
        self.all_done.emit(success, failed)


# ---------------------------------------------------------------------------
# Tab Widget
# ---------------------------------------------------------------------------
class CreateInstancesTab(QWidget):
    instances_created = pyqtSignal()  # so MainWindow can refresh the instance list

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ldconsole: Optional[LDConsoleWrapper] = None
        self._settings: Optional[AppSettings] = None
        self._existing_instances: List[InstanceInfo] = []
        self._worker: Optional[CreateWorker] = None
        self._init_ui()

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        # Title
        title = QLabel("Create Instances")
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #e6edf3;")
        root.addWidget(title)
        sub = QLabel(
            "Quickly create multiple LDPlayer instances with sequential names. "
            "You can optionally copy from an existing instance."
        )
        sub.setStyleSheet("color: #8b949e; font-size: 12px;")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # --- Configuration -------------------------------------------
        cfg_group = QGroupBox("Configuration")
        cg = QVBoxLayout(cfg_group)

        # Name prefix + separator + start number + count
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Name prefix:"))
        self._prefix = QLineEdit("LDPlayer")
        self._prefix.setMaximumWidth(160)
        self._prefix.setToolTip("e.g. 'emulator' → produces emulator-0, emulator-1, …")
        self._prefix.textChanged.connect(self._update_preview)
        row1.addWidget(self._prefix)

        row1.addWidget(QLabel("Separator:"))
        self._sep = QLineEdit("-")
        self._sep.setMaximumWidth(50)
        self._sep.textChanged.connect(self._update_preview)
        row1.addWidget(self._sep)

        row1.addWidget(QLabel("Start number:"))
        self._start_num = QSpinBox()
        self._start_num.setRange(0, 9999)
        self._start_num.setValue(0)
        self._start_num.setFixedWidth(75)
        self._start_num.valueChanged.connect(self._update_preview)
        row1.addWidget(self._start_num)

        row1.addWidget(QLabel("Count:"))
        self._count = QSpinBox()
        self._count.setRange(1, 500)
        self._count.setValue(5)
        self._count.setFixedWidth(75)
        self._count.valueChanged.connect(self._update_preview)
        row1.addWidget(self._count)

        row1.addStretch()
        cg.addLayout(row1)

        # Copy from existing instance (optional)
        row2 = QHBoxLayout()
        self._chk_copy = QCheckBox("Copy from existing instance:")
        self._chk_copy.toggled.connect(self._on_copy_toggled)
        row2.addWidget(self._chk_copy)
        self._combo_copy = QComboBox()
        self._combo_copy.setEnabled(False)
        self._combo_copy.setMinimumWidth(200)
        self._combo_copy.setToolTip("Creates a copy of this instance for each new entry")
        row2.addWidget(self._combo_copy)
        row2.addStretch()
        cg.addLayout(row2)

        # Hardware properties
        row3 = QHBoxLayout()
        
        row3.addWidget(QLabel("CPU cores:"))
        self._cpu_combo = QComboBox()
        self._cpu_combo.addItems(["Default", "1", "2", "3", "4"])
        row3.addWidget(self._cpu_combo)
        
        row3.addWidget(QLabel("Memory:"))
        self._mem_combo = QComboBox()
        self._mem_combo.addItems(["Default", "256", "512", "768", "1024", "1536", "2048", "4096", "8192"])
        row3.addWidget(self._mem_combo)

        self._chk_root = QCheckBox("Root")
        self._chk_root.setToolTip("Enable root access")
        row3.addWidget(self._chk_root)

        row3.addWidget(QLabel("ADB:"))
        self._adb_combo = QComboBox()
        self._adb_combo.addItems(["Default", "Close", "Local Connection"])
        row3.addWidget(self._adb_combo)
        
        row3.addStretch()
        cg.addLayout(row3)
        
        # Resolution properties
        row4 = QHBoxLayout()
        self._chk_res = QCheckBox("Override Resolution:")
        self._chk_res.toggled.connect(self._on_res_toggled)
        row4.addWidget(self._chk_res)
        
        row4.addWidget(QLabel("View:"))
        self._view_combo = QComboBox()
        self._view_combo.addItems(["Tablet", "Mobile"])
        self._view_combo.currentIndexChanged.connect(self._on_view_changed)
        self._view_combo.setEnabled(False)
        row4.addWidget(self._view_combo)
        
        row4.addWidget(QLabel("W:"))
        self._res_w = QSpinBox()
        self._res_w.setRange(1, 7680)
        self._res_w.setValue(1280)
        self._res_w.setEnabled(False)
        row4.addWidget(self._res_w)
        
        row4.addWidget(QLabel("H:"))
        self._res_h = QSpinBox()
        self._res_h.setRange(1, 7680)
        self._res_h.setValue(720)
        self._res_h.setEnabled(False)
        row4.addWidget(self._res_h)
        
        row4.addWidget(QLabel("DPI:"))
        self._res_dpi = QSpinBox()
        self._res_dpi.setRange(1, 1000)
        self._res_dpi.setValue(240)
        self._res_dpi.setEnabled(False)
        row4.addWidget(self._res_dpi)
        
        row4.addStretch()
        cg.addLayout(row4)

        root.addWidget(cfg_group)

        # --- Preview -------------------------------------------------
        prev_group = QGroupBox("Preview — Instance Names to be Created")
        pg = QVBoxLayout(prev_group)

        self._preview_list = QListWidget()
        self._preview_list.setMaximumHeight(150)
        self._preview_list.setStyleSheet(
            "font-family: 'Consolas', monospace; font-size: 12px;"
        )
        pg.addWidget(self._preview_list)

        root.addWidget(prev_group)

        # --- Progress ------------------------------------------------
        prog_group = QGroupBox("Creation Progress")
        pgl = QVBoxLayout(prog_group)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFixedHeight(10)
        self._progress_bar.setTextVisible(False)
        pgl.addWidget(self._progress_bar)

        self._lbl_prog = QLabel("Configure above and click Create.")
        self._lbl_prog.setStyleSheet("color: #8b949e;")
        pgl.addWidget(self._lbl_prog)

        root.addWidget(prog_group)

        # Status list
        status_group = QGroupBox("Status")
        sl = QVBoxLayout(status_group)
        self._status_list = QListWidget()
        self._status_list.setMaximumHeight(160)
        sl.addWidget(self._status_list)
        root.addWidget(status_group, 1)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._btn_create = QPushButton("➕  Create Instances")
        self._btn_create.setObjectName("btn_primary")
        self._btn_create.setMinimumWidth(180)
        self._btn_create.clicked.connect(self._on_create)
        btn_row.addWidget(self._btn_create)

        self._btn_cancel = QPushButton("■  Cancel")
        self._btn_cancel.setObjectName("btn_danger")
        self._btn_cancel.setEnabled(False)
        self._btn_cancel.clicked.connect(self._on_cancel)
        btn_row.addWidget(self._btn_cancel)
        root.addLayout(btn_row)

        # Initial preview
        self._update_preview()

    # ------------------------------------------------------------------
    def setup(self, ldconsole: Optional[LDConsoleWrapper], settings: AppSettings):
        self._ldconsole = ldconsole
        self._settings = settings

    def update_instances(self, instances: List[InstanceInfo]):
        self._existing_instances = instances
        self._combo_copy.clear()
        for inst in instances:
            self._combo_copy.addItem(f"#{inst.index} — {inst.name}", userData=inst.index)

    # ------------------------------------------------------------------
    def _build_names(self) -> List[str]:
        prefix = self._prefix.text().strip() or "LDPlayer"
        sep = self._sep.text()
        start = self._start_num.value()
        count = self._count.value()
        return [f"{prefix}{sep}{start + i}" for i in range(count)]

    def _update_preview(self):
        names = self._build_names()
        self._preview_list.clear()
        for name in names[:50]:  # cap preview at 50
            self._preview_list.addItem(name)
        if len(names) > 50:
            self._preview_list.addItem(f"… and {len(names) - 50} more")

    def _on_copy_toggled(self, checked: bool):
        self._combo_copy.setEnabled(checked)

    @pyqtSlot(bool)
    def _on_res_toggled(self, checked: bool):
        self._view_combo.setEnabled(checked)
        self._res_w.setEnabled(checked)
        self._res_h.setEnabled(checked)
        self._res_dpi.setEnabled(checked)

    @pyqtSlot(int)
    def _on_view_changed(self, index: int):
        w = self._res_w.value()
        h = self._res_h.value()
        if index == 0 and w < h:
            self._res_w.setValue(h)
            self._res_h.setValue(w)
        elif index == 1 and h <= w:
            self._res_w.setValue(h)
            self._res_h.setValue(w)

    # ------------------------------------------------------------------
    @pyqtSlot()
    def _on_create(self):
        if not self._ldconsole:
            QMessageBox.warning(self, "Not Ready", "Configure ldconsole.exe path in Settings.")
            return

        names = self._build_names()
        copy_idx = -1
        if self._chk_copy.isChecked() and self._combo_copy.currentData() is not None:
            copy_idx = int(self._combo_copy.currentData())

        confirm = QMessageBox.question(
            self, "Confirm",
            f"Create {len(names)} new instance(s)?\n\nFirst: {names[0]}\nLast:  {names[-1]}",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        self._status_list.clear()
        self._progress_bar.setValue(0)
        self._lbl_prog.setText(f"Creating {len(names)} instance(s)…")
        self._btn_create.setEnabled(False)
        self._btn_cancel.setEnabled(True)

        cpu_val = 0
        if self._cpu_combo.currentIndex() > 0:
            cpu_val = int(self._cpu_combo.currentText())
            
        mem_val = 0
        if self._mem_combo.currentIndex() > 0:
            mem_val = int(self._mem_combo.currentText())
            
        res_val = ""
        if self._chk_res.isChecked():
            res_val = f"{self._res_w.value()},{self._res_h.value()},{self._res_dpi.value()}"

        root_val = 1 if self._chk_root.isChecked() else -1
        
        adb_val = -1
        if self._adb_combo.currentIndex() == 1:
            adb_val = 0
        elif self._adb_combo.currentIndex() == 2:
            adb_val = 1

        self._worker = CreateWorker(names, self._ldconsole, copy_idx, res_val, cpu_val, mem_val, root_val, adb_val, self)
        self._worker.instance_created.connect(self._on_instance_created)
        self._worker.all_done.connect(self._on_all_done)
        self._worker.start()
        self._total = len(names)
        self._done = 0

    @pyqtSlot()
    def _on_cancel(self):
        if self._worker:
            self._worker.cancel()
        self._btn_create.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._lbl_prog.setText("Cancelled.")

    @pyqtSlot(str, bool, str)
    def _on_instance_created(self, name: str, ok: bool, msg: str):
        self._done += 1
        pct = int(100 * self._done / self._total) if self._total else 0
        self._progress_bar.setValue(pct)
        self._lbl_prog.setText(f"Creating… {self._done}/{self._total}")

        item = QListWidgetItem(f"{'✅' if ok else '❌'}  {name}  {('— ' + msg[:60]) if msg and not ok else ''}")
        item.setForeground(Qt.green if ok else Qt.red)
        self._status_list.addItem(item)
        self._status_list.scrollToBottom()

    @pyqtSlot(int, int)
    def _on_all_done(self, success: int, failed: int):
        self._progress_bar.setValue(100)
        self._btn_create.setEnabled(True)
        self._btn_cancel.setEnabled(False)
        self._lbl_prog.setText(
            f"Done — {success} created successfully, {failed} failed."
        )
        self.instances_created.emit()  # signal MainWindow to reload instance list
        QMessageBox.information(
            self, "Done",
            f"{success} instance(s) created.\n{failed} failed.\n\n"
            "Click '↻ Refresh' in the Backup tab to see the updated list."
        )
