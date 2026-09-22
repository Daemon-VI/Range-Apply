"""One CareerOS desktop per state directory.

Found in the pilot (2026-09-13): launching the shortcut while CareerOS was
already open silently started a second copy on another port — a second
server, a second window, a second local worker with the same worker id and a
second notification poller writing the same cursor file (duplicate toasts).

The guard is an OS-level exclusive lock on ``DESKTOP_STATE_DIR/desktop.lock``
held for the life of the process. The operating system releases it when the
process ends, however it ends, so a crashed instance never blocks the next
start. ``desktop.json`` beside it says where the running instance listens
(pid, port, start time — never a credential) so a second launch can point
the person at it instead of starting anything.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import IO, Any, Callable, Optional

logger = logging.getLogger(__name__)

LOCK_NAME = "desktop.lock"
INFO_NAME = "desktop.json"
#: Exit code of a launch that found CareerOS already running (nothing started).
ALREADY_RUNNING = 3


class InstanceLock:
    """Non-blocking, process-lifetime exclusive lock plus a small info file."""

    def __init__(self, directory: str | os.PathLike):
        self.directory = Path(directory)
        self.lock_path = self.directory / LOCK_NAME
        self.info_path = self.directory / INFO_NAME
        self._handle: Optional[IO[bytes]] = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """True when this process now owns the desktop; False when another process does."""
        if self._handle is not None:
            return True
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+b")  # never truncate a file another process may hold
        try:
            _lock(handle)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def record(self, port: int) -> None:
        """Say where this instance listens (only while the lock is held)."""
        if self._handle is None:
            return
        data = {"pid": os.getpid(), "port": int(port), "started_at": time.time()}
        tmp = self.info_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.info_path)

    def mark_stopping(self) -> None:
        """Tell a new launch that this instance is shutting down (it may still answer /health)."""
        if self._handle is None:
            return
        data = {**self.read_info(), "pid": os.getpid(), "stopping": True, "stopping_at": time.time()}
        tmp = self.info_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.info_path)
        except OSError:
            pass

    def read_info(self) -> dict[str, Any]:
        try:
            data = json.loads(self.info_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self.info_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            _unlock(self._handle)
        except OSError:
            pass
        self._handle.close()
        self._handle = None


def _lock(handle: IO[bytes]) -> None:
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - the desktop shell targets Windows; kept correct elsewhere
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: IO[bytes]) -> None:
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def answering(port: Optional[int], timeout: float = 2.0) -> bool:
    """Whether the recorded instance still serves ``/health`` on the loopback."""
    if not port:
        return False
    try:
        import httpx

        return httpx.get(f"http://127.0.0.1:{int(port)}/health", timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001 - a dead or hung instance simply does not answer
        return False


def bring_window_forward(title: str = "CareerOS") -> bool:
    """Best effort: restore and focus the running instance's window (Windows only)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:  # noqa: BLE001 - focus is a courtesy, never a failure
        return False


def acquire_waiting_for_shutdown(lock: InstanceLock, timeout: float = 45.0, poll: float = 0.5, is_answering: Callable[[Optional[int]], bool] = None) -> bool:
    """Acquire the lock, waiting while the previous instance is still closing.

    Closing the window and clicking the shortcut again used to refuse the new
    launch ("already running") while the old one was still stopping its worker
    and autopilot. A launch now waits (up to ``timeout``) when the holder said it
    is stopping, or is not answering at all (starting or stopping). A holder
    that is running normally is not waited for: the window is brought forward.
    """
    if lock.acquire():
        return True
    check = is_answering or answering
    info = lock.read_info()
    if not info.get("stopping") and check(info.get("port")):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll)
        if lock.acquire():
            return True
    return False


def already_running_message(lock: InstanceLock) -> tuple[str, bool]:
    """What to tell the person, and whether the running instance answered."""
    info = lock.read_info()
    port, pid = info.get("port"), info.get("pid")
    if answering(port):
        return (f"CareerOS is already running (http://127.0.0.1:{port}). Use the open CareerOS window; a second copy was not started.", True)
    who = f" (process {pid})" if pid else ""
    return (
        f"Another CareerOS process{who} holds {lock.lock_path} but is not answering. "
        "It may still be starting or shutting down: wait a moment and try again, or end that process in Task Manager.",
        False,
    )
