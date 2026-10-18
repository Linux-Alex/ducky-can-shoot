"""Desktop notifications with an action button, without Qt.

Plugging a medium in has to be *visible*, and "visible" on a freedesktop desktop
means the notification server: KDE, GNOME and everything else render the actions
of a notification as buttons.  `notify-send --wait` waits for the user and prints
the name of the action that was picked to stdout, so no D-Bus client and no Qt
binding is needed -- only libnotify, which every desktop ships.

The class is intentionally forgiving: on a machine without a notification server
(SSH, a bare window manager, a container) :meth:`DesktopNotifier.notify` returns
an empty string instead of raising, and the caller falls back to the log or to
the interface itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from typing import Any

from dcs.core.log import get_logger
from dcs.version import APP_ID, APP_NAME

log = get_logger("core.notify.desktop")

#: the binary libnotify installs
NOTIFY_BINARY = "notify-send"

#: the action a caller can offer; the name is what comes back to the caller
ACTION_OPEN = "open"

#: how long the process waits for the user before giving up on the notification
EXTRA_WAIT_S = 15.0


@dataclass
class DesktopNotifier:
    """Show a notification with buttons (`notify-send` underneath)."""

    app_name: str = APP_NAME
    icon: str = APP_ID
    timeout_ms: int = 20000
    #: seconds to wait for an answer before closing the window on our side
    extra_wait_s: float = EXTRA_WAIT_S
    #: injectable for the tests
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    binary: str = NOTIFY_BINARY

    def available(self) -> bool:
        """True when a notification can be shown at all."""
        return bool(shutil.which(self.binary))

    def notify(
        self,
        title: str,
        body: str = "",
        *,
        actions: Sequence[tuple[str, str]] = ((ACTION_OPEN, "Open Ducky Can Shoot"),),
        urgency: str = "normal",
    ) -> str:
        """Show the notification and return the action name the user picked.

        An empty string means the notification was dismissed, timed out or could
        not be shown at all.
        """
        if not self.available() and os.path.basename(self.binary) == NOTIFY_BINARY:
            log.info("notify-send is not installed; no desktop notification was shown")
            return ""

        argv = [
            self.binary,
            "--app-name", self.app_name,
            "--icon", self.icon,
            "--urgency", urgency,
            "--expire-time", str(int(self.timeout_ms)),
        ]
        for name, label in actions:
            argv += ["--action", f"{name}={label}"]
        argv += [title, body or title]

        timeout = max(1.0, self.timeout_ms / 1000.0) + self.extra_wait_s
        try:
            proc = self.runner(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            log.debug("the notification was not answered within %.0fs", timeout)
            return ""
        except OSError as exc:
            log.warning("cannot show a desktop notification: %s", exc)
            return ""
        if proc.returncode != 0:
            log.info(
                "the notification server refused the notification (exit %s): %s",
                proc.returncode,
                (proc.stderr or "").strip()[:200],
            )
            return ""
        choice = (proc.stdout or "").strip()
        log.debug("notification answered with %r", choice)
        return choice


def notify(
    title: str,
    body: str = "",
    *,
    actions: Sequence[tuple[str, str]] = ((ACTION_OPEN, "Open Ducky Can Shoot"),),
    icon: str = APP_ID,
    timeout_ms: int = 20000,
    urgency: str = "normal",
) -> str:
    """Show one notification with the best notifier this machine has."""
    return build_notifier(icon=icon, timeout_ms=timeout_ms, urgency=urgency).notify(
        title, body, actions=actions
    )


def build_notifier(**kwargs: Any) -> Any:
    """The notifier to use, best first.

    `notify-send` sends a notification without its buttons on systems where it
    believes the server cannot show them - Plasma 6.6 answers exactly that, in
    spite of advertising the capability - and a notification whose button is
    missing is useless for "plug a medium in and click Open".  Talking to the
    notification service directly keeps the button.

    The order matters beyond the buttons: Plasma withdraws a notification that
    carries actions as soon as the client connection that sent it closes, so the
    one-shot `gdbus call` produced a toast that flashed for about a second.
    GioNotifier holds the connection open for as long as the notification is on
    screen and is therefore preferred; gdbus stays as the fallback for a machine
    without python3-gi, and notify-send for a machine without gdbus.
    """
    from dcs.core.notify.dbus import DbusNotifier, GioNotifier

    for candidate in (GioNotifier, DbusNotifier, DesktopNotifier):
        known = {field_info.name for field_info in dataclass_fields(candidate)}
        instance = candidate(**{key: value for key, value in kwargs.items() if key in known})
        if instance.available():
            log.debug("notifying through %s", candidate.__name__)
            return instance
    return DesktopNotifier()  # nothing is available; notify() reports that


__all__ = [
    "ACTION_OPEN",
    "NOTIFY_BINARY",
    "DesktopNotifier",
    "build_notifier",
    "notify",
]
