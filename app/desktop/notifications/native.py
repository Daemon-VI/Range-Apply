"""Native OS toast adapters for :class:`~app.desktop.notifications.model.Notification`.

The in-app notification center is the source of truth and the fallback: a
native toast is a *convenience*. Every adapter here therefore fails soft —
:meth:`Notifier.send` returns ``False`` instead of raising, and the poller
carries on.

Privacy (notifications appear outside the window): this module never logs a
notification's title, body or launch URL. INFO carries the adapter choice
only; DEBUG carries the kind and the dedupe key, nothing else.

Windows uses ``winotify``, which builds a toast XML document and shows it via
a detached PowerShell process. Three consequences shape the code below:

* ``winotify`` interpolates ``launch`` straight into an XML attribute without
  escaping, so a launch URL carrying ``&`` (``/desktop/open?to=...&n=...``)
  produces malformed XML. PowerShell's ``LoadXml`` then throws inside a
  process whose stderr is ``DEVNULL`` and which nobody waits on, so the toast
  simply never appears and Python sees no error at all. :func:`escape_launch`
  pre-escapes the URL; the XML parser hands Windows the original string back.
* the toast XML is embedded in a PowerShell *expandable* here-string
  (``@"`` ... ``"@``), so ``$`` and a backtick inside the title or body are
  interpreted by PowerShell: a body containing ``$env:USERNAME`` renders as
  the real Windows account name. :func:`escape_text` turns those two
  characters into their here-string escapes, which render as the original
  characters — the toast shows the true text and nothing of the machine.
* ``winotify`` imports ``winreg`` at module import time (``winotify/_registry.py``),
  so the import is a hard failure off Windows. It is therefore always
  imported lazily, inside the adapter — never at module import.
"""

import logging
import sys
from typing import Optional, Protocol, runtime_checkable

from app.desktop.notifications.model import Notification

logger = logging.getLogger(__name__)

#: The toast's application name. Windows shows it above the title.
APP_ID = "CareerOS"

#: Every native title reads ``CareerOS — <title>`` so a toast is recognisable
#: at a glance in the Windows action center.
TITLE_PREFIX = "CareerOS — "

DURATION = "short"


def escape_launch(url: str) -> str:
    """XML-escape a launch URL for ``winotify``'s unescaped attribute interpolation.

    ``&`` (and, defensively, ``<``, ``>`` and the quote characters) would
    otherwise break the toast document. The escaping is undone by the XML
    parser before Windows sees the URL, so the browser receives the original.
    ``$`` and a backtick are escaped for the surrounding PowerShell
    here-string for the same reason as in :func:`escape_text`.
    """
    escaped = (
        url.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
    return escaped.replace("`", "``").replace("$", "`$")


def escape_text(text: str) -> str:
    """Neutralise PowerShell here-string expansion in toast text.

    ``winotify`` drops the title and body into a ``@"`` ... ``"@`` here-string,
    where ``$`` starts a variable reference and a backtick is the escape
    character. Both are escaped with a leading backtick, which PowerShell
    renders back as the plain character — so ordinary text is untouched and
    ``$env:USERNAME`` stays literal text instead of becoming the account
    name. ``]]>`` would also close the CDATA section, so it is broken up.
    """
    return text.replace("`", "``").replace("$", "`$").replace("]]>", "]]&gt;")


def native_title(notification: Notification) -> str:
    """The toast heading: the shared prefix plus the notification's own title."""
    return TITLE_PREFIX + notification.title


@runtime_checkable
class Notifier(Protocol):
    """Something that can (or politely cannot) show an OS-level toast."""

    #: Short adapter name for logs and the control center.
    name: str
    #: True when this adapter can actually show a toast on this machine.
    available: bool

    def send(self, notification: Notification, launch_url: Optional[str] = None) -> bool:
        """Show ``notification``; return True only when a native toast was shown.

        ``launch_url`` is the local ``http://127.0.0.1:<port>/...`` address to
        open when the person clicks the toast. Never raises.
        """
        ...


class NullNotifier:
    """The no-op adapter: the in-app notification center is the fallback."""

    name = "null"
    available = False

    def send(self, notification: Notification, launch_url: Optional[str] = None) -> bool:
        """Do nothing, successfully. The notification center already has it."""
        return False


class WinotifyNotifier:
    """Windows toasts through ``winotify``.

    ``winotify`` is imported lazily (it imports ``winreg``, so it cannot even
    be imported off Windows) and every failure is swallowed: a toast that did
    not appear must never take the desktop poller down with it.
    """

    name = "winotify"

    def __init__(self) -> None:
        self.available = False
        if sys.platform != "win32":
            return
        try:
            import winotify  # noqa: F401
        except Exception as exc:  # pragma: no cover - exercised via select_notifier
            logger.debug("winotify unavailable (%s)", type(exc).__name__)
            return
        self.available = True

    def send(self, notification: Notification, launch_url: Optional[str] = None) -> bool:
        try:
            import winotify

            toast = winotify.Notification(
                app_id=APP_ID,
                title=escape_text(native_title(notification)),
                msg=escape_text(notification.body),
                duration=DURATION,
            )
            if launch_url:
                toast.launch = escape_launch(launch_url)
            toast.show()
        except Exception as exc:
            # The exception TYPE only: a message could echo the body or the URL.
            logger.warning("native notification failed (%s)", type(exc).__name__)
            return False
        logger.debug("native notification shown kind=%s key=%s", notification.kind, notification.key)
        return True


def select_notifier(prefer_native: bool = True) -> Notifier:
    """Pick the best adapter for this machine. Never raises.

    Falls back to :class:`NullNotifier` whenever native toasts are not
    possible (not Windows, ``winotify`` not installed) or not wanted.
    """
    if not prefer_native:
        logger.info("desktop notifications: in-app only (native toasts not requested)")
        return NullNotifier()
    if sys.platform != "win32":
        logger.info("desktop notifications: in-app only (platform %s has no toast adapter)", sys.platform)
        return NullNotifier()
    try:
        import winotify  # noqa: F401
    except Exception as exc:
        logger.info("desktop notifications: in-app only (winotify unavailable: %s)", type(exc).__name__)
        return NullNotifier()
    notifier = WinotifyNotifier()
    if not notifier.available:  # pragma: no cover - defensive
        logger.info("desktop notifications: in-app only (winotify adapter unavailable)")
        return NullNotifier()
    logger.info("desktop notifications: native Windows toasts via winotify")
    return notifier
