"""
Discord Webhook Notifier
========================
Sends embedded messages to a Discord channel via webhook.
No bot token required — just a webhook URL.

Two message types:
  1. Session complete summary  (after every full backup/restore run)
  2. Failure alert             (when an individual job fails, if configured)
"""
import time
from typing import List, Optional

import requests

from utils.helpers import format_duration
from utils.logger import get_logger

logger = get_logger(__name__)

# Discord color codes (decimal)
COLOR_SUCCESS = 0x2ECC71   # green
COLOR_WARNING = 0xE67E22   # orange
COLOR_ERROR = 0xE74C3C     # red
COLOR_INFO = 0x3498DB      # blue

_TIMEOUT = 10  # seconds


def _post(webhook_url: str, payload: dict) -> bool:
    try:
        resp = requests.post(webhook_url, json=payload, timeout=_TIMEOUT)
        if resp.status_code in (200, 204):
            return True
        logger.warning("Discord webhook returned %d: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        logger.error("Discord webhook error: %s", e)
        return False


def send_session_summary(
    webhook_url: str,
    *,
    operation: str,  # "Backup" or "Restore"
    success_count: int,
    fail_count: int,
    total_count: int,
    duration_sec: float,
    failed_names: Optional[List[str]] = None,
) -> bool:
    """Send an embedded summary message after a complete backup/restore session."""
    if not webhook_url:
        return False

    total = success_count + fail_count
    pct = int(100 * success_count / total) if total else 0
    color = COLOR_SUCCESS if fail_count == 0 else (COLOR_ERROR if success_count == 0 else COLOR_WARNING)
    status_emoji = "✅" if fail_count == 0 else ("❌" if success_count == 0 else "⚠️")

    fields = [
        {"name": "✅ Succeeded", "value": str(success_count), "inline": True},
        {"name": "❌ Failed",    "value": str(fail_count),    "inline": True},
        {"name": "📊 Success Rate", "value": f"{pct}%",      "inline": True},
        {"name": "⏱ Duration",  "value": format_duration(duration_sec), "inline": True},
        {"name": "📦 Total Processed", "value": str(total_count), "inline": True},
    ]

    if failed_names:
        failed_list = "\n".join(f"• `{n}`" for n in failed_names[:20])
        if len(failed_names) > 20:
            failed_list += f"\n_…and {len(failed_names) - 20} more_"
        fields.append({"name": "Failed Instances", "value": failed_list, "inline": False})

    payload = {
        "embeds": [{
            "title": f"{status_emoji} LDPlayer {operation} Session Complete",
            "color": color,
            "fields": fields,
            "footer": {"text": "LDPlayer Backup Tool"},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }]
    }
    ok = _post(webhook_url, payload)
    if ok:
        logger.info("Discord session summary sent.")
    return ok


def send_failure_alert(
    webhook_url: str,
    *,
    operation: str,
    instance_index: int,
    instance_name: str,
    error: str,
) -> bool:
    """Send an alert when an individual job fails."""
    if not webhook_url:
        return False

    payload = {
        "embeds": [{
            "title": f"❌ {operation} Failed — Instance #{instance_index}",
            "description": f"**Instance:** `{instance_name}` (index {instance_index})\n**Error:** {error}",
            "color": COLOR_ERROR,
            "footer": {"text": "LDPlayer Backup Tool"},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }]
    }
    return _post(webhook_url, payload)


def test_webhook(webhook_url: str) -> bool:
    """Send a test message to verify the webhook URL works."""
    payload = {
        "embeds": [{
            "title": "🔔 LDPlayer Backup Tool — Webhook Test",
            "description": "Discord notifications are configured correctly!",
            "color": COLOR_INFO,
            "footer": {"text": "LDPlayer Backup Tool"},
        }]
    }
    return _post(webhook_url, payload)
