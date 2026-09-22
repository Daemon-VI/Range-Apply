"""Closing the window and relaunching at once must start CareerOS, not refuse it.

2026-09-14: the old instance was still stopping autopilot and the worker (and
still answering /health) when the shortcut was clicked again; the new launch
said "already running" and exited, leaving nothing running.
"""

import threading
import time

from app.desktop.instance import InstanceLock, acquire_waiting_for_shutdown


def test_a_launch_waits_for_an_instance_that_is_stopping(tmp_path):
    old = InstanceLock(tmp_path)
    assert old.acquire()
    old.record(8000)
    old.mark_stopping()
    assert old.read_info()["stopping"] is True and old.read_info()["port"] == 8000

    threading.Timer(0.4, old.release).start()
    new = InstanceLock(tmp_path)
    started = time.monotonic()
    assert acquire_waiting_for_shutdown(new, timeout=10, poll=0.05, is_answering=lambda port: True)
    assert 0.3 < time.monotonic() - started < 5
    new.release()


def test_a_running_instance_is_not_waited_for(tmp_path):
    old = InstanceLock(tmp_path)
    assert old.acquire()
    old.record(8000)
    new = InstanceLock(tmp_path)
    started = time.monotonic()
    assert acquire_waiting_for_shutdown(new, timeout=10, poll=0.05, is_answering=lambda port: True) is False
    assert time.monotonic() - started < 1, "a normal second click brings the window forward immediately"
    old.release()


def test_a_holder_that_never_exits_gives_up_after_the_timeout(tmp_path):
    old = InstanceLock(tmp_path)
    assert old.acquire()
    old.record(8000)
    old.mark_stopping()
    new = InstanceLock(tmp_path)
    assert acquire_waiting_for_shutdown(new, timeout=0.3, poll=0.05, is_answering=lambda port: True) is False
    old.release()


def test_the_desktop_marks_itself_stopping_before_shutting_down():
    from app.desktop.launcher import DesktopApp

    calls = []
    shell = DesktopApp(app=object())
    shell.on_stopping = lambda: calls.append("stopping")
    shell.server.stop = lambda timeout=10.0: True
    shell.stop()
    shell.stop()
    assert calls == ["stopping"], "marked once, before anything stops"
