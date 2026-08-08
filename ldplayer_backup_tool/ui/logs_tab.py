"""
Logs Tab — wraps the LogViewer widget with a header and level legend.
"""
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui.widgets.log_viewer import LogViewer


class LogsTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(8)

        # Level legend
        legend = QHBoxLayout()
        legend_items = [
            ("DEBUG",   "#484f58"),
            ("INFO",    "#c9d1d9"),
            ("WARNING", "#d29922"),
            ("ERROR",   "#f85149"),
        ]
        for level, color in legend_items:
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {color}; font-size: 14px;")
            lbl = QLabel(level)
            lbl.setStyleSheet(f"color: {color}; font-size: 11px; margin-right: 12px;")
            legend.addWidget(dot)
            legend.addWidget(lbl)
        legend.addStretch()
        root.addLayout(legend)

        self._viewer = LogViewer()
        root.addWidget(self._viewer)

    def get_viewer(self) -> LogViewer:
        return self._viewer
