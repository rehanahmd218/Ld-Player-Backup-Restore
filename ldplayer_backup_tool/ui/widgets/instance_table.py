"""
Instance table widget — sortable/filterable table of LDPlayer instances.
"""
from typing import List

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.ldconsole import InstanceInfo

_COLUMNS = ["#", "Index", "Name", "Status"]


class InstanceTable(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_instances: List[InstanceInfo] = []
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Search bar
        search_row = QHBoxLayout()
        lbl = QLabel("🔍")
        search_row.addWidget(lbl)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter instances by name…")
        self._search.textChanged.connect(self._apply_filter)
        search_row.addWidget(self._search)

        self._lbl_count = QLabel("0 instances")
        self._lbl_count.setObjectName("label_muted")
        search_row.addWidget(self._lbl_count)
        layout.addLayout(search_row)

        # Table
        self._table = QTableWidget()
        self._table.setColumnCount(len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self._table)

    def load_instances(self, instances: List[InstanceInfo]):
        self._all_instances = instances
        self._apply_filter(self._search.text())

    def _apply_filter(self, text: str):
        text = text.lower().strip()
        filtered = [
            i for i in self._all_instances
            if not text or text in i.name.lower() or text in str(i.index)
        ]
        self._populate(filtered)

    def _populate(self, instances: List[InstanceInfo]):
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(instances))
        for row, inst in enumerate(instances):
            # Row number
            n_item = QTableWidgetItem(str(row + 1))
            n_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 0, n_item)

            # Index
            idx_item = QTableWidgetItem(str(inst.index))
            idx_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 1, idx_item)

            # Name
            self._table.setItem(row, 2, QTableWidgetItem(inst.name))

            # Status
            status_item = QTableWidgetItem("Running" if inst.is_running else "Stopped")
            status_item.setForeground(
                Qt.green if inst.is_running else Qt.gray
            )
            status_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 3, status_item)

        self._table.setSortingEnabled(True)
        self._lbl_count.setText(f"{len(instances)} instance{'s' if len(instances) != 1 else ''}")

    def get_all_instances(self) -> List[InstanceInfo]:
        return list(self._all_instances)
