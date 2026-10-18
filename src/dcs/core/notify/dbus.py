"""Notifications with real buttons, straight over D-Bus.

libnotify's `notify-send` refuses to send actions on some systems - Plasma 6.6
answers "Actions are not supported by this notifications server" and shows the
notification without its buttons, even though the server does advertise the
`actions` capability.  Going to `org.freedesktop.Notifications` ourselves
avoids that code path completely: one `Notify` call with the buttons, then a
signal monitor that waits for `ActionInvoked`.

The transport is the `gdbus` command line tool, which ships with GLib and is
therefore present on every desktop, so nothing here needs a Python D-Bus
binding or Qt.
"""

from __future__ import annotations

import contextlib
import re
import select
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from dcs.core.log import get_logger
from dcs.version import APP_ID, APP_NAME

log = get_logger("core.notify.dbus")

#: the freedesktop notification service
SERVICE = "org.freedesktop.Notifications"
OBJECT_PATH = "/org/freedesktop/Notifications"
INTERFACE = "org.freedesktop.Notifications"

#: the action a caller offers; the name is what comes back
ACTION_OPEN = "open"

#: urgency names the D-Bus API uses as bytes
URGENCY = {"low": 0, "normal": 1, "critical": 2}

#: how much longer than the notification itself the monitor waits
GRACE_S = 5.0

#: parsed signals from the monitor
_ACTION_RE = re.compile(r"ActionInvoked \(uint32 (\d+), '([^']*)'\)")
_CLOSED_RE = re.compile(r"NotificationClosed \(uint32 (\d+), (\d+)\)")
_ID_RE = re.compile(r"uint32 (\d+)")


def _quote(text: str) -> str:
    """A single quoted GVariant string."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"



def _load_gi() -> tuple[Any, Any] | None:
    """Import Gio and GLib, or None when PyGObject is not installed."""
    try:
        import importlib

        gio = importlib.import_module("gi.repository.Gio")
        glib = importlib.import_module("gi.repository.GLib")
    except Exception:
        return None
    return gio, glib


@dataclass
class GioNotifier:
    """Notifications over one long-lived Gio connection.

    The connection stays open while the notification is on screen, which is what
    keeps it alive: the notification service withdraws action notifications whose
    sending client has gone away, and a one-shot tool like "gdbus call" is
    exactly such a client.
    """

    app_name: str = APP_NAME
    icon: str = APP_ID
    timeout_ms: int = 20000
    urgency: str = "normal"
    resident: bool = True
    #: injectable for the tests: returns (Gio, GLib) or None
    loader: Callable[[], tuple[Any, Any] | None] = field(default=_load_gi)

    last_id: int = field(default=0, init=False)

    def _libs(self) -> tuple[Any, Any] | None:
        try:
            return self.loader()
        except Exception:
            return None

    def available(self) -> bool:
        libs = self._libs()
        if libs is None:
            return False
        gio, _glib = libs
        try:
            gio.bus_get_sync(gio.BusType.SESSION, None)
        except Exception:
            return False
        return True

    def gio_args(
        self, title: str, body: str, actions: Sequence[tuple[str, str]]
    ) -> tuple[Any, ...]:
        """The Notify argument tuple, in the order the interface expects."""
        flat: list[str] = []
        for name, label in actions:
            flat += [name, label]
        return (
            self.app_name,
            0,  # no notification to replace
            self.icon,
            title,
            body or title,
            flat,
            self._hints(),
            int(self.timeout_ms),
        )

    def _hints(self) -> dict[str, Any]:
        libs = self._libs() or (None, None)
        _gio, glib = libs
        hints: dict[str, Any] = {}
        if glib is None:
            return hints
        hints["urgency"] = glib.Variant("y", URGENCY.get(self.urgency, 1))
        if self.resident:
            hints["resident"] = glib.Variant("b", True)
        return hints

    def notify(
        self,
        title: str,
        body: str = "",
        *,
        actions: Sequence[tuple[str, str]] = ((ACTION_OPEN, "Open Ducky Can Shoot"),),
    ) -> str:
        """Show the notification and return the action the user picked."""
        libs = self._libs()
        if libs is None:
            return ""
        gio, glib = libs
        # A private main context, so pumping it here cannot disturb a Qt or
        # GLib loop that is running in another thread of the same process.
        context = glib.MainContext.new()
        context.push_thread_default()
        events: list[tuple[str, tuple[Any, ...]]] = []
        connection = None
        subscription = 0
        try:
            connection = gio.bus_get_sync(gio.BusType.SESSION, None)
            subscription = connection.signal_subscribe(
                None,
                INTERFACE,
                None,
                OBJECT_PATH,
                None,
                gio.DBusSignalFlags.NONE,
                lambda *args: events.append((args[4], tuple(args[5].unpack()))),
            )
            reply = connection.call_sync(
                INTERFACE,
                OBJECT_PATH,
                INTERFACE,
                "Notify",
                glib.Variant("(susssasa{sv}i)", self.gio_args(title, body, actions)),
                None,
                gio.DBusCallFlags.NONE,
                10000,
                None,
            )
            self.last_id = int(reply.unpack()[0])
            answer = self._wait(context, events, actions)
        except Exception as exc:
            log.warning("cannot show a notification: %s", exc)
            return ""
        finally:
            if connection is not None and subscription:
                with contextlib.suppress(Exception):
                    connection.signal_unsubscribe(subscription)
            context.pop_thread_default()
        if not answer and self.last_id:
            self.close(self.last_id)
        return answer

    def _wait(
        self,
        context: Any,
        events: list[tuple[str, tuple[Any, ...]]],
        actions: Sequence[tuple[str, str]],
    ) -> str:
        names = {name for name, _ in actions}
        deadline = time.monotonic() + max(1.0, self.timeout_ms / 1000.0) + GRACE_S
        while time.monotonic() < deadline:
            context.iteration(False)
            while events:
                signal, payload = events.pop(0)
                if signal == "ActionInvoked":
                    notification, name = int(payload[0]), str(payload[1])
                    if self.last_id and notification != self.last_id:
                        continue
                    return name if name in names else ""
                if signal == "NotificationClosed":
                    notification = int(payload[0])
                    if not self.last_id or notification == self.last_id:
                        return ""
            time.sleep(0.02)
        return ""

    def close(self, notification_id: int) -> None:
        """Ask the service to withdraw a notification."""
        libs = self._libs()
        if libs is None:
            return
        gio, glib = libs
        try:
            connection = gio.bus_get_sync(gio.BusType.SESSION, None)
            connection.call_sync(
                INTERFACE,
                OBJECT_PATH,
                INTERFACE,
                "CloseNotification",
                glib.Variant("(u)", (int(notification_id),)),
                None,
                gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except Exception as exc:
            log.debug("cannot close notification %s: %s", notification_id, exc)

@dataclass
class DbusNotifier:
    """Show a notification with buttons by talking to the service directly."""

    app_name: str = APP_NAME
    icon: str = APP_ID
    timeout_ms: int = 20000
    #: normal, low or critical
    urgency: str = "normal"
    #: keep the notification on screen until it is dismissed
    resident: bool = True
    binary: str = "gdbus"
    #: injectable for the tests
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    opener: Callable[..., subprocess.Popen] = subprocess.Popen

    #: set by notify() so a caller can log what happened
    last_id: int = field(default=0, init=False)

    def available(self) -> bool:
        """True when the notification can be sent at all."""
        return bool(shutil.which(self.binary))

    def actions_literal(self, actions: Sequence[tuple[str, str]]) -> str:
        """The GVariant array of the freedesktop action pairs."""
        flat: list[str] = []
        for name, label in actions:
            flat += [name, label]
        return "[" + ", ".join(_quote(part) for part in flat) + "]"

    def hints_literal(self) -> str:
        hints = [f"'urgency': <byte {URGENCY.get(self.urgency, 1)}>"]
        if self.resident:
            hints.append("'resident': <true>")
        return "{" + ", ".join(hints) + "}"

    def call_argv(
        self,
        title: str,
        body: str,
        actions: Sequence[tuple[str, str]],
    ) -> list[str]:
        """The gdbus invocation that shows the notification."""
        return [
            self.binary,
            "call",
            "--session",
            "--dest", SERVICE,
            "--object-path", OBJECT_PATH,
            "--method", f"{INTERFACE}.Notify",
            self.app_name,
            "0",  # no notification to replace
            self.icon,
            title,
            body or title,
            self.actions_literal(actions),
            self.hints_literal(),
            str(int(self.timeout_ms)),
        ]

    def notify(
        self,
        title: str,
        body: str = "",
        *,
        actions: Sequence[tuple[str, str]] = ((ACTION_OPEN, "Open Ducky Can Shoot"),),
    ) -> str:
        """Show the notification and return the action the user picked.

        An empty string means it was dismissed, timed out or could not be shown.
        """
        try:
            result = self.runner(self.call_argv(title, body, actions), capture_output=True, text=True,
                                 timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("cannot show a notification: %s", exc)
            return ""
        if result.returncode != 0:
            log.warning(
                "the notification service refused the call (exit %s): %s",
                result.returncode,
                (result.stderr or "").strip()[:200],
            )
            return ""
        match = _ID_RE.search(result.stdout or "")
        self.last_id = int(match.group(1)) if match else 0
        return self.wait_for_action(actions)

    def wait_for_action(self, actions: Sequence[tuple[str, str]]) -> str:
        """Wait for the button press of the notification that was just sent."""
        names = {name for name, _ in actions}
        try:
            monitor = self.opener(
                [self.binary, "monitor", "--session", "--dest", SERVICE, "--object-path", OBJECT_PATH],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("cannot watch for the answer: %s", exc)
            return ""
        deadline = time.monotonic() + max(1.0, self.timeout_ms / 1000.0) + GRACE_S
        try:
            answer = self._read_until(monitor, names, deadline)
        finally:
            self._close(monitor)
        if not answer and self.last_id:
            # nothing was pressed in time: take the notification down again
            # instead of leaving a button that nobody listens to any more
            self.close(self.last_id)
        return answer

    def close(self, notification_id: int) -> None:
        """Ask the service to withdraw a notification."""
        try:
            self.runner(
                [
                    self.binary,
                    "call",
                    "--session",
                    "--dest", SERVICE,
                    "--object-path", OBJECT_PATH,
                    "--method", f"{INTERFACE}.CloseNotification",
                    str(int(notification_id)),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - defensive
            log.debug("cannot close notification %s: %s", notification_id, exc)

    def _read_until(self, monitor: subprocess.Popen[str], names: set[str], deadline: float) -> str:
        stream = monitor.stdout
        if stream is None:  # pragma: no cover - defensive
            return ""
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select([stream], [], [], min(0.5, remaining))
            if not ready:
                continue
            line = stream.readline()
            if not line:
                if monitor.poll() is not None:
                    break
                continue
            action = _ACTION_RE.search(line)
            if action:
                notification, name = int(action.group(1)), action.group(2)
                if self.last_id and notification != self.last_id:
                    continue
                if name in names:
                    return name
                continue
            closed = _CLOSED_RE.search(line)
            if closed:
                notification = int(closed.group(1))
                if not self.last_id or notification == self.last_id:
                    return ""
        return ""

    @staticmethod
    def _close(monitor: subprocess.Popen[str]) -> None:
        with contextlib.suppress(Exception):
            monitor.terminate()
            monitor.wait(timeout=2)


__all__ = ["ACTION_OPEN", "INTERFACE", "OBJECT_PATH", "SERVICE", "DbusNotifier"]
