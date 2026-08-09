"""
LDPlayer 9 Backup & Restore Tool
=================================
Entry point — bootstraps the PyQt5 application.
"""
import os
import sys

# Ensure the project root is on sys.path so all imports work
# regardless of where the script is launched from.
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PyQt5.QtGui import QFont, QIcon
from PyQt5.QtWidgets import QApplication

from ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("LDPlayer Backup Tool")
    app.setOrganizationName("LDBackup")
    app.setApplicationVersion("1.0.0")

    # Apply global font
    font = QFont("Segoe UI", 10)
    app.setFont(font)

    # Load and apply dark theme
    window = MainWindow()
    qss = window.load_theme()
    if qss:
        app.setStyleSheet(qss)

    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
    