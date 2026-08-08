"""
SHA-256 checksum computation and verification.
"""
import hashlib
import os
from utils.logger import get_logger

logger = get_logger(__name__)

CHUNK_SIZE = 1024 * 1024  # 1 MB chunks for large backup files


def compute_sha256(file_path: str) -> str:
    """Compute SHA-256 hex digest of a file. Returns empty string on error."""
    try:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                h.update(chunk)
        digest = h.hexdigest()
        logger.debug("SHA-256 [%s] = %s", os.path.basename(file_path), digest)
        return digest
    except Exception as e:
        logger.error("Checksum error for %s: %s", file_path, e)
        return ""


def verify_checksum(file_path: str, expected: str) -> bool:
    """Return True if file's SHA-256 matches expected digest."""
    if not expected:
        logger.warning("No expected checksum provided for %s — skipping verify.", file_path)
        return True
    actual = compute_sha256(file_path)
    match = actual == expected
    if not match:
        logger.error("Checksum MISMATCH for %s: expected=%s actual=%s", file_path, expected, actual)
    return match
