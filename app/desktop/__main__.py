"""``python -m app.desktop`` — start CareerOS as one local desktop application."""

import argparse
import logging
import os
import sys
from pathlib import Path

from app.config import settings
from app.core.logging import configure_logging

LOG_FILE_NAME = "desktop.log"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.desktop", description="CareerOS desktop control center (local, loopback only)")
    parser.add_argument("--port", type=int, default=None, help=f"preferred port on 127.0.0.1 (default API_PORT={settings.api_port}; a free port is used when busy)")
    parser.add_argument("--worker", action="store_true", help="also run the local Playwright worker in DRY RUN inside this process (needs the browser extra)")
    parser.add_argument("--autopilot", action="store_true", help="run discovery -> matching -> preparation on a timer inside this process (never submits)")
    parser.add_argument("--autopilot-interval", type=float, default=30.0, help="minutes between autopilot cycles (default 30, minimum 1)")
    parser.add_argument("--headed", action="store_true", help="show the worker's browser window (dry run only)")
    parser.add_argument("--no-window", action="store_true", help="start the server (and worker) without a window; stop with Ctrl-C")
    parser.add_argument("--ready-timeout", type=float, default=30.0, help="seconds to wait for /health")
    parser.add_argument("--no-notifications", action="store_true", help="do not poll for desktop notifications (native toasts and the in-app list)")
    parser.add_argument("--no-native-notifications", action="store_true", help="keep the in-app notification list but never show a Windows toast")
    parser.add_argument("--notification-interval", type=float, default=15.0, help="seconds between notification polls (default 15)")
    parser.add_argument("--log-file", default=None, help=f"also write the log to this file (used automatically, as DESKTOP_STATE_DIR/{LOG_FILE_NAME}, when there is no console)")
    parser.add_argument("--install-shortcut", action="store_true", help="create or refresh the 'CareerOS' shortcut on the Windows desktop and exit; starts nothing")
    parser.add_argument("--shortcut-dir", default=None, help="write the shortcut into this folder instead of the desktop (with --install-shortcut)")
    return parser


def _has_console() -> bool:
    """False under ``pythonw.exe`` (the desktop shortcut): stdout/stderr are None."""
    return sys.stderr is not None and sys.stdout is not None


def _setup_logging(log_file: str | None) -> str | None:
    """Console logging as always; a file as well when asked for or when there is no console."""
    handlers: list[logging.Handler] = []
    path = log_file
    if path is None and not _has_console():
        path = os.path.join(settings.desktop_state_dir, LOG_FILE_NAME)
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        handlers.append(file_handler)
    root = logging.getLogger()
    if not _has_console() and not root.handlers:
        # No stderr to write to: the file is the only handler (basicConfig
        # would otherwise install a stream handler on None).
        root.setLevel(settings.log_level)
        for handler in handlers:
            root.addHandler(handler)
        handlers = []
    configure_logging(settings.log_level, extra_handlers=handlers)
    return path


#: Title of the launcher's own message boxes. Deliberately not the control
#: center's window title: a later launch looks the running window up by that
#: title and must never find (and "focus") a leftover dialog instead.
MESSAGE_TITLE = "CareerOS launcher"
ICON_ERROR = 0x10
ICON_INFORMATION = 0x40


def _tell_person(message: str, icon: int = ICON_ERROR) -> None:
    """Print, or — with no console to print to — show a native message box."""
    if _has_console():
        print(message, file=sys.stderr)
        return
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, MESSAGE_TITLE, icon)
        except Exception:  # noqa: BLE001 - a failed box must not mask the exit code
            pass


def _install_shortcut(directory: str | None) -> int:
    from app.desktop.shortcut import ShortcutError, describe, install_shortcut, read_shortcut

    try:
        path = install_shortcut(Path(directory) if directory else None)
        details = read_shortcut(path)
    except ShortcutError as exc:
        print(f"CareerOS shortcut was not created: {exc}", file=sys.stderr)
        return 2
    print(describe(path, details))
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.install_shortcut:
        # Creates the shortcut and exits: no server, no worker, no window.
        return _install_shortcut(args.shortcut_dir)
    log_path = _setup_logging(args.log_file)
    logger = logging.getLogger("app.desktop")
    if log_path:
        logger.info("Desktop log file: %s", log_path)

    from app.desktop.instance import (
        ALREADY_RUNNING,
        InstanceLock,
        acquire_waiting_for_shutdown,
        already_running_message,
        bring_window_forward,
    )

    lock = InstanceLock(settings.desktop_state_dir)
    # A relaunch right after closing the window waits for the old instance to finish stopping.
    if not acquire_waiting_for_shutdown(lock):
        # A second launch (the shortcut clicked twice): start nothing.
        message, answering = already_running_message(lock)
        logger.warning(message)
        if not (answering and bring_window_forward()):
            _tell_person(message, ICON_INFORMATION if answering else ICON_ERROR)
        return ALREADY_RUNNING
    try:
        return _run(args, logger, log_path, lock)
    finally:
        lock.release()


def _run(args, logger, log_path, lock) -> int:
    from app.desktop.launcher import DesktopApp
    from app.desktop.services import (
        WorkerSupervisor,
        default_poller_factory,
        default_worker_factory,
    )
    from app.desktop.window import is_available, open_window
    from app.main import app

    open_fn = None
    if not args.no_window:
        if not is_available():
            logger.error("pywebview is not installed. Install it with: pip install -e \".[desktop]\"  (or run with --no-window)")
            _tell_person("pywebview is not installed. Install it with: pip install -e \".[desktop]\"")
            return 2
        open_fn = open_window
    supervisor = WorkerSupervisor(default_worker_factory(headless=not args.headed)) if args.worker else None
    shell = DesktopApp(app, port=args.port, worker_supervisor=supervisor, open_window=open_fn, ready_timeout=args.ready_timeout)
    if args.autopilot:
        from app.config import settings
        from app.desktop.autopilot import default_autopilot_factory

        shell.autopilot = WorkerSupervisor(default_autopilot_factory(settings.default_tenant_id, args.autopilot_interval), name="careeros-autopilot")
    if not args.no_notifications:
        shell.notifications = WorkerSupervisor(default_poller_factory(shell, interval=args.notification_interval, prefer_native=not args.no_native_notifications), name="careeros-notifier")
    lock.record(shell.server.port)
    shell.on_stopping = lock.mark_stopping
    code = shell.run()
    if code != 0 and shell.readiness is not None and not shell.readiness.ready:
        problems = "\n".join(f"CareerOS cannot start: {problem}" for problem in shell.readiness.problems)
        _tell_person(problems + (f"\n\nLog: {log_path}" if log_path else ""))
    elif code != 0:
        _tell_person(f"CareerOS did not start (exit code {code})." + (f" See the log: {log_path}" if log_path else ""))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
