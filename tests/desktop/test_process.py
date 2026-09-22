"""The real entrypoint as a child process: starts on loopback, answers
``/health`` and the guarded status route, and exits cleanly on an interrupt
(Ctrl-Break on Windows, SIGINT elsewhere), taking its browser with it."""

import os
import signal
import subprocess
import sys
import time

import httpx
import pytest

from app.execution.playwright import is_available
from app.security import DASHBOARD_COOKIE

pytestmark = pytest.mark.skipif(os.environ.get("CAREEROS_SKIP_PROCESS_TESTS") == "1", reason="process tests disabled")


def _spawn(extra_args, port, state_dir=None):
    env = {**os.environ, "APP_ENV": "test", "API_KEY": "process-test-key"}
    if state_dir is not None:
        # The notification cursor file goes to a scratch directory, never the
        # developer's own .cache/desktop; toasts are never shown by a test.
        env["DESKTOP_STATE_DIR"] = str(state_dir)
        extra_args = [*extra_args, "--no-native-notifications", "--notification-interval", "1"]
    creation = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    return subprocess.Popen(
        [sys.executable, "-m", "app.desktop", "--no-window", "--port", str(port), *extra_args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creation,
    )


def _wait_health(port, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return None


def _interrupt(process):
    if sys.platform == "win32":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.send_signal(signal.SIGINT)


def _chromium_count():
    if sys.platform != "win32":
        return None
    out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*ms-playwright*' }).Count"], capture_output=True, text=True, timeout=60).stdout.strip()
    return int(out or 0)


@pytest.mark.parametrize("with_worker", [False, pytest.param(True, marks=pytest.mark.skipif(not is_available(), reason="playwright not installed"))])
def test_entrypoint_serves_and_exits_cleanly_on_interrupt(with_worker, tmp_path):
    from app.desktop.launcher import find_free_port

    port = find_free_port()
    process = _spawn(["--worker"] if with_worker else [], port, state_dir=tmp_path)
    try:
        health = _wait_health(port)
        assert health is not None, process.stdout.read() if process.poll() is not None else "no health within 60s"
        assert health["status"] == "ok"
        status = httpx.get(f"http://127.0.0.1:{port}/desktop/status", cookies={DASHBOARD_COOKIE: "process-test-key"}, timeout=5).json()
        assert status["server"]["running"] and status["readiness"]["ready"]
        if with_worker:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and status["worker"]["state"] != "running":
                time.sleep(0.5)
                status = httpx.get(f"http://127.0.0.1:{port}/desktop/status", cookies={DASHBOARD_COOKIE: "process-test-key"}, timeout=5).json()
            assert status["worker"]["state"] == "running" and status["worker"]["dry_run"] is True, status["worker"]
        else:
            assert status["worker"] == {"state": "disabled"}
        # Increment 5: the notification poller runs by default, in its own
        # supervised thread, and has completed at least one poll.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (status["notifications"].get("totals") or {}).get("polls"):
            time.sleep(0.5)
            status = httpx.get(f"http://127.0.0.1:{port}/desktop/status", cookies={DASHBOARD_COOKIE: "process-test-key"}, timeout=5).json()
        assert status["notifications"]["state"] == "running", status["notifications"]
        assert status["notifications"]["totals"]["polls"] >= 1 and status["notifications"]["totals"]["errors"] == 0, status["notifications"]
        assert (tmp_path / "notifications.json").exists(), "the cursor file lives in DESKTOP_STATE_DIR"
        assert httpx.get(f"http://127.0.0.1:{port}/desktop/status", timeout=5).status_code == 401
        _interrupt(process)
        output = process.communicate(timeout=60)[0]
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=30)
    assert process.returncode == 0, output[-2000:]
    assert "process-test-key" not in output, "the key is never logged"
    assert "Notification poller stopped" in output, output[-2000:]
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
    if with_worker:
        # The worker's own shutdown line proves its browser was closed; a global
        # Chromium count is not asserted because other Playwright tests may run
        # concurrently in another pytest chunk.
        assert "Local worker desktop-worker stopped" in output, output[-2000:]
