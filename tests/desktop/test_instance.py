"""One desktop per state directory: a second launch starts nothing, a crashed one never blocks."""

import json
import os
import subprocess
import sys
import time

import httpx
import pytest

from app.desktop import __main__ as entry
from app.desktop import instance
from app.desktop.instance import ALREADY_RUNNING, InstanceLock


def test_a_held_lock_refuses_a_second_owner_and_is_free_again_after_release(tmp_path):
    first, second = InstanceLock(tmp_path), InstanceLock(tmp_path)
    assert first.acquire() is True and first.held
    assert second.acquire() is False and not second.held, "a second handle cannot take the desktop"
    first.record(8123)
    info = second.read_info()
    assert info["port"] == 8123 and info["pid"] == os.getpid()
    assert set(info) == {"pid", "port", "started_at"}, "no credential or anything else in the info file"
    first.release()
    assert not (tmp_path / "desktop.json").exists()
    assert second.acquire() is True, "released: the next start proceeds"
    second.release()


def test_a_stale_info_file_from_a_crashed_instance_never_blocks(tmp_path):
    (tmp_path / "desktop.json").write_text(json.dumps({"pid": 999999, "port": 1, "started_at": 0}), encoding="utf-8")
    (tmp_path / "desktop.lock").write_bytes(b"")
    lock = InstanceLock(tmp_path)
    assert lock.acquire() is True, "only a live lock blocks, never a leftover file"
    lock.release()


def test_a_held_but_silent_instance_is_described_not_mistaken_for_a_running_one(tmp_path, monkeypatch):
    owner = InstanceLock(tmp_path)
    assert owner.acquire()
    owner.record(1)
    try:
        monkeypatch.setattr(instance, "answering", lambda port, timeout=2.0: False)
        message, answering = instance.already_running_message(InstanceLock(tmp_path))
        assert answering is False and "not answering" in message and str(os.getpid()) in message
        monkeypatch.setattr(instance, "answering", lambda port, timeout=2.0: True)
        message, answering = instance.already_running_message(InstanceLock(tmp_path))
        assert answering is True and "already running" in message and "http://127.0.0.1:1" in message
    finally:
        owner.release()


def test_second_launch_through_the_entrypoint_starts_nothing(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "desktop_state_dir", str(tmp_path))
    told = []
    monkeypatch.setattr(entry, "_tell_person", lambda message, icon=None: told.append(message))
    monkeypatch.setattr(instance, "answering", lambda port, timeout=2.0: True)
    monkeypatch.setattr(instance, "bring_window_forward", lambda title="CareerOS": False)

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("a second launch must not build a DesktopApp")

    monkeypatch.setattr("app.desktop.launcher.DesktopApp", Boom)
    owner = InstanceLock(tmp_path)
    assert owner.acquire()
    owner.record(8000)
    try:
        assert entry.main(["--no-window"]) == ALREADY_RUNNING
    finally:
        owner.release()
    assert told and "already running" in told[0]


def test_focusing_the_running_window_replaces_the_message_box(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "desktop_state_dir", str(tmp_path))
    told = []
    monkeypatch.setattr(entry, "_tell_person", lambda message, icon=None: told.append(message))
    monkeypatch.setattr(instance, "answering", lambda port, timeout=2.0: True)
    monkeypatch.setattr(instance, "bring_window_forward", lambda title="CareerOS": True)
    owner = InstanceLock(tmp_path)
    assert owner.acquire()
    try:
        assert entry.main(["--no-window"]) == ALREADY_RUNNING
    finally:
        owner.release()
    assert told == [], "the window came forward: no extra dialog"


@pytest.mark.skipif(os.environ.get("CAREEROS_SKIP_PROCESS_TESTS") == "1", reason="process tests disabled")
def test_two_real_launches_leave_one_running_desktop_and_a_restart_works(tmp_path):
    """The pilot reproduction, as a regression: A runs; B exits at once with
    ALREADY_RUNNING and never listens; after A stops, C starts normally."""
    from app.desktop.launcher import find_free_port

    env = {**os.environ, "APP_ENV": "test", "API_KEY": "instance-test-key", "DESKTOP_STATE_DIR": str(tmp_path)}
    creation = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

    def spawn(port):
        return subprocess.Popen([sys.executable, "-m", "app.desktop", "--no-window", "--no-native-notifications", "--port", str(port)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=creation)

    def healthy(port, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        return False

    def settled(port, seconds=60):
        """Ready for Ctrl-Break: start() has finished (the poller runs), so the graceful handler is installed.

        /health answers before the worker and poller start; a break sent in that window ends the
        process with the Windows Ctrl-C status instead of a clean 0 (seen once under full-suite load).
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                status = httpx.get(f"http://127.0.0.1:{port}/desktop/status", cookies={"careeros_key": "instance-test-key"}, timeout=2).json()
                if (status.get("notifications") or {}).get("state") == "running":
                    time.sleep(1.0)
                    return True
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.5)
        return False

    def stop(proc):
        if proc.poll() is None:
            import signal

            proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        try:
            return proc.communicate(timeout=60)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.communicate(timeout=30)[0]

    port_a = find_free_port()
    a = spawn(port_a)
    try:
        assert healthy(port_a), "first instance did not come up"
        assert settled(port_a), "first instance did not finish starting"
        port_b = find_free_port()
        b = spawn(port_b)
        out_b = b.communicate(timeout=60)[0]
        assert b.returncode == ALREADY_RUNNING, out_b[-1500:]
        assert "already running" in out_b and f"127.0.0.1:{port_a}" in out_b
        assert "instance-test-key" not in out_b
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"http://127.0.0.1:{port_b}/health", timeout=1)
        assert healthy(port_a, 5), "the running instance is untouched"
    finally:
        out_a = stop(a)
    assert a.returncode == 0, out_a[-1500:]
    assert not (tmp_path / "desktop.json").exists(), "a clean stop removes the info file"
    port_c = find_free_port()
    c = spawn(port_c)
    try:
        assert healthy(port_c), "after a stop the next launch starts normally"
        assert settled(port_c), "the restarted instance did not finish starting"
    finally:
        stop(c)
    assert c.returncode == 0
