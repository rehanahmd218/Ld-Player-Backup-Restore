"""
Centralized logging setup.
Writes to both a rotating file and broadcasts to UI via Qt signal.
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from PyQt5.QtCore import QObject, pyqtSignal


class _UILogHandler(logging.Handler):
    """Bridges Python logging to a Qt signal so the UI can display live logs."""

    def __init__(self, signal):
        super().__init__()
        self._signal = signal

    def emit(self, record):
        try:
            msg = self.format(record)
            self._signal.emit(record.levelname, msg)
        except Exception:
            pass


class LogBridge(QObject):
    """Qt object that holds the signal; must stay alive as long as logging is active."""
    log_emitted = pyqtSignal(str, str)  # level, message


_bridge: LogBridge | None = None
_ui_handler: _UILogHandler | None = None


def setup_logging(log_dir: str = "logs", level: str = "INFO") -> LogBridge:
    """Call once at startup. Returns the LogBridge so the UI can connect its slot."""
    global _bridge, _ui_handler

    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "app.log")

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler (5 MB × 3 backups)
    fh = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Qt bridge — uses a friendlier format (no module paths)
    ui_fmt = logging.Formatter("%(asctime)s — %(message)s", datefmt="%H:%M:%S")
    _bridge = LogBridge()
    _ui_handler = _UILogHandler(_bridge.log_emitted)
    _ui_handler.setFormatter(ui_fmt)
    root.addHandler(_ui_handler)

    return _bridge


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
