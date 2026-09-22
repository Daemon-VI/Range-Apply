"""The Windows desktop shortcut: deterministic, credential-free, and never a launch by accident."""

import logging
import sys
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, settings
from app.desktop import __main__ as entry
from app.desktop import shortcut

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")


@windows_only
def test_install_writes_one_shortcut_that_runs_the_supported_entry_point(tmp_path):
    first = shortcut.install_shortcut(tmp_path)
    second = shortcut.install_shortcut(tmp_path)
    assert first == second == tmp_path / "CareerOS.lnk" and first.is_file()
    assert [p.name for p in tmp_path.iterdir()] == ["CareerOS.lnk"], "running twice leaves one shortcut"
    details = shortcut.read_shortcut(first)
    target = Path(details["target"])
    assert target.name.lower() in ("pythonw.exe", "python.exe") and target.parent == Path(sys.executable).resolve().parent, "the project's own interpreter"
    assert details["arguments"] == "-m app.desktop --worker --headed --autopilot", "the documented entry point in its normal desktop mode"
    assert Path(details["working_directory"]) == Path(PROJECT_ROOT), "works from any current directory"
    assert details["icon"].lower().startswith(str(shortcut.ICON_PATH).lower()), "the CareerOS icon"
    assert "dry run" in details["description"].lower()
    for secret in (settings.api_key, "careeros_key", "X-API-Key", "token="):
        assert not secret or secret not in details["arguments"] + details["target"] + details["working_directory"]


@windows_only
def test_paths_with_spaces_and_quotes_round_trip(tmp_path):
    folder = tmp_path / "My Desktop 'quoted'"
    path = shortcut.install_shortcut(folder)
    assert path.is_file() and path.parent == folder
    assert Path(shortcut.read_shortcut(path)["working_directory"]) == Path(PROJECT_ROOT)


@windows_only
def test_windowless_interpreter_is_preferred_but_never_required(monkeypatch, tmp_path):
    fake = tmp_path / "python.exe"
    fake.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(fake))
    assert shortcut.interpreter() == fake.resolve(), "no pythonw next to it: the console interpreter"
    (tmp_path / "pythonw.exe").write_bytes(b"")
    assert shortcut.interpreter().name == "pythonw.exe"
    assert shortcut.interpreter(prefer_windowless=False).name == "python.exe"


@windows_only
def test_install_shortcut_command_creates_it_reports_the_path_and_starts_nothing(tmp_path, monkeypatch, capsys):
    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("--install-shortcut must never start the desktop")

    monkeypatch.setattr("app.desktop.launcher.DesktopApp", Boom)
    code = entry.main(["--install-shortcut", "--shortcut-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0 and (tmp_path / "CareerOS.lnk").is_file()
    assert str(tmp_path / "CareerOS.lnk") in out and "-m app.desktop --worker --headed --autopilot" in out and "DRY RUN" in out


def test_install_shortcut_reports_a_failure_instead_of_raising(monkeypatch, capsys):
    def broken(*a, **k):
        raise shortcut.ShortcutError("no COM today")

    monkeypatch.setattr(shortcut, "install_shortcut", broken)
    assert entry.main(["--install-shortcut"]) == 2
    assert "no COM today" in capsys.readouterr().err


def test_console_less_start_logs_to_a_file_and_tells_the_person_with_a_message_box(monkeypatch, tmp_path):
    """Under pythonw (the shortcut) there is no stderr: the log goes to a file
    and a readiness failure is shown in a native box instead of a lost print."""
    monkeypatch.setattr(settings, "desktop_state_dir", str(tmp_path))
    monkeypatch.setattr(entry, "_has_console", lambda: False)
    root = logging.getLogger()
    saved = list(root.handlers)
    for handler in saved:
        root.removeHandler(handler)
    try:
        path = entry._setup_logging(None)
        assert path == str(tmp_path / "desktop.log")
        logging.getLogger("app.desktop.test").info("hello from a console-less start; X-API-Key: abc123")
        for handler in root.handlers:
            handler.flush()
        text = (tmp_path / "desktop.log").read_text(encoding="utf-8")
        assert "hello from a console-less start" in text and "abc123" not in text, "file logging is redacted like every handler"
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
        for handler in saved:
            root.addHandler(handler)
    boxes = []
    monkeypatch.setattr(sys, "platform", "win32")

    class FakeUser32:
        def MessageBoxW(self, hwnd, text, title, flags):  # noqa: N802 - Win32 name
            boxes.append((text, title, flags))
            return 1

    class FakeWinDLL:
        user32 = FakeUser32()

    import ctypes

    monkeypatch.setattr(ctypes, "windll", FakeWinDLL(), raising=False)
    entry._tell_person("CareerOS cannot start: the database is not migrated")
    assert boxes and "not migrated" in boxes[0][0] and boxes[0][2] == entry.ICON_ERROR
    # Pilot finding: the box shared the control center's window title, so a
    # later launch looking the running window up by title could find the box.
    from app.desktop.window import WINDOW_TITLE

    assert boxes[0][1] == entry.MESSAGE_TITLE and boxes[0][1] != WINDOW_TITLE
    entry._tell_person("CareerOS is already running", entry.ICON_INFORMATION)
    assert boxes[1][2] == entry.ICON_INFORMATION, "already running is information, not an error"
