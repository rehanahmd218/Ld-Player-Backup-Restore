"""
Application settings — loaded from / saved to config.json next to the exe.
"""
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

_CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


@dataclass
class AppSettings:
    # Paths
    ldconsole_path: str = r"C:\LDPlayer\LDPlayer9\ldconsole.exe"
    backup_destination: str = ""
    restore_path: str = ""

    # Batch control
    batch_size: int = 5
    max_concurrency: int = 5

    # Retention: rename old backup to .old while new one runs, delete .old on success
    keep_old_on_failure: bool = True  # keep .old if new backup fails

    # Notifications
    discord_webhook_url: str = ""
    notify_on_failure: bool = True
    notify_on_session_complete: bool = True

    # Logging
    log_level: str = "INFO"

    # Runtime state
    last_backup_path: str = ""


_settings: Optional[AppSettings] = None


def load_settings() -> AppSettings:
    global _settings
    if _settings is not None:
        return _settings
    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _settings = AppSettings(**{k: v for k, v in data.items() if k in AppSettings.__dataclass_fields__})
            return _settings
        except Exception:
            pass
    _settings = AppSettings()
    return _settings


def save_settings(settings: AppSettings) -> None:
    global _settings
    _settings = settings
    try:
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(asdict(settings), f, indent=2)
    except Exception as e:
        from utils.logger import get_logger
        get_logger(__name__).error(f"Failed to save settings: {e}")


def get_settings() -> AppSettings:
    return load_settings()
