"""Offscreen GUI tests: Qt models, the Backend facade and the QML interface.

The whole module is skipped when PyQt6 or the offscreen platform plugin is
missing, so the headless parts of the suite keep working on a machine without
Qt.  No window is ever shown: QT_QPA_PLATFORM is forced to offscreen before the
QApplication exists.
"""

from __future__ import annotations

import itertools
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")
pytest.importorskip("PyQt6.QtQml", reason="PyQt6 QtQml is not installed")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Q_ARG, QLibraryInfo, QModelIndex

_PLATFORM_PLUGINS = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)) / "platforms"
if not list(_PLATFORM_PLUGINS.glob("*offscreen*")):
    pytest.skip("the offscreen Qt platform plugin is not installed", allow_module_level=True)

from dcs.core.config import AppConfig, UpdateConfig  # noqa: E402
from dcs.core.device import BlockDevice  # noqa: E402
from dcs.core.events import (  # noqa: E402
    STAGE_ORDER,
    STAGE_TITLES,
    Finding,
    Severity,
    Stage,
    StageProgress,
    StageState,
)
from dcs.core.log import LogRecordView, log_ring  # noqa: E402
from dcs.core.upload import provider_catalog  # noqa: E402
from dcs.gui import app as gui_app  # noqa: E402
from dcs.gui.backend import IDLE, Backend  # noqa: E402
from dcs.gui.models import (  # noqa: E402
    DevicesModel,
    FindingsModel,
    LogModel,
    StagesModel,
    ThroughputModel,
)
from dcs.version import APP_NAME, __version__  # noqa: E402

#: the QML of the interface, for the tests that read it instead of running it
ROOT_QML = Path(__file__).resolve().parent.parent / "src" / "dcs" / "gui" / "qml"

#: Kirigami's global drawer triggers a harmless binding loop inside Kirigami
#: itself; it is not a problem with the DCS QML and is ignored here.
IGNORED_QML_WARNINGS = (
    'Binding loop detected for property "bottomMargin"',
    "DrawerHandle.qml",
)


@pytest.fixture
def backend(qapp: Any, tmp_config: AppConfig) -> Iterator[Backend]:
    """A Backend that never polls lsblk, cleaned up after the test."""
    instance = Backend(tmp_config, poll_devices=False)
    yield instance
    log_ring().remove_listener(instance._on_log_record)


@pytest.fixture
def qt_messages() -> Iterator[list[str]]:
    """Collect every message Qt prints while the test runs."""
    from PyQt6.QtCore import qInstallMessageHandler

    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    try:
        yield messages
    finally:
        qInstallMessageHandler(previous)


def walk(model: Any, role: int, row: int) -> Any:
    return model.data(model.index(row, 0), role)


# ---------------------------------------------------------------------------
# StagesModel
# ---------------------------------------------------------------------------


def test_stages_model_starts_with_every_stage_pending(qapp: Any) -> None:
    model = StagesModel()
    assert model.rowCount() == len(STAGE_ORDER) == 7
    assert [walk(model, model.IdRole, row) for row in range(7)] == [str(s) for s in STAGE_ORDER]
    assert [walk(model, model.TitleRole, row) for row in range(7)] == [
        STAGE_TITLES[stage] for stage in STAGE_ORDER
    ]
    assert all(walk(model, model.StateRole, row) == StageState.PENDING.value for row in range(7))
    assert all(walk(model, model.PercentRole, row) == 0.0 for row in range(7))
    assert all(walk(model, model.DescriptionRole, row) for row in range(7))
    assert model.roleNames()[model.IdRole] == b"stageId"
    assert model.data(QModelIndex(), model.StateRole) is None


def test_stages_model_set_state_marks_running_and_done(qapp: Any) -> None:
    model = StagesModel()
    model.set_state("scan", StageState.RUNNING.value, "engines: heuristic")
    assert walk(model, model.StateRole, 1) == "running"
    assert walk(model, model.PercentRole, 1) == pytest.approx(0.01)
    assert walk(model, model.MessageRole, 1) == "engines: heuristic"

    model.set_state("scan", StageState.DONE.value, "no threats found")
    assert walk(model, model.StateRole, 1) == "done"
    assert walk(model, model.PercentRole, 1) == 1.0
    assert walk(model, model.MessageRole, 1) == "no threats found"

    model.set_state("upload", StageState.SKIPPED.value, "the medium is infected")
    assert walk(model, model.StateRole, 4) == "skipped"
    assert walk(model, model.MessageRole, 4) == "the medium is infected"
    # an unknown stage id and an empty message change nothing
    model.set_state("nope", StageState.DONE.value, "")
    assert walk(model, model.StateRole, 0) == "pending"
    assert walk(model, model.MessageRole, 1) == "no threats found"


def test_stages_model_set_state_emits_data_changed(qapp: Any) -> None:
    model = StagesModel()
    seen: list[tuple[int, int]] = []
    model.dataChanged.connect(lambda first, last, roles=None: seen.append((first.row(), last.row())))
    model.set_state("archive", StageState.RUNNING.value)
    assert seen == [(3, 3)]


def test_stages_model_apply_progress(qapp: Any) -> None:
    model = StagesModel()
    progress = StageProgress(
        stage=Stage.SCAN,
        state=StageState.RUNNING,
        processed=12,
        total=48,
        bytes_processed=2048,
        bytes_total=8192,
        current="/media/usb/invoice.pdf.exe",
        message="12 / 48 files",
        rate=1024.0,
        elapsed=3.0,
        eta=7.0,
        percent=0.25,
    )
    model.apply_progress(progress.to_dict())

    assert walk(model, model.StateRole, 1) == "running"
    assert walk(model, model.PercentRole, 1) == pytest.approx(0.25)
    assert walk(model, model.MessageRole, 1) == "12 / 48 files"
    assert walk(model, model.CurrentRole, 1) == "/media/usb/invoice.pdf.exe"
    assert walk(model, model.ElapsedRole, 1) == pytest.approx(3.0)
    assert walk(model, model.EtaRole, 1) == pytest.approx(7.0)
    assert walk(model, model.RateRole, 1) == pytest.approx(1024.0)
    assert walk(model, model.ProcessedRole, 1) == pytest.approx(2048)

    # a terminal state from the pipeline is applied verbatim
    model.apply_progress({"stage": "scan", "state": "done", "percent": 1.0})
    assert walk(model, model.StateRole, 1) == "done"
    assert walk(model, model.PercentRole, 1) == pytest.approx(1.0)


def test_stages_model_reset_stages(qapp: Any) -> None:
    model = StagesModel()
    model.set_state("scan", StageState.DONE.value, "done")
    model.apply_progress({"stage": "scan", "percent": 1.0, "message": "done", "current": "x"})
    model.reset_stages()
    assert all(walk(model, model.StateRole, row) == "pending" for row in range(7))
    assert all(walk(model, model.PercentRole, row) == 0.0 for row in range(7))
    assert all(walk(model, model.MessageRole, row) == "" for row in range(7))
    assert all(walk(model, model.IndexRole, row) == row for row in range(7))


# ---------------------------------------------------------------------------
# FindingsModel
# ---------------------------------------------------------------------------


def test_findings_model_append_and_clear(qapp: Any) -> None:
    model = FindingsModel()
    assert model.rowCount() == 0
    assert model.count == 0

    finding = Finding(
        path="/media/usb/eicar.com",
        threat="EICAR-AV-Test-File",
        engine="heuristic",
        severity=Severity.CRITICAL,
        category="test-file",
        size=68,
        detail="the EICAR test pattern is present",
    )
    model.append(finding)

    assert model.rowCount() == 1
    assert model.count == 1
    assert walk(model, model.PathRole, 0) == finding.path
    assert walk(model, model.NameRole, 0) == "eicar.com"
    assert walk(model, model.ThreatRole, 0) == "EICAR-AV-Test-File"
    assert walk(model, model.SeverityRole, 0) == "critical"
    assert walk(model, model.SeverityRankRole, 0) == 4
    assert walk(model, model.SizeRole, 0) == "68 B"

    # a plain dict works too, which is what the worker thread emits
    model.append(
        {
            "path": "/media/usb/a.txt",
            "name": "a.txt",
            "threat": "note",
            "severity": "low",
            "severityRank": 1,
        }
    )
    assert model.rowCount() == 2
    assert walk(model, model.NameRole, 1) == "a.txt"
    assert walk(model, model.SeverityRole, 1) == "low"

    counts: list[int] = []
    model.countChanged.connect(lambda: counts.append(model.count))
    model.clear()
    assert model.rowCount() == 0 and model.count == 0
    assert counts == [0]


def test_findings_model_warns_only_about_severe_findings(qapp: Any) -> None:
    model = FindingsModel()
    seen: list[tuple[str, str]] = []
    model.severeFindingAdded.connect(lambda severity, threat: seen.append((severity, threat)))

    model.append({"threat": "note", "severity": "low", "severityRank": 1})
    model.append({"threat": "greyware", "severity": "medium", "severityRank": 2})
    assert seen == []

    model.append({"threat": "double extension executable", "severity": "high", "severityRank": 3})
    model.append({"threat": "EICAR-AV-Test-File", "severity": "critical", "severityRank": 4})
    assert seen == [
        ("high", "double extension executable"),
        ("critical", "EICAR-AV-Test-File"),
    ]


# ---------------------------------------------------------------------------
# LogModel
# ---------------------------------------------------------------------------


def test_log_model_appends_and_renders_text(qapp: Any) -> None:
    model = LogModel()
    model.append(LogRecordView(time="12:00:01", level="INFO", logger="dcs.scanner", message="started"))
    assert model.rowCount() == 1
    assert walk(model, model.TimeRole, 0) == "12:00:01"
    assert walk(model, model.LevelRole, 0) == "INFO"
    assert walk(model, model.MessageRole, 0) == "started"
    assert walk(model, model.LoggerRole, 0) == "dcs.scanner"
    assert model.as_text() == "12:00:01 INFO    started"

    model.append({"time": "12:00:02", "level": "ERROR", "message": "boom", "logger": "dcs"})
    assert "12:00:02 ERROR   boom" in model.as_text()

    model.clear()
    assert model.rowCount() == 0 and model.as_text() == ""


def test_log_model_keeps_only_the_ring_capacity(qapp: Any) -> None:
    model = LogModel()
    for index in range(LogModel.MAX_ROWS + 5):
        model.append({"time": "00:00:00", "level": "INFO", "message": f"line {index}", "logger": "dcs"})
    assert model.rowCount() == LogModel.MAX_ROWS
    assert walk(model, model.MessageRole, 0) == "line 5"
    assert walk(model, model.MessageRole, LogModel.MAX_ROWS - 1) == f"line {LogModel.MAX_ROWS + 4}"


# ---------------------------------------------------------------------------
# ThroughputModel
# ---------------------------------------------------------------------------


def test_throughput_model_normalises_against_the_peak(qapp: Any) -> None:
    model = ThroughputModel()
    assert model.rowCount() == 0
    assert model.peak == 1.0
    assert model.latestRate == 0.0

    for rate in (0.0, 1000.0, 500.0):
        model.append({"stage": "scan", "percent": 0.5, "rate": rate})

    assert model.rowCount() == 3
    assert model.peak == pytest.approx(1000.0)
    assert model.latestRate == pytest.approx(500.0)
    assert walk(model, model.RateRole, 0) == 0.0
    assert walk(model, model.RateRole, 1) == pytest.approx(1.0)
    assert walk(model, model.RateRole, 2) == pytest.approx(0.5)
    assert walk(model, model.PercentRole, 2) == pytest.approx(0.5)
    assert walk(model, model.StageRole, 2) == "scan"

    # a new maximum re-normalises the whole series
    model.append({"stage": "scan", "rate": 2000.0})
    assert model.peak == pytest.approx(2000.0)
    assert walk(model, model.RateRole, 2) == pytest.approx(0.25)


def test_throughput_model_accepts_stage_progress_and_caps_rows(qapp: Any) -> None:
    model = ThroughputModel()
    model.append(StageProgress(stage=Stage.UPLOAD, rate=123.0, percent=0.25))
    assert model.rowCount() == 1
    assert model.peak == pytest.approx(123.0)
    assert walk(model, model.StageRole, 0) == "upload"

    for _ in range(ThroughputModel.MAX_ROWS + 10):
        model.append({"stage": "scan", "rate": 1.0})
    assert model.rowCount() == ThroughputModel.MAX_ROWS

    model.clear()
    assert model.rowCount() == 0
    assert model.peak == 1.0


# ---------------------------------------------------------------------------
# DevicesModel
# ---------------------------------------------------------------------------


def test_devices_model_exposes_device_fields(qapp: Any) -> None:
    model = DevicesModel()
    device = BlockDevice(
        name="sdb1",
        path="/dev/sdb1",
        type="part",
        size=8 * 1024**3,
        fstype="vfat",
        label="USB STICK",
        uuid="ABCD-1234",
        mountpoints=["/media/usb"],
        transport="usb",
        removable=True,
        model="Ultra",
        vendor="SanDisk",
    )
    counts: list[int] = []
    model.countChanged.connect(lambda: counts.append(model.count))
    model.replace([device])

    assert model.rowCount() == 1
    assert model.count == 1
    assert counts == [1]
    assert walk(model, model.PathRole, 0) == "/dev/sdb1"
    assert walk(model, model.LabelRole, 0) == "USB STICK"
    assert walk(model, model.SizeRole, 0) == "8.0 GiB"
    assert walk(model, model.MountRole, 0) == "/media/usb"
    assert walk(model, model.MountedRole, 0) is True
    assert walk(model, model.BusRole, 0) == "USB"
    assert walk(model, model.DataRole, 0)["scannable"] is True
    assert model.device_at(0) is device
    assert model.device_at(5) is None


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


def test_backend_starts_idle_with_providers_and_engines(backend: Backend) -> None:
    assert backend.state == IDLE
    assert backend.busy is False
    assert backend.overallPercent == 0.0
    assert backend.appVersion == __version__
    assert backend.appName == APP_NAME
    # the source is empty unless the machine running the suite happens to have a
    # removable medium plugged in, in which case it is selected automatically
    assert backend.sourcePath == "" or Path(backend.sourcePath).is_dir()
    assert backend.summary == {}

    catalog = provider_catalog()
    assert backend.providers == catalog
    ids = {entry["id"] for entry in backend.providers}
    assert {"gofile", "pixeldrain", "tmpfiles", "litterbox", "x0at", "catbox", "tempsh"} == ids
    assert any(entry["requiresKey"] for entry in backend.providers)
    for entry in catalog:
        assert set(entry) == {"id", "label", "requiresKey", "maxBytes", "notes", "verified"}

    assert backend.engineStatus
    ids = {entry["id"] for entry in backend.engineStatus}
    assert ids == {"heuristic"}  # tmp_config enables the built-in engine only
    assert backend.engineStatus[0]["available"] is True

    settings = backend.settings
    assert settings["email"]["password"] == ""
    assert settings["scan"]["engines"] == ["heuristic"]


def test_backend_masks_secrets_in_settings(qapp: Any, tmp_config: AppConfig) -> None:
    tmp_config.email.password = "hunter2"
    tmp_config.upload.gofile.token = "token-123"
    instance = Backend(tmp_config, poll_devices=False)
    try:
        assert instance.settings["email"]["password"] == "***"
        assert instance.settings["upload"]["gofile"]["token"] == "***"
    finally:
        log_ring().remove_listener(instance._on_log_record)


def test_backend_stage_row_shape(backend: Backend) -> None:
    row = backend.stageRow(0)
    assert set(row) == {
        "id",
        "title",
        "description",
        "icon",
        "state",
        "percent",
        "message",
        "current",
        "elapsed",
        "eta",
        "rate",
        "processed",
    }
    assert row["id"] == "inspect"
    assert row["title"] == STAGE_TITLES[Stage.INSPECT]
    assert row["state"] == "pending"
    assert row["percent"] == 0.0
    assert backend.stageRow(-1) == {}
    assert backend.stageRow(99) == {}

    backend.stages_model.set_state("inspect", StageState.DONE.value, "USB STICK at /media/usb")
    updated = backend.stageRow(0)
    assert updated["state"] == "done"
    assert updated["percent"] == 1.0
    assert updated["message"] == "USB STICK at /media/usb"


def test_backend_progress_updates_the_live_values(backend: Backend, qapp: Any) -> None:
    progress = StageProgress(
        stage=Stage.UPLOAD,
        processed=1024,
        total=4096,
        bytes_processed=1024,
        bytes_total=4096,
        current="/tmp/image.zip",
        message="1.0 KiB of 4.0 KiB sent",
        rate=512.0,
        elapsed=2.0,
        eta=6.0,
        percent=0.25,
    )
    changed: list[int] = []
    backend.stateChanged.connect(lambda: changed.append(1))
    backend._on_progress(progress.to_dict())

    assert backend.liveStageTitle == STAGE_TITLES[Stage.UPLOAD]
    assert backend.liveCurrent == "/tmp/image.zip"
    assert backend.livePercent == pytest.approx(0.25)
    assert backend.liveRate == pytest.approx(512.0)
    assert backend.liveMessage == "1.0 KiB of 4.0 KiB sent"
    assert backend.liveEtaText == "00:06"
    # an upload has no file count, so the "Files inspected" tile must not borrow
    # the upload byte counters: it reports the scan, which has not run here yet
    assert backend.liveFilesText == "-"
    assert backend.liveBytesText == "1.0 KiB / 4.0 KiB"
    assert changed  # stateChanged drives every live property
    # the overall bar is the stage index plus the stage fraction
    expected = (list(STAGE_ORDER).index(Stage.UPLOAD) + 0.25) / len(STAGE_ORDER)
    assert backend.overallPercent == pytest.approx(expected)
    assert backend.stageRow(4)["state"] == "running"
    assert backend.throughput_model.rowCount() == 1


def test_live_tiles_keep_reporting_the_scan_while_a_later_stage_runs(
    backend: Backend, qapp: Any
) -> None:
    """The scan numbers survive the archive and upload stages.

    Regression test for the tiles that showed "-" as soon as the upload (which
    reports bytes, not files) became the active stage.
    """
    scan = StageProgress(
        stage=Stage.SCAN,
        processed=120,
        total=500,
        bytes_processed=120 * 1024,
        bytes_total=500 * 1024,
        current="/media/usb/file.bin",
        rate=1024 * 1024,
        elapsed=12.0,
        percent=0.24,
    )
    backend._state = "running"
    backend._on_progress(scan.to_dict())
    assert backend.liveFilesText == "120 / 500"
    assert backend.liveBytesText == "120.0 KiB / 500.0 KiB"
    assert backend.liveRateText != "-"

    upload = StageProgress(
        stage=Stage.UPLOAD,
        bytes_processed=64 * 1024,
        bytes_total=500 * 1024,
        current="/tmp/image.zip",
        percent=0.5,
    )
    backend._on_progress(upload.to_dict())
    # the headline still describes the upload, but the tiles keep the scan values
    assert backend.liveStageTitle == STAGE_TITLES[Stage.UPLOAD]
    assert backend.liveFilesText == "120 / 500"
    assert backend.liveBytesText == "120.0 KiB / 500.0 KiB"

    backend._state = "finished"
    backend._summary = {
        "filesScanned": 500,
        "bytesScannedText": "500.0 KiB",
        "bytesScanned": 500 * 1024,
        "durationText": "00:25",
        "scan": {"stats": {"duration": 20.0}},
    }
    assert backend.liveFilesText == "500"
    assert backend.liveBytesText == "500.0 KiB"
    assert backend.liveRateText == "25.0 KiB/s"  # average over the scan
    assert backend.liveElapsedText == "00:25"


# ---------------------------------------------------------------------------
# the QML interface
# ---------------------------------------------------------------------------


def _is_ignored(message: str) -> bool:
    return any(marker in message for marker in IGNORED_QML_WARNINGS)


def test_main_qml_loads_and_tears_down_without_warnings(
    backend: Backend, qapp: Any, qt_messages: list[str]
) -> None:
    """The real entry point loads Main.qml, binds to the backend and tears down cleanly."""
    qml_warnings: list[str] = []

    def collect(record: Any) -> None:
        if record.message.startswith("QML:"):
            qml_warnings.append(record.message)

    log_ring().add_listener(collect)
    engine, loaded_backend = gui_app.build_engine(backend.config, backend)
    try:
        assert loaded_backend is backend
        qapp.processEvents()

        roots = engine.rootObjects()
        assert len(roots) == 1
        window = roots[0]
        assert window.property("title") == f"{APP_NAME} - USB triage"
        # the window binds its own properties to the backend context object
        assert window.property("jobActive") is False
        window.setProperty("tabIndex", 3)
        assert window.property("tabIndex") == 3
        # the module keeps the engine and the backend alive for the session, so
        # no QML binding can ever re-evaluate against a collected backend
        assert gui_app._SESSION["backend"] is backend
        assert gui_app._SESSION["engine"] is engine

        # teardown is part of the contract: it must destroy the QML tree before
        # the interpreter drops the backend, and stay silent while doing it
        gui_app.teardown_engine(engine)
        qapp.processEvents()
        assert gui_app._SESSION == {}
    finally:
        log_ring().remove_listener(collect)

    assert qml_warnings == []
    assert [message for message in qt_messages if not _is_ignored(message)] == []


def test_qml_directory_and_import_paths_exist() -> None:
    directory = Path(gui_app.qml_directory())
    assert directory.is_dir()
    assert (directory / "Main.qml").is_file()
    paths = gui_app.find_qml_imports()
    assert str(directory) in paths
    assert paths[0] == str(directory)
    assert len(paths) == len(set(paths))
    assert all(isinstance(path, str) for path in paths)


def test_stage_rows_are_laid_out_without_overlapping(
    backend: Backend, qapp: Any, qt_messages: list[str]
) -> None:
    """The pipeline rows must each get their own vertical space.

    Regression test for a binding loop: the row derived its implicitHeight from
    a layout that was anchored with anchors.fill, and Qt resolved the loop by
    collapsing every row onto the same y, so the whole pipeline was drawn on top
    of itself.
    """
    from PyQt6.QtQuick import QQuickItem

    engine, _ = gui_app.build_engine(backend.config, backend=backend)
    window = engine.rootObjects()[0]
    window.setProperty("width", 1280)
    window.setProperty("height", 950)
    window.show()
    window.metaObject().invokeMethod(window, "showTab", Q_ARG("QVariant", 1))
    for _ in range(40):
        qapp.processEvents()
        time.sleep(0.01)

    items: list[QQuickItem] = []

    def walk(item: QQuickItem) -> None:
        items.append(item)
        for child in item.childItems():
            walk(child)

    walk(window.contentItem())
    rows = [item for item in items if "StageDelegate" in item.metaObject().className()]
    assert len(rows) == len(STAGE_ORDER), f"expected {len(STAGE_ORDER)} stage rows, got {len(rows)}"

    geometry = sorted((round(row.y(), 1), round(row.height(), 1)) for row in rows)
    ys = [y for y, _ in geometry]
    heights = [height for _, height in geometry]
    assert all(height > 20 for height in heights), f"collapsed rows: {geometry}"
    assert ys == sorted(ys) and len(set(ys)) == len(ys), f"rows share a position: {geometry}"
    for (y, height), (next_y, _) in itertools.pairwise(geometry):
        assert y + height <= next_y + 1, f"rows overlap: {geometry}"

    gui_app.teardown_engine(engine)

def test_no_empty_toolbar_band_above_the_navigation(
    backend: Backend, qapp: Any, qt_messages: list[str]
) -> None:
    """The tab bar must sit at the top of the window.

    Kirigami builds a page toolbar row and reserves its height (48 px with the
    Breeze style) even when the page has no title, no actions and no drawer, so
    the interface used to start with an empty band where the hamburger had been.
    The page sets Kirigami.ApplicationHeaderStyle.None, and this asserts the
    geometry rather than the property: the navigation starts at the top.
    """
    from PyQt6.QtCore import QPointF
    from PyQt6.QtQuick import QQuickItem

    engine, _ = gui_app.build_engine(backend.config, backend=backend)
    window = engine.rootObjects()[0]
    window.setProperty("width", 1280)
    window.setProperty("height", 900)
    window.show()
    for _ in range(60):
        qapp.processEvents()
        time.sleep(0.01)

    content = window.contentItem()
    items: list[QQuickItem] = []

    def walk(item: QQuickItem) -> None:
        items.append(item)
        for child in item.childItems():
            walk(child)

    walk(content)
    tab_bars = [item for item in items if "TabBar" in item.metaObject().className()]
    assert tab_bars, "the navigation tab bar was not found"
    top = tab_bars[0].mapToItem(content, QPointF(0, 0)).y()
    assert top < 12, f"an empty header band is reserved above the navigation (tab bar y={top})"

    # and the toolbar row must not be shown either: Kirigami keeps the item in
    # the tree, so what matters is that nothing above the navigation is visible
    toolbar_rows = [
        item
        for item in items
        if "ToolBarPageHeader" in item.metaObject().className() and item.isVisible()
    ]
    assert not toolbar_rows, "the page toolbar row is still being shown"

    gui_app.teardown_engine(engine)

def test_status_bar_is_inside_the_page_and_reaches_the_bottom(
    backend: Backend, qapp: Any, qt_messages: list[str]
) -> None:
    """The status bar is the last row of the page, not the window footer.

    Regression test for a footer that came out about two thirds tall: the styled
    footer container (Kirigami.AbstractApplicationHeader, and a ToolBar with an
    explicit height) painted its background at its own implicit height while the
    window content, which Kirigami raises above it, covered the rest.  Keeping
    the bar inside the page removes the whole class of problem.
    """
    from PyQt6.QtCore import QObject, QPointF
    from PyQt6.QtQuick import QQuickItem

    engine, _ = gui_app.build_engine(backend.config, backend=backend)
    window = engine.rootObjects()[0]
    window.show()
    for _ in range(40):
        qapp.processEvents()
        time.sleep(0.01)

    # an item in the footer slot would mean the bar is back where the style
    # paints it wrong; a null property comes back as a voidptr, not None
    footer = window.property("footer")
    assert not isinstance(footer, QQuickItem), "the window footer slot is used again"

    status_bar = window.findChild(QObject, "statusBar")
    assert status_bar is not None, "the status bar was not found"
    # the bar only exists while a job runs or just after it; the layout is only
    # meaningful in that state
    backend._state = "running"
    backend.stateChanged.emit()
    for _ in range(30):
        qapp.processEvents()
        time.sleep(0.01)
    content = window.contentItem()
    top = status_bar.mapToItem(content, QPointF(0, 0)).y()
    assert top + status_bar.height() == pytest.approx(content.height(), abs=1.5), (
        f"the status bar ends at {top + status_bar.height():.1f} instead of the bottom "
        f"of the window ({content.height():.1f})"
    )

    gui_app.teardown_engine(engine)


# ---------------------------------------------------------------------------
# the settings page: an always visible save bar, and a USB watcher
# ---------------------------------------------------------------------------


def settings_page_of(window: Any) -> Any:
    """The SettingsView instance inside a loaded Main.qml."""
    from PyQt6.QtCore import QObject

    for child in window.findChildren(QObject):
        if child.metaObject().className().startswith("SettingsView"):
            return child
    return None


def open_settings(backend: Backend, qapp: Any) -> tuple[Any, Any, Any]:
    """Load the interface, show it and switch to the settings tab."""
    engine, _ = gui_app.build_engine(backend.config, backend=backend)
    window = engine.rootObjects()[0]
    window.show()
    window.setProperty("tabIndex", 3)
    for _ in range(40):
        qapp.processEvents()
        time.sleep(0.01)
    return engine, window, settings_page_of(window)


def test_the_save_button_is_visible_without_scrolling_to_the_bottom(
    backend: Backend, qapp: Any, qt_messages: list[str]
) -> None:
    """Regression test for a save button at the end of a very long page.

    The settings form is much taller than the window, so the button that writes
    the whole draft to disk used to be visible only after scrolling to the very
    bottom - and a change could be left unsaved without noticing.
    """
    from PyQt6.QtCore import QObject, QPointF

    engine, window, page = open_settings(backend, qapp)
    assert page is not None, "the settings page was not built"

    bar = window.findChild(QObject, "saveBar")
    button = window.findChild(QObject, "saveButton")
    assert bar is not None, "the save bar was not found"
    assert button is not None, "the save button was not found"
    assert bar.property("visible") is True
    assert button.property("visible") is True

    content = window.contentItem()
    bar_bottom = bar.mapToItem(content, QPointF(0, 0)).y() + bar.height()
    page_bottom = page.mapToItem(content, QPointF(0, 0)).y() + page.height()
    assert bar_bottom == pytest.approx(page_bottom, abs=1.5), (
        f"the save bar ends at {bar_bottom:.1f} instead of the bottom of the settings page "
        f"({page_bottom:.1f})"
    )
    assert bar.width() == pytest.approx(page.width(), abs=1.5)
    # and the button really is inside that bar
    button_top = button.mapToItem(content, QPointF(0, 0)).y()
    assert button_top >= bar.mapToItem(content, QPointF(0, 0)).y() - 1
    assert button_top + button.height() <= bar_bottom + 1

    gui_app.teardown_engine(engine)


def test_the_form_scrolls_but_the_bar_does_not(
    backend: Backend, qapp: Any, qt_messages: list[str]
) -> None:
    """The page really is taller than its viewport, which is the whole point."""
    from PyQt6.QtCore import QObject

    engine, window, _ = open_settings(backend, qapp)
    scroll = window.findChild(QObject, "settingsScroll")
    assert scroll is not None, "the settings form is not in a scroll view"
    assert scroll.property("contentHeight") > scroll.property("height")

    # scrolling to the bottom must not move the bar
    bar = window.findChild(QObject, "saveBar")
    before = bar.property("y")
    scroll.setProperty("contentY", scroll.property("contentHeight") - scroll.property("height"))
    for _ in range(5):
        qapp.processEvents()
    assert bar.property("y") == pytest.approx(before, abs=0.5)

    gui_app.teardown_engine(engine)


def test_the_bar_says_whether_there_is_anything_to_save(
    backend: Backend, qapp: Any, qt_messages: list[str]
) -> None:
    from PyQt6.QtCore import QMetaObject

    engine, _window, page = open_settings(backend, qapp)
    assert page is not None
    assert page.property("unsavedChanges") is False

    # editing one field of the draft makes it dirty
    page.setProperty("smtpHost", "smtp.changed.test")
    for _ in range(5):
        qapp.processEvents()
    assert page.property("unsavedChanges") is True

    # Revert throws the draft away
    QMetaObject.invokeMethod(page, "reset")
    for _ in range(5):
        qapp.processEvents()
    assert page.property("unsavedChanges") is False
    assert page.property("smtpHost") == backend.config.email.smtp_host

    gui_app.teardown_engine(engine)


def test_saving_from_the_bar_clears_the_dirty_state(
    backend: Backend, qapp: Any, qt_messages: list[str]
) -> None:
    from PyQt6.QtCore import QMetaObject

    engine, _window, page = open_settings(backend, qapp)
    page.setProperty("smtpHost", "smtp.saved.test")
    for _ in range(5):
        qapp.processEvents()
    assert page.property("unsavedChanges") is True

    QMetaObject.invokeMethod(page, "save")
    for _ in range(10):
        qapp.processEvents()

    assert backend.config.email.smtp_host == "smtp.saved.test"
    assert page.property("unsavedChanges") is False
    assert backend.saveOk is True
    assert "Saved to" in backend.saveMessage
    assert Path(backend.config.source_path).is_file()

    gui_app.teardown_engine(engine)


def test_test_smtp_uses_the_values_in_the_form(
    backend: Backend, qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: "Test SMTP" used to test what was saved last.

    Editing the form and pressing Test reported a failure about values the user
    could not even see, and saving afterwards made the same values work.
    """
    import dcs.core.notify as notify_module

    seen: list[dict[str, Any]] = []

    class FakeNotifier:
        def __init__(self, email: Any, *, password: str = "", username: str = "") -> None:
            seen.append(
                {
                    "host": email.smtp_host,
                    "port": email.smtp_port,
                    "security": email.security,
                    "username": email.username or username,
                    "from_addr": email.from_addr,
                    "to_addrs": list(email.to_addrs),
                    "password": password,
                }
            )

        def test_connection(self) -> tuple[bool, str]:
            return True, "server said hello"

    monkeypatch.setattr(notify_module, "Notifier", FakeNotifier)

    results: list[tuple[str, bool, str]] = []
    backend.testResult.connect(lambda what, ok, message: results.append((what, ok, message)))
    backend.testSmtp(
        {
            "smtp_host": "smtp.draft.test",
            "smtp_port": 2525,
            "security": "ssl",
            "username": "draft-user",
            "from_addr": "draft@test.test",
            "to_addrs": ["a@test.test", "b@test.test"],
            "password": "draft-secret",
        }
    )
    for _ in range(200):
        qapp.processEvents()
        if results:
            break
        time.sleep(0.01)

    assert seen, "the notifier was never asked to test anything"
    assert seen[0]["host"] == "smtp.draft.test"
    assert seen[0]["port"] == 2525
    assert seen[0]["security"] == "ssl"
    assert seen[0]["from_addr"] == "draft@test.test"
    assert seen[0]["to_addrs"] == ["a@test.test", "b@test.test"]
    assert seen[0]["password"] == "draft-secret"
    assert results == [("smtp", True, "server said hello")]


def test_test_smtp_without_a_draft_tests_the_saved_settings(
    backend: Backend, qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dcs.core.notify as notify_module

    backend.config.email.smtp_host = "smtp.saved.test"
    backend.config.email.password = "stored-secret"
    seen: list[str] = []

    class FakeNotifier:
        def __init__(self, email: Any, *, password: str = "", username: str = "") -> None:
            seen.append(f"{email.smtp_host}|{password}")

        def test_connection(self) -> tuple[bool, str]:
            return True, "ok"

    monkeypatch.setattr(notify_module, "Notifier", FakeNotifier)
    backend.testSmtp()
    for _ in range(200):
        qapp.processEvents()
        if seen:
            break
        time.sleep(0.01)

    assert seen == ["smtp.saved.test|stored-secret"]


def test_the_usb_card_offers_the_four_modes(backend: Backend) -> None:
    modes = backend.watchModes
    assert [entry["value"] for entry in modes] == ["off", "notify", "open", "open_run"]
    for entry in modes:
        assert entry["label"]
        assert entry["description"]
    assert backend.watchMode == "off"
    assert backend.watchActive is False, "nothing runs while the mode is off"


def test_saving_the_mode_starts_the_watcher_and_writes_the_autostart_entry(
    backend: Backend, qapp: Any
) -> None:
    from dcs.core import autostart

    assert autostart.is_enabled() is False

    backend.saveSettings({"watch": {"mode": "notify", "autostart": True}})
    for _ in range(5):
        qapp.processEvents()

    assert backend.watchMode == "notify"
    assert backend.watchActive is True
    assert backend.watchAutostart is True
    assert autostart.is_enabled() is True
    assert "watch" in autostart.autostart_path().read_text(encoding="utf-8")

    backend.saveSettings({"watch": {"mode": "off", "autostart": True}})
    for _ in range(5):
        qapp.processEvents()

    assert backend.watchActive is False
    assert autostart.is_enabled() is False, "nothing may watch while the mode is off"


def test_a_plugged_medium_is_offered_on_the_new_job_page(
    backend: Backend, qapp: Any, monkeypatch: pytest.MonkeyPatch, qt_messages: list[str]
) -> None:
    from PyQt6.QtCore import QObject

    import dcs.gui.backend as backend_module

    notifications: list[str] = []

    class FakeNotifier:
        def available(self) -> bool:
            return True

        def notify(self, title: str, body: str = "", **kwargs: Any) -> str:
            notifications.append(title)
            return ""

    monkeypatch.setattr(backend_module, "build_notifier", lambda **kwargs: FakeNotifier())
    monkeypatch.setattr(backend, "openDevice", lambda node, autorun=False: True)

    backend.config.watch.mode = "notify"
    payload = {
        "node": "/dev/sdb1",
        "displayName": "HOLIDAY",
        "sizeText": "7.6 GiB",
        "mountPath": "/media/u/HOLIDAY",
        "summary": "HOLIDAY (7.6 GiB, /media/u/HOLIDAY)",
    }
    alerts: list[dict[str, Any]] = []
    backend.deviceInserted.connect(alerts.append)

    engine, _backend = gui_app.build_engine(backend.config, backend=backend)
    window = engine.rootObjects()[0]
    window.show()
    for _ in range(20):
        qapp.processEvents()
        time.sleep(0.01)

    card = window.findChild(QObject, "newDeviceCard")
    assert card is not None, "the new medium banner was not found"
    assert card.property("visible") is False, "nothing is plugged in yet"

    backend._on_device_inserted(payload)
    for _ in range(30):
        qapp.processEvents()
        time.sleep(0.01)

    assert alerts and alerts[0]["node"] == "/dev/sdb1"
    assert backend.pendingDevice["displayName"] == "HOLIDAY"
    # the HIG forbids a system notification while the window is in front: the
    # banner and the passive notification carry the message there, and only a
    # backgrounded window sends one
    assert notifications == [], "a foreground window must not send a system notification"
    assert card.property("visible") is True, "the banner did not appear"
    assert backend.state == IDLE, "notify mode must not read anything from the medium"

    backend.dismissPendingDevice()
    for _ in range(20):
        qapp.processEvents()
        time.sleep(0.01)
    assert backend.pendingDevice == {}
    assert card.property("visible") is False

    gui_app.teardown_engine(engine)


def test_open_mode_selects_the_medium_without_starting_a_job(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        backend, "openDevice", lambda node, autorun=False: calls.append((node, autorun)) or True
    )
    monkeypatch.setattr(backend, "startJob", lambda options: calls.append(("job", dict(options))))
    backend.config.watch.mode = "open"

    backend._on_device_inserted({"node": "/dev/sdb1", "displayName": "HOLIDAY"})

    assert calls == [("/dev/sdb1", False)]


def test_open_run_mode_opens_the_medium_and_asks_for_the_job(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        backend, "openDevice", lambda node, autorun=False: opened.append((node, autorun)) or True
    )
    backend.config.watch.mode = "open_run"

    backend._on_device_inserted({"node": "/dev/sdb1", "displayName": "HOLIDAY"})

    assert opened == [("/dev/sdb1", True)], "open_run has to ask for the job as well"


def test_opening_a_medium_with_autorun_starts_the_job(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dcs.gui.backend as backend_module

    device = BlockDevice(
        name="sdb1",
        path="/dev/sdb1",
        type="part",
        size=7_600_000_000,
        fstype="vfat",
        label="HOLIDAY",
        transport="usb",
        removable=True,
        mountpoints=["/media/aleks/HOLIDAY"],
    )
    jobs: list[dict[str, Any]] = []
    monkeypatch.setattr(backend_module, "find_device", lambda node: device)
    monkeypatch.setattr(backend, "refreshDevices", lambda: None)
    monkeypatch.setattr(backend, "startJob", jobs.append)
    backend.config.upload.enabled = True
    backend.config.email.to_addrs = ["ops@example.test"]

    assert backend.openDevice("/dev/sdb1", autorun=True) is True

    assert len(jobs) == 1, "the automatic run has to start exactly one job"
    assert jobs[0]["source"] == "/media/aleks/HOLIDAY"
    assert jobs[0]["assumeYes"] is True, "nobody is at the keyboard to confirm"
    assert jobs[0]["email"] is True
    assert jobs[0]["recipients"] == "ops@example.test"


def test_opening_a_medium_without_autorun_runs_nothing(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dcs.gui.backend as backend_module

    device = BlockDevice(
        name="sdb1",
        path="/dev/sdb1",
        type="part",
        fstype="vfat",
        label="HOLIDAY",
        transport="usb",
        removable=True,
        mountpoints=["/media/aleks/HOLIDAY"],
    )
    jobs: list[dict[str, Any]] = []
    monkeypatch.setattr(backend_module, "find_device", lambda node: device)
    monkeypatch.setattr(backend, "refreshDevices", lambda: None)
    monkeypatch.setattr(backend, "startJob", jobs.append)

    assert backend.openDevice("/dev/sdb1") is True
    assert jobs == []
    assert backend.sourcePath == "/media/aleks/HOLIDAY"


def test_an_automatic_run_without_a_recipient_still_makes_the_link(
    backend: Backend,
) -> None:
    backend.config.email.to_addrs = []
    backend.config.upload.enabled = True
    backend.config.archive.enabled = True

    options = backend.autoRunOptions()

    assert options["email"] is False, "no recipient, no mail"
    assert options["upload"] is True, "the link is the point of the automatic run"
    assert options["archive"] is True


def test_an_automatic_run_follows_the_disabled_switches(backend: Backend) -> None:
    backend.config.upload.enabled = False
    backend.config.archive.enabled = False

    options = backend.autoRunOptions()

    assert options["upload"] is False
    assert options["archive"] is False
    assert options["scan"] is True, "scanning always happens"


def test_the_watcher_is_not_started_while_its_mode_is_off(backend: Backend) -> None:
    assert backend.watchMode == "off"
    assert backend.watcher.active is False


def test_a_backgrounded_window_sends_a_system_notification(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the rule: no window in front, so notify the desktop."""
    import dcs.gui.backend as backend_module

    sent: list[str] = []

    class FakeNotifier:
        def available(self) -> bool:
            return True

        def notify(self, title: str, body: str = "", **kwargs: Any) -> str:
            sent.append(title)
            return ""

    monkeypatch.setattr(backend_module, "build_notifier", lambda **kwargs: FakeNotifier())
    monkeypatch.setattr(backend, "_window_is_active", staticmethod(lambda: False))
    monkeypatch.setattr(backend, "openDevice", lambda node, autorun=False: True)
    backend.config.watch.mode = "notify"

    backend._on_device_inserted({"node": "/dev/sdb1", "displayName": "HOLIDAY"})

    assert sent and "HOLIDAY" in sent[0]


# ---------------------------------------------------------------------------
# the KDE Human Interface Guidelines, where they are checkable
# ---------------------------------------------------------------------------


def test_the_navigation_is_the_control_the_hig_asks_for(
    backend: Backend, qapp: Any, qt_messages: list[str]
) -> None:
    """Five destinations that are not a linear flow: Kirigami.NavigationTabBar.

    The HIG is explicit ("with five or fewer destinations, use a
    Kirigami.NavigationTabBar"), and it is also the control for immutable tabs.
    """
    from PyQt6.QtCore import QObject

    engine, window, _page = open_settings(backend, qapp)
    bar = next(
        (
            child
            for child in window.findChildren(QObject)
            if "NavigationTabBar" in child.metaObject().className()
        ),
        None,
    )
    assert bar is not None, "the navigation is not a Kirigami.NavigationTabBar"
    assert bar.property("count") == 5

    for index in range(5):
        window.setProperty("tabIndex", index)
        for _ in range(3):
            qapp.processEvents()
        assert bar.property("currentIndex") == index, f"destination {index} does not follow"

    gui_app.teardown_engine(engine)


def test_the_settings_page_validates_the_draft_and_disables_save(
    backend: Backend, qapp: Any, qt_messages: list[str]
) -> None:
    """Invalid input is shown in an InlineMessage and Save is disabled.

    "When the current text is invalid, indicate this using a
    Kirigami.InlineMessage and disable the ability to confirm or send the input."
    """
    from PyQt6.QtCore import QObject

    engine, window, page = open_settings(backend, qapp)
    message = window.findChild(QObject, "validationMessage")
    save = window.findChild(QObject, "saveButton")
    assert message is not None and save is not None

    # a fresh configuration is valid, so a new user meets no warning at all
    assert page.property("validationText") == ""
    assert message.property("visible") is False

    # enabling email without a server is exactly what the validator is for
    page.setProperty("emailEnabled", True)
    page.setProperty("smtpHost", "")
    page.setProperty("fromAddr", "")
    page.setProperty("recipients", "")
    for _ in range(5):
        qapp.processEvents()
    assert "smtp_host" in page.property("validationText")
    assert message.property("visible") is True
    assert save.property("enabled") is False, "invalid input must not be committable"

    # and a valid draft that differs from the file can be saved again
    page.setProperty("smtpHost", "smtp.changed.test")
    page.setProperty("fromAddr", "dcs@example.test")
    page.setProperty("recipients", "ops@example.test")
    for _ in range(5):
        qapp.processEvents()
    assert page.property("validationText") == ""
    assert page.property("unsavedChanges") is True
    assert save.property("enabled") is True

    gui_app.teardown_engine(engine)


def test_validate_draft_reports_what_the_candidate_would_break(backend: Backend) -> None:
    before = backend.config.to_dict()

    assert backend.validateDraft({}) == ""
    problems = backend.validateDraft({"email": {"enabled": True, "smtp_host": ""}})
    assert "smtp_host" in problems
    assert problems.startswith("• "), "one bullet per problem, ready for the message"

    # the candidate is validated on a copy: the stored configuration is untouched
    assert backend.config.to_dict() == before


def test_the_password_field_is_the_kirigami_control(
    backend: Backend, qapp: Any, qt_messages: list[str]
) -> None:
    """The HIG asks for Kirigami.PasswordField, which brings the reveal button."""
    from PyQt6.QtCore import QObject

    engine, window, _page = open_settings(backend, qapp)
    field = window.findChild(QObject, "smtpPasswordField")
    assert field is not None, "the password field was not found"
    assert "PasswordField" in field.metaObject().className()
    assert field.property("showPassword") is False

    gui_app.teardown_engine(engine)


def test_no_label_hides_a_dialog_behind_three_dots() -> None:
    """The HIG asks for the ellipsis character (U+2026), not three periods."""
    offenders = []
    for path in (ROOT_QML).rglob("*.qml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "text:" in line and "..." in line and "//" not in line.split("text:")[0]:
                offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, "use the … character instead: " + ", ".join(offenders)


def test_the_well_known_labels_follow_the_capitalization_rule() -> None:
    """Sentence case for labels in front of controls, title case elsewhere.

    Only the labels that were wrong once are pinned here: a button that opens a
    dialog ends with an ellipsis, and interactive labels are in title case.
    """
    settings = (ROOT_QML / "views" / "SettingsView.qml").read_text(encoding="utf-8")
    for label in ("Save Settings", "Test SMTP", "Install ClamAV…", "Open Config File", "Revert"):
        assert f'"{label}"' in settings, f"{label} is not in the settings page"
    assert '"Save settings"' not in settings and '"Open report folder"' not in settings

    main = (ROOT_QML / "Main.qml").read_text(encoding="utf-8")
    for label in ("New Job", "Progress", "Result", "Settings", "About"):
        assert f'"{label}"' in main, f"{label} is not a navigation destination"
    assert '"Scanning…"' in main, "the running state uses the ellipsis character"
    # labels in front of a control stay in sentence case
    assert 'Kirigami.FormData.label: "SMTP server:"' in settings
    assert 'text: "Send the verdict by SMTP"' in settings


def test_the_about_page_shows_the_shipped_icon(backend: Backend, qapp: Any, qt_messages: list[str]) -> None:
    """The artwork is an SVG and Qt only draws it with the SVG image plugin.

    A missing qt6-svg-plugins (a separate package from libqt6svg6) leaves the
    About page with an empty square and a warning nobody reads, so the status of
    the image is asserted here instead.
    """
    from PyQt6.QtCore import QObject

    engine, window, _page = open_settings(backend, qapp)
    window.setProperty("tabIndex", 4)  # the About page
    for _ in range(40):
        qapp.processEvents()
        time.sleep(0.01)

    icon = window.findChild(QObject, "aboutIcon")
    assert icon is not None, "the About page does not show the application icon"
    assert icon.property("source").toString().endswith("icons/dcs.png")
    assert icon.property("implicitWidth") > 0
    assert icon.property("implicitHeight") > 0

    about = next(
        child
        for child in window.findChildren(QObject)
        if child.metaObject().className().startswith("AboutView")
    )
    assert about.property("iconLoaded") is True, (
        "the icon did not load; is the Qt SVG image plugin (qt6-svg-plugins) installed?"
    )

    gui_app.teardown_engine(engine)


# ---------------------------------------------------------------------------
# updates
# ---------------------------------------------------------------------------


def test_the_update_preference_is_saved_and_reloaded(backend: Backend, qapp: Any) -> None:
    """The checkbox in the settings page is a normal setting, not a live toggle."""
    from dcs.core.config import load_config

    assert backend.config.updates.check_on_start is True

    backend.saveSettings({"updates": {"check_on_start": False}})
    for _ in range(5):
        qapp.processEvents()

    path = backend.config.source_path
    assert "check_on_start = false" in Path(path).read_text(encoding="utf-8")
    assert load_config(path).updates.check_on_start is False


def test_the_update_preference_defaults_to_on(backend: Backend) -> None:
    assert UpdateConfig().check_on_start is True
    assert backend.config.updates.check_on_start is True


def test_the_update_check_reports_what_it_found(
    backend: Backend, qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dcs.core import updates
    from dcs.gui import backend as backend_module

    # one patch above whatever is running, so the test keeps working after a
    # version bump without having to think about it
    parts = [int(part) for part in __version__.split(".")]
    newer = f"{parts[0]}.{parts[1]}.{parts[2] + 1}"
    release = updates.Release.from_payload(
        {
            "tag_name": f"v{newer}",
            "name": f"Ducky Can Shoot {newer}",
            "html_url": f"https://example.invalid/v{newer}",
            "assets": [
                {
                    "name": f"ducky_can_shoot-{newer}-py3-none-any.whl",
                    "browser_download_url": "https://example.invalid/pkg.whl",
                    "size": 581157,
                }
            ],
        }
    )
    check = updates.UpdateCheck(
        current=__version__,
        release=release,
        target=updates.InstallTarget("venv", "venv at /tmp/v"),
    )
    monkeypatch.setattr(backend_module.updates, "check_for_update", lambda **_: check)
    monkeypatch.setattr(backend_module.updates, "write_state", lambda _check: None)

    backend.checkForUpdates()
    for _ in range(200):
        qapp.processEvents()
        if backend.updateState != "checking":
            break
        time.sleep(0.01)

    assert backend.updateState == "available"
    info = backend.updateInfo
    assert info["latest"] == newer
    assert info["canInstall"] is True
    assert info["state"] == "available"
    assert f"{newer} is available" in info["message"]


def test_installing_without_a_check_does_nothing(backend: Backend, qapp: Any) -> None:
    messages: list[tuple[str, str]] = []
    backend.toast.connect(lambda level, message: messages.append((level, message)))

    backend.installUpdate()
    for _ in range(3):
        qapp.processEvents()

    assert backend.updateState == "idle"
    assert messages and messages[-1][0] == "warning"


def test_the_settings_page_offers_the_update_controls() -> None:
    settings = (ROOT_QML / "views" / "SettingsView.qml").read_text(encoding="utf-8")

    assert '"Check for Updates"' in settings
    assert '"Download and Install"' in settings
    assert '"Check for updates when Ducky Can Shoot starts"' in settings
    # the checkbox has to travel in the draft, or Save would silently drop it
    assert "check_on_start" in settings
    assert "payload.updates" in settings


def test_the_interface_checks_for_updates_when_it_starts() -> None:
    source = Path(gui_app.__file__).read_text(encoding="utf-8")

    assert "updates.auto_check_due(backend.config.updates)" in source
    assert "backend.checkForUpdates" in source



