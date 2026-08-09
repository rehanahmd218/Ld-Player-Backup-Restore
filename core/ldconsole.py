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

    def __init__(self, ldconsole_path: str):
        self.ldconsole_path = ldconsole_path
        self._proc_lock = __import__("threading").Lock()
        self._current_proc = None          # the live Popen object (if any)

    def kill_current(self):
        """Kill the subprocess that is currently running inside _run().
        Safe to call from any thread at any time."""
        with self._proc_lock:
            if self._current_proc is not None:
                try:
                    self._current_proc.kill()
                except OSError:
                    pass

    def _run(self, *args, timeout: int = 60) -> Tuple[int, str, str]:
        cmd = [self.ldconsole_path] + [str(a) for a in args]
        logger.debug("CMD: %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            with self._proc_lock:
                self._current_proc = proc
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                logger.error("Timeout running: %s", " ".join(cmd))
                return -1, "", "Timeout"
            finally:
                with self._proc_lock:
                    self._current_proc = None
            return proc.returncode, stdout.decode(errors="replace").strip(), stderr.decode(errors="replace").strip()
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

    def modify_instance(self, name: str, resolution: str = "", cpu: int = 0, memory: int = 0, root: int = -1, adb: int = -1) -> Tuple[bool, str]:
        """Modify an instance's settings. resolution is w,h,dpi (e.g. 1280,720,240)."""
        logger.info("Modifying instance: %s", name)
        args = ["modify", "--name", name]
        if resolution:
            args.extend(["--resolution", resolution])
        if cpu > 0:
            args.extend(["--cpu", str(cpu)])
        if memory > 0:
            args.extend(["--memory", str(memory)])
        if root >= 0:
            args.extend(["--root", str(root)])
            
        if len(args) > 3:
            rc, out, err = self._run(*args, timeout=60)
            is_error = bool(err and ("error" in err.lower() or "fail" in err.lower()))
            if is_error:
                logger.error("Modify instance failed: %s", err)
                return False, err

        if adb >= 0:
            instances = self.list_instances()
            idx = -1
            for inst in instances:
                if inst.name == name:
                    idx = inst.index
                    break
            if idx >= 0:
                import json
                import os
                config_path = os.path.join(os.path.dirname(self.ldconsole_path), "vms", "config", f"leidian{idx}.config")
                if os.path.exists(config_path):
                    try:
                        with open(config_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        data["basicSettings.adbDebug"] = adb
                        with open(config_path, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=4)
                    except Exception as e:
                        logger.error("Failed to update adbDebug config: %s", e)
                        return False, str(e)
            else:
                return False, f"Instance {name} not found to enable ADB"
                
        return True, "Modifications applied"
