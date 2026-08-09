"""
Thin subprocess wrapper around ldconsole.exe.
All methods are synchronous (called from worker threads, not the main thread).
"""
import subprocess
import time
from dataclasses import dataclass
from typing import List, Tuple

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class InstanceInfo:
    index: int
    name: str
    top_window: str = ""
    pid: str = ""
    is_running: bool = False


class LDConsoleError(Exception):
    pass


class LDConsoleWrapper:
    """Wraps ldconsole.exe commands via subprocess."""

    def __init__(self, ldconsole_path: str):
        self.ldconsole_path = ldconsole_path

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run(self, *args, timeout: int = 60) -> Tuple[int, str, str]:
        cmd = [self.ldconsole_path] + [str(a) for a in args]
        logger.debug("CMD: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            logger.error("Timeout running: %s", " ".join(cmd))
            return -1, "", "Timeout"
        except FileNotFoundError:
            raise LDConsoleError(f"ldconsole.exe not found at: {self.ldconsole_path}")
        except Exception as e:
            logger.error("Subprocess error: %s", e)
            return -1, "", str(e)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_instances(self) -> List[InstanceInfo]:
        """
        Return all registered LDPlayer instances.
        LDPlayer 9 uses 'list2' (not 'list') to enumerate instances.
        'list' only returns the app version string.
        Output format: index,title,top-handle,bind-handle,android-int,pid,pid-vbox,width,height,dpi
        """
        # LDPlayer 9 uses list2 for instance enumeration
        rc, out, err = self._run("list2")
        logger.debug("list2 rc=%d  stdout=%r  stderr=%r", rc, out[:200], err[:100])

        instances: List[InstanceInfo] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            try:
                idx = int(parts[0])          # must be a number
                name = parts[1] if len(parts) > 1 else f"emulator-{idx}"
                top_w = parts[2] if len(parts) > 2 else ""
                pid = parts[5] if len(parts) > 5 else ""
                instances.append(InstanceInfo(index=idx, name=name, top_window=top_w, pid=pid))
            except (ValueError, IndexError):
                # Skip non-data lines (headers, blank lines, etc.)
                continue
        logger.debug("Found %d instances", len(instances))
        return instances

    def is_running(self, index: int) -> bool:
        rc, out, err = self._run("isrunning", "--index", index, timeout=15)
        return "running" in out.lower()

    def stop_instance(self, index: int, wait_seconds: int = 30) -> bool:
        """Gracefully stop an instance. Returns True if stopped (or was already stopped)."""
        if not self.is_running(index):
            logger.debug("Instance %d already stopped.", index)
            return True
        logger.info("Stopping instance %d …", index)
        self._run("quit", "--index", index, timeout=30)
        # Poll until stopped
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if not self.is_running(index):
                logger.info("Instance %d stopped.", index)
                return True
            time.sleep(2)
        logger.warning("Instance %d did not stop within %ds", index, wait_seconds)
        return False

    def backup(self, index: int, file_path: str) -> Tuple[bool, str]:
        """Run backup. file_path should include filename (e.g. .ldbk)."""
        logger.info("Backing up instance %d → %s", index, file_path)
        rc, out, err = self._run("backup", "--index", index, "--file", file_path, timeout=900)
        if rc == 0:
            return True, out
        logger.error("Backup failed for index %d: %s", index, err or out)
        return False, err or out

    def restore(self, index: int, file_path: str) -> Tuple[bool, str]:
        """Restore from a backup file into the given instance index."""
        logger.info("Restoring instance %d ← %s", index, file_path)
        rc, out, err = self._run("restore", "--index", index, "--file", file_path, timeout=900)
        if rc == 0:
            return True, out
        logger.error("Restore failed for index %d: %s", index, err or out)
        return False, err or out

    def add_instance(self, name: str) -> Tuple[bool, str]:
        """Create a new empty instance with the given name."""
        logger.info("Creating new instance: %s", name)
        rc, out, err = self._run("add", "--name", name, timeout=60)
        # ldconsole returns rc=1 even when add succeeds (silent success = empty stdout+stderr)
        # Treat as failure only if stderr contains an actual error message
        is_error = bool(err and ("error" in err.lower() or "fail" in err.lower()))
        if not is_error:
            return True, out
        logger.error("Add instance failed: %s", err)
        return False, err

    def copy_instance(self, source_index: int, new_name: str) -> Tuple[bool, str]:
        """Clone an existing instance. Correct syntax: copy --name NAME --from INDEX."""
        logger.info("Copying instance #%d → %s", source_index, new_name)
        rc, out, err = self._run("copy", "--name", new_name, "--from", str(source_index), timeout=120)
        # Success = stdout does NOT contain the help page
        is_help_page = "Commands :" in out
        is_error = bool(err and ("error" in err.lower() or "fail" in err.lower()))
        if not is_help_page and not is_error:
            return True, out
        logger.error("Copy instance failed (src=%d → %s): %s", source_index, new_name, err or out[:80])
        return False, err or "Unknown error (copy command returned help page)"
