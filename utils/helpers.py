"""
Miscellaneous helper utilities.
"""
import re
from datetime import datetime
from typing import List


def parse_index_range(range_str: str) -> List[int]:
    """
    Parse a range string like "0-100" or "0,5,10-20" into a sorted list of ints.
    Supports:
      - Single numbers: "5"
      - Ranges:         "0-100"
      - Comma lists:    "0,1,2"
      - Mixed:          "0-10,20,30-35"
    """
    indices: set[int] = set()
    for part in range_str.replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            indices.update(range(lo, hi + 1))
        elif re.fullmatch(r"\d+", part):
            indices.add(int(part))
        else:
            raise ValueError(f"Invalid range segment: '{part}'")
    return sorted(indices)


def format_bytes(size: int) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def format_duration(seconds: float) -> str:
    """Human-readable duration from seconds."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def now_iso() -> str:
    """Current UTC timestamp as ISO 8601 string."""
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def now_display() -> str:
    """Current local time for display."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_filename(name: str) -> str:
    """Strip characters that are unsafe in file/folder names."""
    return re.sub(r'[\\/:*?"<>|]', "_", name)
