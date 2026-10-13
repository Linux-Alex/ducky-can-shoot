"""Qt models behind the QML views.

Five models cover the whole UI:

DevicesModel     - plugged in removable media, refreshed by polling lsblk
StagesModel      - the seven pipeline stages with live progress
FindingsModel    - findings as they stream out of the scan
LogModel         - the tail of the application log
ThroughputModel  - recent rate samples for the little throughput chart

Every model is written so that updates always happen on the GUI thread: the
worker emits Qt signals that are queued into these slots.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QAbstractListModel, QModelIndex, Qt, pyqtProperty, pyqtSignal, pyqtSlot

from dcs.core.device import BlockDevice
from dcs.core.events import (
    STAGE_DESCRIPTIONS,
    STAGE_ICONS,
    STAGE_ORDER,
    STAGE_TITLES,
    StageState,
)
from dcs.core.log import LogRecordView


class DevicesModel(QAbstractListModel):
    """Removable media currently attached."""

    PathRole = Qt.ItemDataRole.UserRole + 1
    NameRole = Qt.ItemDataRole.UserRole + 2
    LabelRole = Qt.ItemDataRole.UserRole + 3
    SizeRole = Qt.ItemDataRole.UserRole + 4
    FsRole = Qt.ItemDataRole.UserRole + 5
    MountRole = Qt.ItemDataRole.UserRole + 6
    MountedRole = Qt.ItemDataRole.UserRole + 7
    BusRole = Qt.ItemDataRole.UserRole + 8
    ModelRole = Qt.ItemDataRole.UserRole + 9
    VendorRole = Qt.ItemDataRole.UserRole + 10
    RemovableRole = Qt.ItemDataRole.UserRole + 11
    DataRole = Qt.ItemDataRole.UserRole + 12

    countChanged = pyqtSignal()

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._devices: list[BlockDevice] = []

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.PathRole: b"devicePath",
            self.NameRole: b"deviceName",
            self.LabelRole: b"label",
            self.SizeRole: b"sizeText",
            self.FsRole: b"fstype",
            self.MountRole: b"mountPath",
            self.MountedRole: b"mounted",
            self.BusRole: b"transport",
            self.ModelRole: b"modelName",
            self.VendorRole: b"vendor",
            self.RemovableRole: b"hotplug",
            self.DataRole: b"raw",
        }

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return len(self._devices)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._devices)):
            return None
        device = self._devices[index.row()]
        mapping = {
            self.PathRole: device.path,
            self.NameRole: device.name,
            self.LabelRole: device.display_name,
            self.SizeRole: device.size_text,
            self.FsRole: device.fstype,
            self.MountRole: device.mount_path,
            self.MountedRole: device.mounted,
            self.BusRole: device.transport.upper(),
            self.ModelRole: device.model,
            self.VendorRole: device.vendor,
            self.RemovableRole: device.removable or device.hotplug,
            self.DataRole: device.to_dict(),
        }
        return mapping.get(role)

    @pyqtProperty(int, notify=countChanged)
    def count(self) -> int:
        return len(self._devices)

    def replace(self, devices: list[BlockDevice]) -> None:
        """Swap the whole list, but keep the selection when possible."""
        self.beginResetModel()
        self._devices = devices
        self.endResetModel()
        self.countChanged.emit()

    def device_at(self, row: int) -> BlockDevice | None:
        if 0 <= row < len(self._devices):
            return self._devices[row]
        return None


class StagesModel(QAbstractListModel):
    """The seven pipeline stages and their live progress."""

    IdRole = Qt.ItemDataRole.UserRole + 1
    TitleRole = Qt.ItemDataRole.UserRole + 2
    DescriptionRole = Qt.ItemDataRole.UserRole + 3
    IconRole = Qt.ItemDataRole.UserRole + 4
    StateRole = Qt.ItemDataRole.UserRole + 5
    PercentRole = Qt.ItemDataRole.UserRole + 6
    MessageRole = Qt.ItemDataRole.UserRole + 7
    CurrentRole = Qt.ItemDataRole.UserRole + 8
    ElapsedRole = Qt.ItemDataRole.UserRole + 9
    EtaRole = Qt.ItemDataRole.UserRole + 10
    RateRole = Qt.ItemDataRole.UserRole + 11
    ProcessedRole = Qt.ItemDataRole.UserRole + 12
    IndexRole = Qt.ItemDataRole.UserRole + 13

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = [
            {
                "id": str(stage),
                "title": STAGE_TITLES[stage],
                "description": STAGE_DESCRIPTIONS[stage],
                "icon": STAGE_ICONS[stage],
                "state": StageState.PENDING.value,
                "percent": 0.0,
                "message": "",
                "current": "",
                "elapsed": 0.0,
                "eta": -1.0,
                "rate": 0.0,
                "processed": 0.0,
                "index": index,
            }
            for index, stage in enumerate(STAGE_ORDER)
        ]

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.IdRole: b"stageId",
            self.TitleRole: b"title",
            self.DescriptionRole: b"description",
            self.IconRole: b"icon",
            self.StateRole: b"state",
            self.PercentRole: b"percent",
            self.MessageRole: b"message",
            self.CurrentRole: b"current",
            self.ElapsedRole: b"elapsed",
            self.EtaRole: b"eta",
            self.RateRole: b"rate",
            self.ProcessedRole: b"processed",
            self.IndexRole: b"stageIndex",
        }

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        mapping = {
            self.IdRole: row["id"],
            self.TitleRole: row["title"],
            self.DescriptionRole: row["description"],
            self.IconRole: row["icon"],
            self.StateRole: row["state"],
            self.PercentRole: row["percent"],
            self.MessageRole: row["message"],
            self.CurrentRole: row["current"],
            self.ElapsedRole: row["elapsed"],
            self.EtaRole: row["eta"],
            self.RateRole: row["rate"],
            self.ProcessedRole: row["processed"],
            self.IndexRole: row["index"],
        }
        return mapping.get(role)

    # -- mutations (GUI thread only) -------------------------------------
    def reset_stages(self) -> None:
        for row in self._rows:
            row.update(
                state=StageState.PENDING.value,
                percent=0.0,
                message="",
                current="",
                elapsed=0.0,
                eta=-1.0,
                rate=0.0,
                processed=0.0,
            )
        self._touch_all()

    def set_state(self, stage_id: str, state: str, message: str = "") -> None:
        for row in self._rows:
            if row["id"] != stage_id:
                continue
            row["state"] = state
            if message:
                row["message"] = message
            if state == StageState.RUNNING.value and row["percent"] <= 0.0:
                row["percent"] = 0.01
            if state == StageState.DONE.value:
                row["percent"] = 1.0
            self._touch(row)
            break

    def apply_progress(self, payload: dict[str, Any]) -> None:
        stage_id = str(payload.get("stage", ""))
        for row in self._rows:
            if row["id"] != stage_id:
                continue
            row["percent"] = float(payload.get("percent", 0.0))
            row["message"] = str(payload.get("message", ""))
            row["current"] = str(payload.get("current", ""))
            row["elapsed"] = float(payload.get("elapsed", 0.0))
            row["eta"] = float(payload.get("eta", -1.0))
            row["rate"] = float(payload.get("rate", 0.0))
            row["processed"] = float(payload.get("bytesProcessed", 0) or 0)
            if str(payload.get("state", "")) == StageState.RUNNING.value:
                row["state"] = StageState.RUNNING.value
            elif payload.get("state"):
                row["state"] = str(payload["state"])
            self._touch(row)
            break

    def _touch(self, row: dict[str, Any]) -> None:
        index = self.index(row["index"], 0)
        self.dataChanged.emit(index, index)

    def _touch_all(self) -> None:
        if self._rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, 0))


class FindingsModel(QAbstractListModel):
    """Findings, appended as the scan discovers them."""

    PathRole = Qt.ItemDataRole.UserRole + 1
    NameRole = Qt.ItemDataRole.UserRole + 2
    ThreatRole = Qt.ItemDataRole.UserRole + 3
    EngineRole = Qt.ItemDataRole.UserRole + 4
    SeverityRole = Qt.ItemDataRole.UserRole + 5
    SeverityRankRole = Qt.ItemDataRole.UserRole + 6
    CategoryRole = Qt.ItemDataRole.UserRole + 7
    SizeRole = Qt.ItemDataRole.UserRole + 8
    DetailRole = Qt.ItemDataRole.UserRole + 9
    ShaRole = Qt.ItemDataRole.UserRole + 10

    countChanged = pyqtSignal()
    severeFindingAdded = pyqtSignal(str, str)  # severity, threat

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.PathRole: b"path",
            self.NameRole: b"fileName",
            self.ThreatRole: b"threat",
            self.EngineRole: b"engine",
            self.SeverityRole: b"severity",
            self.SeverityRankRole: b"severityRank",
            self.CategoryRole: b"category",
            self.SizeRole: b"sizeText",
            self.DetailRole: b"detail",
            self.ShaRole: b"sha256",
        }

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        mapping = {
            self.PathRole: row["path"],
            self.NameRole: row["name"],
            self.ThreatRole: row["threat"],
            self.EngineRole: row["engine"],
            self.SeverityRole: row["severity"],
            self.SeverityRankRole: row["severityRank"],
            self.CategoryRole: row["category"],
            self.SizeRole: row["sizeText"],
            self.DetailRole: row["detail"],
            self.ShaRole: row["sha256"],
        }
        return mapping.get(role)

    @pyqtProperty(int, notify=countChanged)
    def count(self) -> int:
        return len(self._rows)

    @pyqtSlot()
    def clear(self) -> None:
        self.beginResetModel()
        self._rows = []
        self.endResetModel()
        self.countChanged.emit()

    @pyqtSlot(object)
    def append(self, payload: Any) -> None:
        # the slot accepts either a Finding or an already converted dict
        data = payload if isinstance(payload, dict) else payload.to_dict()
        row = {
            "path": data.get("path", ""),
            "name": data.get("name", ""),
            "threat": data.get("threat", ""),
            "engine": data.get("engine", ""),
            "severity": data.get("severity", "medium"),
            "severityRank": int(data.get("severityRank", 2)),
            "category": data.get("category", ""),
            "sizeText": data.get("sizeText", ""),
            "detail": data.get("detail", ""),
            "sha256": data.get("sha256", ""),
        }
        position = len(self._rows)
        self.beginInsertRows(QModelIndex(), position, position)
        self._rows.append(row)
        self.endInsertRows()
        self.countChanged.emit()
        if row["severityRank"] >= 3:
            self.severeFindingAdded.emit(row["severity"], row["threat"])


class TextLinesModel(QAbstractListModel):
    """A capped list of plain text lines, for streamed command output.

    A ListView over this model gives the installation dialog real scrolling and
    a scrollbar, and positionViewAtEnd() keeps the newest line visible, which is
    what somebody watching apt work actually wants to see.
    """

    TextRole = Qt.ItemDataRole.UserRole + 1

    countChanged = pyqtSignal()

    def __init__(self, parent: Any = None, *, max_lines: int = 4000) -> None:
        super().__init__(parent)
        self._rows: list[str] = []
        self.max_lines = max_lines

    def roleNames(self) -> dict[int, bytes]:
        return {self.TextRole: b"line"}

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        if role in (self.TextRole, Qt.ItemDataRole.DisplayRole):
            return self._rows[index.row()]
        return None

    @pyqtProperty(int, notify=countChanged)
    def count(self) -> int:
        return len(self._rows)

    @pyqtSlot(str)
    def append(self, text: str) -> None:
        if len(self._rows) >= self.max_lines:
            self.beginRemoveRows(QModelIndex(), 0, 0)
            self._rows.pop(0)
            self.endRemoveRows()
        position = len(self._rows)
        self.beginInsertRows(QModelIndex(), position, position)
        self._rows.append(text)
        self.endInsertRows()
        self.countChanged.emit()

    @pyqtSlot()
    def clear(self) -> None:
        self.beginResetModel()
        self._rows = []
        self.endResetModel()
        self.countChanged.emit()

    @pyqtSlot(result=str)
    def as_text(self) -> str:
        return "\n".join(self._rows)

    @pyqtProperty(str, notify=countChanged)
    def text(self) -> str:
        return "\n".join(self._rows)


class LogModel(QAbstractListModel):
    """The log tail shown in the collapsible pane."""

    TimeRole = Qt.ItemDataRole.UserRole + 1
    LevelRole = Qt.ItemDataRole.UserRole + 2
    MessageRole = Qt.ItemDataRole.UserRole + 3
    LoggerRole = Qt.ItemDataRole.UserRole + 4

    MAX_ROWS = 1000

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, str]] = []

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.TimeRole: b"time",
            self.LevelRole: b"level",
            self.MessageRole: b"message",
            self.LoggerRole: b"loggerName",
        }

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        return {
            self.TimeRole: row["time"],
            self.LevelRole: row["level"],
            self.MessageRole: row["message"],
            self.LoggerRole: row["logger"],
        }.get(role)

    @pyqtSlot(object)
    def append(self, record: Any) -> None:
        if isinstance(record, LogRecordView):
            row = {
                "time": record.time,
                "level": record.level,
                "message": record.message,
                "logger": record.logger,
            }
        else:
            row = dict(record)
        if len(self._rows) >= self.MAX_ROWS:
            self.beginRemoveRows(QModelIndex(), 0, 0)
            self._rows.pop(0)
            self.endRemoveRows()
        position = len(self._rows)
        self.beginInsertRows(QModelIndex(), position, position)
        self._rows.append(row)
        self.endInsertRows()

    @pyqtSlot()
    def clear(self) -> None:
        self.beginResetModel()
        self._rows = []
        self.endResetModel()

    @pyqtSlot(result=str)
    def as_text(self) -> str:
        return "\n".join(f"{row['time']} {row['level']:<7} {row['message']}" for row in self._rows)


class ThroughputModel(QAbstractListModel):
    """Recent progress samples, drawn as a compact bar chart."""

    PercentRole = Qt.ItemDataRole.UserRole + 1
    RateRole = Qt.ItemDataRole.UserRole + 2
    StageRole = Qt.ItemDataRole.UserRole + 3

    MAX_ROWS = 180
    changed = pyqtSignal()

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []
        self._peak = 1.0

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.PercentRole: b"percent",
            self.RateRole: b"rate",
            self.StageRole: b"stage",
        }

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        if role == self.RateRole:
            return row["rate"] / self._peak if self._peak else 0.0
        return {self.PercentRole: row["percent"], self.StageRole: row["stage"]}.get(role)

    @pyqtProperty(float, notify=changed)
    def peak(self) -> float:
        return self._peak

    @pyqtProperty(float, notify=changed)
    def latestRate(self) -> float:
        return self._rows[-1]["rate"] if self._rows else 0.0

    @pyqtSlot(object)
    def append(self, progress: Any) -> None:
        payload = progress if isinstance(progress, dict) else progress.to_dict()
        rate = float(payload.get("rate") or 0.0)
        self._peak = max(self._peak, rate, 1.0)
        self.beginInsertRows(QModelIndex(), len(self._rows), len(self._rows))
        self._rows.append(
            {
                "percent": float(payload.get("percent") or 0.0),
                "rate": rate,
                "stage": str(payload.get("stage", "")),
            }
        )
        self.endInsertRows()
        if len(self._rows) > self.MAX_ROWS:
            self.beginRemoveRows(QModelIndex(), 0, 0)
            self._rows.pop(0)
            self.endRemoveRows()
        self.changed.emit()

    @pyqtSlot()
    def clear(self) -> None:
        self.beginResetModel()
        self._rows = []
        self._peak = 1.0
        self.endResetModel()
        self.changed.emit()
