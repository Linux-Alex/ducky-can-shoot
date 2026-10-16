"""Application bootstrap: Qt setup, QML engine and the QML import paths."""

from __future__ import annotations

import os
import signal
import sys
from typing import Any

from PyQt6.QtCore import QCoreApplication, QEvent, Qt, QTimer, QUrl
from PyQt6.QtGui import QGuiApplication, QIcon
from PyQt6.QtQml import QQmlApplicationEngine, QQmlContext

from dcs.core import autostart, service, session
from dcs.core.config import AppConfig, load_config
from dcs.core.log import configure_logging, get_logger
from dcs.gui.backend import Backend
from dcs.version import APP_ID, APP_NAME, __version__

log = get_logger("gui.app")

#: holds the engine and the backend for the lifetime of the process
_SESSION: dict[str, Any] = {}


def qml_directory() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "qml")


def find_qml_imports() -> list[str]:
    """QML import paths for a source checkout and for an installed package."""
    paths = [qml_directory()]
    for candidate in (
        os.environ.get("QML2_IMPORT_PATH"),
        os.environ.get("QML_IMPORT_PATH"),
        "/usr/lib/x86_64-linux-gnu/qt6/qml",
        "/usr/lib/qt6/qml",
    ):
        if candidate:
            paths.extend(part for part in candidate.split(os.pathsep) if part)
    return list(dict.fromkeys(paths))


def build_engine(config: AppConfig | None = None, backend: Backend | None = None) -> tuple[QQmlApplicationEngine, Backend]:
    """Create the QML engine with the backend attached as a context object."""
    config = config or load_config()
    backend = backend or Backend(config)

    engine = QQmlApplicationEngine()
    for path in find_qml_imports():
        engine.addImportPath(path)
    context: QQmlContext = engine.rootContext()
    context.setContextProperty("backend", backend)
    context.setContextProperty("appVersion", __version__)
    context.setContextProperty("appName", APP_NAME)

    def report_warnings(warnings: list[Any]) -> None:
        for warning in warnings:
            log.warning("QML: %s", warning.toString())

    engine.warnings.connect(report_warnings)
    engine.load(QUrl.fromLocalFile(os.path.join(qml_directory(), "Main.qml")))
    if not engine.rootObjects():
        raise RuntimeError("the QML interface failed to load; see the warnings above")

    # QGuiApplication.instance() is None only when the caller has no application
    # object yet (headless QML tests); remembering the size is then meaningless.
    application = QGuiApplication.instance()
    if application is not None:
        remember_window_geometry(application, engine.rootObjects()[0], backend)

    # Keep both alive for the lifetime of the process.  QML holds the backend
    # only as a context property, so without this reference Python could collect
    # it while the engine is still tearing down and every binding would then log
    # "cannot read property of null" on exit.  The engine is stored first so
    # that it is destroyed before the backend at interpreter shutdown.
    _SESSION.update({"engine": engine, "backend": backend})
    return engine, backend


def remember_window_geometry(app: QGuiApplication, window: Any, backend: Backend) -> None:
    """Store the window size on exit when gui.remember_window_size is set."""
    if not backend.config.gui.remember_window_size:
        return

    def store() -> None:
        try:
            width = int(window.property("width") or 0)
            height = int(window.property("height") or 0)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return
        if width < 400 or height < 300:
            return
        backend.config.gui.window_width = width
        backend.config.gui.window_height = height
        try:
            backend.config.save()
        except OSError as exc:  # pragma: no cover - read only home
            log.warning("could not store the window size: %s", exc)

    app.aboutToQuit.connect(store)


def installed_desktop_file(app_id: str) -> str:
    """Path of the installed .desktop file for *app_id*, or an empty string.

    Qt asks the desktop portal to register the application id, and the portal
    answers "App info not found" when no .desktop file matches.  Running from a
    source checkout that is always the case, so the id is only set when the file
    is really installed (scripts/install.sh puts it in place).
    """
    file_name = f"{app_id}.desktop"
    locations = [os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")]
    locations.extend(
        part
        for part in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(os.pathsep)
        if part
    )
    for base in locations:
        candidate = os.path.join(base, "applications", file_name)
        if os.path.exists(candidate):
            return candidate
    return ""


def run_gui(
    argv: list[str] | None = None,
    *,
    config: AppConfig | None = None,
    device: str = "",
    autorun: bool = False,
) -> int:
    """Entry point used by the dcs-gui script and by 'dcs' without arguments.

    *device* is the medium to open on (a node such as /dev/sdb1, mounted when
    needed) and *autorun* starts the job on it straight away; both are how the
    USB watcher hands a freshly plugged medium to a new window.
    """
    argv = list(argv or sys.argv)
    configure_logging(level=os.environ.get("DCS_LOG_LEVEL", "INFO"))

    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setOrganizationDomain("kde.org")
    QCoreApplication.setApplicationVersion(__version__)
    QGuiApplication.setApplicationDisplayName(APP_NAME)
    if hasattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"):
        QGuiApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

    app = QGuiApplication(argv)

    # Only claim a desktop file when one is installed: otherwise the desktop
    # portal answers "Could not register app ID: App info not found", which is
    # what happens when the interface is started from a source checkout.
    desktop_file = installed_desktop_file(APP_ID)
    if desktop_file:
        app.setDesktopFileName(APP_ID)
        log.debug("registered as %s (%s)", APP_ID, desktop_file)
    else:
        log.debug(
            "no %s.desktop found; skipping desktop portal registration "
            "(scripts/install.sh installs one)",
            APP_ID,
        )

    # the artwork generated by scripts/make-icons.py, shipped inside the package
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qml", "icons", "dcs.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # Ctrl+C in a terminal should close the window instead of killing it rudely
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    try:
        engine, backend = build_engine(config)
    except Exception as exc:  # pragma: no cover - only on a broken installation
        log.error("cannot start the interface: %s", exc)
        return 2

    # Keep the objects alive for the whole session.  QML holds the backend only
    # as a context property, so without this reference Python would collect it
    # while the engine is still tearing down, and every binding would then
    # report "cannot read property of null" on exit.
    _SESSION.update({"engine": engine, "backend": backend})

    # Tell the watcher (the autostart process) that the window is here: while
    # this marker is alive 'dcs watch' stays quiet instead of reacting twice.
    session.claim()

    def leave_session() -> None:
        """Hand the watching over to the background process, then let go.

        Closing the window must not stop the listener when a mode is enabled:
        the marker is dropped first, so the watcher that is already running (or
        the one started here) reacts from that moment on.
        """
        session.release()
        if backend.config.watch.mode != "off":
            started, message = service.start()
            log.info("background watcher: %s", message if started else f"already there ({message})")

    app.aboutToQuit.connect(leave_session)

    # The autostart entry is a setting like any other: put it back when the
    # configuration asks for it but the file went missing.
    if backend.config.watch.autostart and backend.config.watch.mode != "off":
        autostart.sync(True)

    if device:
        QTimer.singleShot(0, lambda: backend.openDevice(device, autorun=autorun))

    # push log records that happened before the UI existed
    ring = configure_logging(level=os.environ.get("DCS_LOG_LEVEL", "INFO"))
    for record in ring.snapshot():
        backend.log_model.append(record)
    ring.add_listener(lambda record: QTimer.singleShot(0, lambda r=record: backend.log_model.append(r)))

    code = app.exec()
    teardown_engine(engine)
    return code


def teardown_engine(engine: QQmlApplicationEngine) -> None:
    """Destroy the QML tree while the Python backend is still alive.

    Without this, the interpreter shuts down in an order where the backend is
    collected first; every QML binding then re-evaluates against a null context
    property and floods the log with "cannot read property of null" on exit.
    """
    try:
        for root_object in engine.rootObjects():
            root_object.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    except RuntimeError:  # pragma: no cover - the C++ object is already gone
        pass
    _SESSION.clear()
