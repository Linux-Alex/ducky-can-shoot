"""Offscreen GUI tests: Qt models, the Backend facade and the QML interface.

The whole module is skipped when PyQt6 or the offscreen platform plugin is
missing, so the headless parts of the suite keep working on a machine without
Qt.  No window is ever shown: QT_QPA_PLATFORM is forced to offscreen before the
QApplication exists.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pytest

pytest.importorskip("PyQt6.QtWidgets", reason="PyQt6 is not installed")
pytest.importorskip("PyQt6.QtQml", reason="PyQt6 QtQml is not installed")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QLibraryInfo, QModelIndex, Qt, QUrl  # noqa: E402
from PyQt6.QtQml import QQmlApplicationEngine  # noqa: E402

_PLATFORM_PLUGINS = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)) / "platforms"
if not list(_PLATFORM_PLUGINS.glob("*offscreen*")):
    pytest.skip("the offscreen Qt platform plugin is not installed", allow_module_level=True)

from dcs.core.config import AppConfig  # noqa: E402
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
from dcs.gui import app as gui_app  # noqa: E402
from dcs.core.upload import provider_catalog  # noqa: E402
from dcs.gui.backend import IDLE, Backend  # noqa: E402
from dcs.gui.models import (  # noqa: E402
    DevicesModel,
    FindingsModel,
    LogModel,
    StagesModel,
    ThroughputModel,
)
from dcs.version import APP_NAME, __version__  # noqa: E402

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
    assert backend.sourcePath == ""
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
    assert backend.liveFilesText == "1,024 / 4,096"
    assert backend.liveBytesText == "1.0 KiB / 4.0 KiB"
    assert backend.liveMessage == "1.0 KiB of 4.0 KiB sent"
    assert backend.liveEtaText == "00:06"
    assert backend.liveElapsedText == "00:02"
    assert changed  # stateChanged drives every live property
    # the overall bar is the stage index plus the stage fraction
    expected = (list(STAGE_ORDER).index(Stage.UPLOAD) + 0.25) / len(STAGE_ORDER)
    assert backend.overallPercent == pytest.approx(expected)
    assert backend.stageRow(4)["state"] == "running"
    assert backend.throughput_model.rowCount() == 1


# ---------------------------------------------------------------------------
# the QML interface
# ---------------------------------------------------------------------------


def _is_ignored(message: str) -> bool:
    return any(marker in message for marker in IGNORED_QML_WARNINGS)


def test_main_qml_loads_without_warnings(backend: Backend, qapp: Any, qt_messages: list[str]) -> None:
    engine_warnings: list[str] = []
    engine = QQmlApplicationEngine()
    for path in gui_app.find_qml_imports():
        engine.addImportPath(path)
    engine.warnings.connect(lambda items: engine_warnings.extend(item.toString() for item in items))
    engine.rootContext().setContextProperty("backend", backend)
    engine.rootContext().setContextProperty("appVersion", __version__)
    engine.rootContext().setContextProperty("appName", APP_NAME)

    main_qml = Path(gui_app.qml_directory()) / "Main.qml"
    engine.load(QUrl.fromLocalFile(str(main_qml)))
    try:
        qapp.processEvents()

        roots = engine.rootObjects()
        assert len(roots) == 1
        window = roots[0]
        assert window.property("title") == f"{APP_NAME} - USB triage"
        # the window binds its own properties to the backend context object
        assert window.property("jobActive") is False
        window.setProperty("tabIndex", 3)
        assert window.property("tabIndex") == 3

        assert engine_warnings == []
        assert [message for message in qt_messages if not _is_ignored(message)] == []
    finally:
        for obj in engine.rootObjects():
            obj.deleteLater()
        qapp.processEvents()


def test_qml_directory_and_import_paths_exist() -> None:
    directory = Path(gui_app.qml_directory())
    assert directory.is_dir()
    assert (directory / "Main.qml").is_file()
    paths = gui_app.find_qml_imports()
    assert str(directory) in paths
    assert paths[0] == str(directory)
    assert len(paths) == len(set(paths))
    assert all(isinstance(path, str) for path in paths)
