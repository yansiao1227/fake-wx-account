"""Spawn a system Chrome/Edge with a DevTools debugging port for CDP control.

Why this exists: driving a system browser via Playwright's
``chromium.launch(channel="chrome")`` makes the app *take over* another app's
process, which on macOS triggers a TCC "Automation" permission prompt and a
multi-second (sometimes 100s+) stall on first use. Launching Chrome ourselves
with ``--remote-debugging-port`` and attaching via ``connect_over_cdp`` avoids
that entirely — from the OS's view it's just a process listening on a local
port — and matches how Codex / Claude Code drive the user's real browser.

The launched process uses an isolated ``--user-data-dir`` so it never fights
the user's day-to-day browser profile, while still persisting login state
across sessions inside that dir.
"""

import os
import sys
import time
import socket
import subprocess
import urllib.request
from typing import Optional, List, Tuple

from common.log import logger


def normalize_profile_path(path: str) -> str:
    """Canonical user-data-dir path for comparisons and Chrome flags."""
    resolved = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    if sys.platform == "win32":
        return os.path.normcase(resolved)
    return resolved


def command_uses_profile(command_line: str, profile: str) -> bool:
    """True when a Chrome/Edge command line is bound to *profile*."""
    if not command_line or not profile:
        return False
    haystack = command_line.replace("/", "\\") if sys.platform == "win32" else command_line
    needle = profile.replace("/", "\\") if sys.platform == "win32" else profile
    if sys.platform == "win32":
        return os.path.normcase(needle) in os.path.normcase(haystack)
    return needle in haystack


class ChromeLauncher:
    """Own the lifecycle of a debugging-enabled Chrome/Edge child process."""

    def __init__(self, executable: str, user_data_dir: str,
                 extra_args: Optional[List[str]] = None,
                 headless: bool = False):
        self._executable = executable
        self._user_data_dir = os.path.normpath(os.path.abspath(os.path.expanduser(user_data_dir)))
        self._extra_args = extra_args or []
        self._headless = headless
        self._proc: Optional[subprocess.Popen] = None
        self._port: Optional[int] = None
        self._owned_pid: Optional[int] = None

    @property
    def endpoint(self) -> str:
        """CDP HTTP endpoint (only valid after a successful launch())."""
        return f"http://127.0.0.1:{self._port}" if self._port else ""

    @staticmethod
    def _free_port() -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()

    def _clear_stale_singleton_locks(self):
        """Remove leftover Chrome Singleton* locks from a crashed/killed run.

        Chrome allows only one instance per user_data_dir and enforces it with
        SingletonLock / SingletonSocket / SingletonCookie. On a clean exit these
        are removed, but a crash or force-quit leaves them behind — the next
        spawn then hands off to the (dead) "existing" instance and exits without
        opening the debug port, so CDP never comes up (a permanent, non
        self-healing failure). This profile is private to us, so clearing stale
        locks before launch is safe: if our own browser were truly alive, the
        service would still be connected and we wouldn't be re-launching.
        """
        # Windows Chrome uses `lockfile` instead of the Unix Singleton* names.
        # A leftover lock from a previous detached spawn makes the next
        # chrome.exe hand off to that instance and immediately exit 0, so the
        # freshly chosen CDP port never comes up.
        for name in (
            "SingletonLock",
            "SingletonSocket",
            "SingletonCookie",
            "lockfile",
        ):
            p = os.path.join(self._user_data_dir, name)
            try:
                # These are symlinks; use lexists so a dangling link is caught.
                if os.path.lexists(p):
                    os.remove(p)
                    logger.info(f"[Browser] cleared stale Chrome lock: {name}")
            except OSError as e:
                logger.debug(f"[Browser] could not remove {name}: {e}")

    def launch(self, ready_timeout: float = 25.0) -> str:
        """Spawn Chrome and block until its CDP endpoint answers.

        Returns the CDP endpoint URL. Raises RuntimeError if the endpoint never
        comes up (the child process is killed in that case).
        """
        os.makedirs(self._user_data_dir, exist_ok=True)
        if self._adopt_existing():
            logger.info(
                f"[Browser] Reusing existing {os.path.basename(self._executable)} "
                f"on CDP port {self._port} (profile={self._user_data_dir})"
            )
            return self.endpoint

        # A leftover instance from a previous crash / detached spawn can hold
        # this private profile without exposing a usable CDP port.
        self._terminate_profile_processes()
        self._clear_stale_singleton_locks()
        self._port = self._free_port()

        args = [
            self._executable,
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={self._user_data_dir}",
            # Trim first-run overhead and background chatter for faster starts.
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-features=Translate,OptimizationHints",
            # A blank first tab keeps startup cheap and predictable.
            "about:blank",
        ]
        if self._headless:
            args.insert(1, "--headless=new")
        args[1:1] = self._extra_args

        popen_kwargs = {}
        if sys.platform == "win32":
            # Hide the extra console, but do not DETACHED_PROCESS: that
            # orphans chrome.exe from this launcher, so close() cannot
            # terminate it and the next launch finds the profile still locked.
            popen_kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            # New session so the child isn't tied to the parent's controlling
            # terminal / process group (clean teardown, no signal bleed).
            popen_kwargs["start_new_session"] = True

        logger.info(f"[Browser] Spawning {os.path.basename(self._executable)} "
                    f"on CDP port {self._port} (profile={self._user_data_dir})")
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **popen_kwargs,
        )

        if not self._wait_ready(ready_timeout):
            if self._adopt_existing():
                logger.info(
                    f"[Browser] Spawn handed off; reusing CDP port {self._port} "
                    f"(profile={self._user_data_dir})"
                )
                return self.endpoint
            # Capture the port before close() clears it, so the error is useful.
            port = self._port
            self.close()
            raise RuntimeError(
                f"Chrome did not expose a CDP endpoint on port {port} "
                f"within {ready_timeout:.0f}s"
            )
        if self._proc is not None:
            self._owned_pid = self._proc.pid
        return self.endpoint

    def _wait_ready(self, timeout: float) -> bool:
        """Poll DevTools /json/version until Chrome is listening (or times out)."""
        deadline = time.time() + timeout
        url = f"http://127.0.0.1:{self._port}/json/version"
        while time.time() < deadline:
            # The Windows chrome.exe stub may exit after handing off to an
            # already-running profile. Keep polling CDP instead of failing
            # immediately on that exit.
            try:
                with urllib.request.urlopen(url, timeout=1) as r:
                    if r.status == 200:
                        return True
            except Exception:
                time.sleep(0.15)
            if self._proc and self._proc.poll() is not None:
                existing = self._find_existing_cdp_port()
                if existing and self._probe_cdp(existing):
                    self._port = existing
                    self._adopt_pid_from_profile()
                    return True
        return False

    def _probe_cdp(self, port: int) -> bool:
        url = f"http://127.0.0.1:{port}/json/version"
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                return r.status == 200
        except Exception:
            return False

    def _read_devtools_active_port(self) -> Optional[int]:
        path = os.path.join(self._user_data_dir, "DevToolsActivePort")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                first = handle.readline().strip()
            port = int(first)
            return port if 0 < port < 65536 else None
        except (OSError, ValueError):
            return None

    def _find_existing_cdp_port(self) -> Optional[int]:
        port = self._read_devtools_active_port()
        if port and self._probe_cdp(port):
            return port
        for pid, command in self._iter_browser_processes():
            if "--type=" in (command or ""):
                continue
            if not command_uses_profile(command, self._user_data_dir):
                continue
            extracted = self._extract_debugging_port(command)
            if extracted and self._probe_cdp(extracted):
                return extracted
        return None

    @staticmethod
    def _extract_debugging_port(command_line: str) -> Optional[int]:
        marker = "--remote-debugging-port="
        idx = command_line.find(marker)
        if idx < 0:
            return None
        raw = command_line[idx + len(marker):].split()[0].strip().strip('"')
        try:
            port = int(raw)
        except ValueError:
            return None
        return port if 0 < port < 65536 else None

    def _adopt_existing(self) -> bool:
        port = self._find_existing_cdp_port()
        if not port:
            return False
        self._port = port
        self._adopt_pid_from_profile()
        return True

    def _adopt_pid_from_profile(self) -> None:
        for pid, command in self._iter_browser_processes():
            if "--type=" in (command or ""):
                continue
            if command_uses_profile(command, self._user_data_dir):
                self._owned_pid = pid
                return

    def _iter_browser_processes(self) -> List[Tuple[int, str]]:
        if sys.platform == "win32":
            return self._iter_windows_browser_processes()
        return self._iter_posix_browser_processes()

    def _iter_windows_browser_processes(self) -> List[Tuple[int, str]]:
        rows: List[Tuple[int, str]] = []
        try:
            import win32com.client
            wmi = win32com.client.GetObject("winmgmts:")
            for proc in wmi.ExecQuery(
                "SELECT ProcessId, CommandLine FROM Win32_Process "
                "WHERE Name='chrome.exe' OR Name='msedge.exe'"
            ):
                pid = int(proc.ProcessId)
                rows.append((pid, proc.CommandLine or ""))
            return rows
        except Exception as e:
            logger.debug(f"[Browser] WMI process listing failed: {e}")
        return rows

    def _iter_posix_browser_processes(self) -> List[Tuple[int, str]]:
        try:
            out = subprocess.check_output(
                ["ps", "-ao", "pid=,command="],
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except Exception:
            return []
        rows: List[Tuple[int, str]] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                rows.append((int(parts[0]), parts[1]))
            except ValueError:
                continue
        return rows

    def _terminate_profile_processes(self) -> None:
        """Kill leftover Chrome/Edge processes that still hold this profile."""
        killed = []
        for pid, command in self._iter_browser_processes():
            if "--type=" in (command or ""):
                continue
            if not command_uses_profile(command, self._user_data_dir):
                continue
            if self._kill_pid_tree(pid):
                killed.append(pid)
        if killed:
            logger.info(f"[Browser] terminated leftover profile processes: {killed}")
            time.sleep(0.4)

    def _kill_pid_tree(self, pid: int) -> bool:
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                return result.returncode == 0
            os.kill(pid, 15)
            return True
        except Exception as e:
            logger.debug(f"[Browser] could not terminate pid {pid}: {e}")
            return False

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self):
        """Terminate the spawned Chrome process (idempotent)."""
        proc = self._proc
        owned_pid = self._owned_pid
        self._proc = None
        self._port = None
        self._owned_pid = None
        if proc is None:
            if owned_pid:
                self._kill_pid_tree(owned_pid)
            return
        if proc.poll() is None:
            try:
                if sys.platform == "win32":
                    self._kill_pid_tree(proc.pid)
                    proc.wait(timeout=8)
                else:
                    proc.terminate()
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
            except Exception as e:
                logger.debug(f"[Browser] error terminating Chrome process: {e}")
        elif owned_pid and owned_pid != proc.pid:
            self._kill_pid_tree(owned_pid)
