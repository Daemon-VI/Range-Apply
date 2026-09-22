"""A Windows desktop shortcut for the control center: ``python -m app.desktop --install-shortcut``.

The shortcut (``CareerOS.lnk`` on the person's Desktop) launches the same
supported entry point the documentation names, with the project's own
interpreter and the repository as working directory, so it works from any
directory and after the repository or the Desktop folder moves (re-run the
installer). It carries no credential: configuration keeps coming from
``.env`` and the environment, exactly as for a manual start.

The ``.lnk`` is written through the ``WScript.Shell`` COM object from
PowerShell — part of every Windows installation — so no extra package is
needed and the same file is overwritten on every run (no duplicates).
"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from app.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

SHORTCUT_NAME = "CareerOS"
DESCRIPTION = "CareerOS control center (local, dry run by default)"
#: The normal desktop mode: window, in-process worker in DRY RUN, visible browser,
#: and autopilot (discover -> match -> prepare on a timer; never submits).
DEFAULT_ARGUMENTS = ("--worker", "--headed", "--autopilot")
ICON_PATH = Path(__file__).resolve().parent / "static" / "careeros.ico"


class ShortcutError(RuntimeError):
    """The shortcut could not be created or read (Windows only, COM failure)."""


def interpreter(prefer_windowless: bool = True) -> Path:
    """The interpreter of the environment CareerOS runs in.

    ``pythonw.exe`` next to the running ``python.exe`` keeps a console window
    from opening beside the control center; the launcher logs to a file when
    it has no console (see ``app.desktop.__main__``).
    """
    current = Path(sys.executable).resolve()
    if prefer_windowless and current.name.lower() == "python.exe":
        windowless = current.with_name("pythonw.exe")
        if windowless.is_file():
            return windowless
    return current


def desktop_directory() -> Path:
    """The person's Desktop folder as Windows reports it (OneDrive-redirected desktops included)."""
    if sys.platform == "win32":
        try:
            out = _powershell("[Environment]::GetFolderPath('Desktop')")
            if out:
                return Path(out)
        except ShortcutError:
            pass
    return Path.home() / "Desktop"


def shortcut_path(directory: Optional[Path] = None) -> Path:
    return (directory or desktop_directory()) / f"{SHORTCUT_NAME}.lnk"


def _ps_quote(value: str) -> str:
    """A PowerShell single-quoted literal (the only escape is a doubled quote)."""
    return "'" + str(value).replace("'", "''") + "'"


def _powershell(script: str) -> str:
    if sys.platform != "win32":
        raise ShortcutError("desktop shortcuts are created on Windows only")
    try:
        completed = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ShortcutError(f"PowerShell could not be run ({type(exc).__name__})") from exc
    if completed.returncode != 0:
        raise ShortcutError((completed.stderr or completed.stdout or "PowerShell failed").strip()[:500])
    return (completed.stdout or "").strip()


def install_shortcut(directory: Optional[Path] = None, arguments: tuple[str, ...] = DEFAULT_ARGUMENTS, prefer_windowless: bool = True) -> Path:
    """Create or overwrite ``<Desktop>/CareerOS.lnk``; returns its path.

    Deterministic: the same target, arguments, working directory and icon
    every time, at the same path, so running it twice leaves one shortcut.
    """
    target = interpreter(prefer_windowless)
    path = shortcut_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    args = "-m app.desktop " + " ".join(arguments)
    for value in (*arguments, str(target), str(PROJECT_ROOT)):
        if "\n" in value or "\r" in value:
            raise ShortcutError("shortcut values must not contain line breaks")
    icon = f"{ICON_PATH},0" if ICON_PATH.is_file() else f"{target},0"
    script = (
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$s = $shell.CreateShortcut({_ps_quote(str(path))}); "
        f"$s.TargetPath = {_ps_quote(str(target))}; "
        f"$s.Arguments = {_ps_quote(args)}; "
        f"$s.WorkingDirectory = {_ps_quote(str(PROJECT_ROOT))}; "
        f"$s.IconLocation = {_ps_quote(icon)}; "
        f"$s.Description = {_ps_quote(DESCRIPTION)}; "
        "$s.WindowStyle = 1; "
        "$s.Save(); Write-Output 'ok'"
    )
    if _powershell(script) != "ok" or not path.is_file():
        raise ShortcutError(f"the shortcut was not written at {path}")
    logger.info("Desktop shortcut written: %s", path)
    return path


def read_shortcut(path: Path) -> dict[str, Any]:
    """What a ``.lnk`` points at (for the installer's report and the tests)."""
    script = (
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$s = $shell.CreateShortcut({_ps_quote(str(path))}); "
        "Write-Output ($s.TargetPath + [char]31 + $s.Arguments + [char]31 + $s.WorkingDirectory + [char]31 + $s.IconLocation + [char]31 + $s.Description)"
    )
    parts = _powershell(script).split("\x1f")
    keys = ("target", "arguments", "working_directory", "icon", "description")
    return dict(zip(keys, parts + [""] * (len(keys) - len(parts))))


def describe(path: Path, details: dict[str, Any]) -> str:
    """The lines the installer prints: where the shortcut is and what it runs."""
    return "\n".join(
        [
            f"Desktop shortcut: {path}",
            f"  runs:    {details.get('target', '')} {details.get('arguments', '')}",
            f"  in:      {details.get('working_directory', '')}",
            f"  icon:    {details.get('icon', '')}",
            "  mode:    control center window + local worker in DRY RUN + visible browser + autopilot (finds, matches, prepares; never submits; no credential is stored in the shortcut)",
            f"  log:     {os.path.join(str(PROJECT_ROOT), '.cache', 'desktop', 'desktop.log')} when started without a console",
        ]
    )
