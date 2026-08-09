"""
Live log viewer widget.
Displays colored log lines in real-time as the backup/restore engine runs.
"""
from PyQt5.QtCore import Qt, pyqtSlot
from PyQt5.QtGui import QColor, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QFileDialog,
)

_LEVEL_COLORS = {
    "DEBUG":    "#484f58",
    "INFO":     "#c9d1d9",
    "WARNING":  "#d29922",
    "ERROR":    "#f85149",
    "CRITICAL": "#ff7b72",
    "SUCCESS":  "#3fb950",  # custom level used by engine
}

MAX_LINES = 5000  # keep log viewer from growing unbounded


class LogViewer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Toolbar
        toolbar = QHBoxLayout()
        lbl = QLabel("Live Logs")
        lbl.setObjectName("label_heading")
        lbl.setStyleSheet("font-size: 15px; font-weight: 700; color: #e6edf3;")
        toolbar.addWidget(lbl)
        toolbar.addStretch()

        self._btn_clear = QPushButton("Clear")
        self._btn_clear.clicked.connect(self._clear)
        toolbar.addWidget(self._btn_clear)

        self._btn_export = QPushButton("Export…")
        self._btn_export.clicked.connect(self._export)
        toolbar.addWidget(self._btn_export)

        self._btn_scroll_lock = QPushButton("⏸ Pause Scroll")
        self._btn_scroll_lock.setCheckable(True)
        toolbar.addWidget(self._btn_scroll_lock)

        layout.addLayout(toolbar)

        # Text area
        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QTextEdit.NoWrap)
        self._text.setStyleSheet(
            "font-family: 'Segoe UI', sans-serif; font-size: 14px; line-height: 1.5;"
        )
        layout.addWidget(self._text)

    @pyqtSlot(str, str)
    def append_log(self, level: str, message: str):
        """Slot connected to LogBridge.log_emitted signal."""
        color_hex = _LEVEL_COLORS.get(level.upper(), "#c9d1d9")

        # Add a friendly prefix so non-technical users can read severity
        prefix_map = {
            "DEBUG":    "🔍 ",
            "INFO":     "ℹ️  ",
            "WARNING":  "⚠️  ",
            "ERROR":    "❌ ",
            "CRITICAL": "🚨 ",
            "SUCCESS":  "✅ ",
        }
        prefix = prefix_map.get(level.upper(), "")
        display_msg = prefix + message

        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.End)

        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color_hex))
        cursor.setCharFormat(fmt)
        cursor.insertText(display_msg + "\n")

        # Prune old lines
        doc = self._text.document()
        if doc.lineCount() > MAX_LINES:
            prune = QTextCursor(doc)
            prune.movePosition(QTextCursor.Start)
            prune.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor, doc.lineCount() - MAX_LINES)
            prune.removeSelectedText()

        if not self._btn_scroll_lock.isChecked():
            self._text.setTextCursor(cursor)
            self._text.ensureCursorVisible()

    def _clear(self):
        self._text.clear()

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Logs", "logs.txt", "Text Files (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._text.toPlainText())
