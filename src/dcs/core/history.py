"""The local index of finished jobs.

A finished job already leaves a complete :class:`~dcs.core.result.JobResult`
in memory and a set of report files on disk.  This module keeps the *index*:
one row per job with the numbers a person looks for a week later (when, which
medium, which verdict, how many files) plus the paths of the reports, and one
row per finding so that a file name or a hash can be searched for.

Deliberate limits
-----------------
* Everything stays on the local machine.  Nothing here is ever uploaded,
  emailed or looked up online.
* Report *paths* are stored, never copies of the report bodies: a CSV with
  every file name would double the size of the record of a sensitive medium.
* The passphrase of an encrypted archive is never part of a result, so it can
  never reach this database either.
* Labels and file names are sensitive, so the database is created with mode
  0600 under the user's data directory and the retention policy is a first
  class setting (`history.max_jobs`, `history.max_age_days`) rather than a
  hidden cleanup.
* The schema is versioned.  A newer release migrates an older database instead
  of deleting it; the version lives in the `meta` table.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dcs.core.config import HistoryConfig, default_data_dir
from dcs.core.log import get_logger
from dcs.core.result import JobResult
from dcs.core.util import human_bytes, human_duration

log = get_logger("history")

#: bump this when the schema changes, and add a migration step below
SCHEMA_VERSION = 1

#: the database file inside the data directory
DB_NAME = "history.db"


class HistoryError(RuntimeError):
    """The history database exists but cannot be used."""


# ---------------------------------------------------------------------------
# one row
# ---------------------------------------------------------------------------


@dataclass
class HistoryEntry:
    """A job as it is stored in the index."""

    job_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration: float = 0.0
    label: str = ""
    source_path: str = ""
    verdict: str = ""
    files_scanned: int = 0
    bytes_scanned: int = 0
    finding_count: int = 0
    threat_count: int = 0
    archive_path: str = ""
    archive_bytes: int = 0
    archive_sha256: str = ""
    archive_tool: str = ""
    archive_encrypted: bool = False
    share_link: str = ""
    upload_provider: str = ""
    upload_links: list[str] = field(default_factory=list)
    report_html: str = ""
    report_text: str = ""
    report_csv: str = ""
    report_json: str = ""
    engines: list[dict[str, Any]] = field(default_factory=list)
    app_version: str = ""
    recorded_at: str = ""
    #: filled in by JobHistory.get(), not by the list query
    findings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "jobId": self.job_id,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "duration": self.duration,
            "durationText": human_duration(self.duration),
            "label": self.label,
            "sourcePath": self.source_path,
            "verdict": self.verdict,
            "filesScanned": self.files_scanned,
            "bytesScanned": self.bytes_scanned,
            "bytesScannedText": human_bytes(self.bytes_scanned),
            "findingCount": self.finding_count,
            "threatCount": self.threat_count,
            "archivePath": self.archive_path,
            "archiveBytes": self.archive_bytes,
            "archiveBytesText": human_bytes(self.archive_bytes),
            "archiveSha256": self.archive_sha256,
            "archiveTool": self.archive_tool,
            "archiveEncrypted": self.archive_encrypted,
            "shareLink": self.share_link,
            "uploadProvider": self.upload_provider,
            "uploadLinks": list(self.upload_links),
            "reports": {
                "html": self.report_html,
                "text": self.report_text,
                "csv": self.report_csv,
                "json": self.report_json,
            },
            "engines": list(self.engines),
            "appVersion": self.app_version,
            "recordedAt": self.recorded_at,
            "findings": [dict(item) for item in self.findings],
        }


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id            TEXT PRIMARY KEY,
    started_at        TEXT NOT NULL DEFAULT '',
    finished_at       TEXT NOT NULL DEFAULT '',
    duration          REAL NOT NULL DEFAULT 0,
    label             TEXT NOT NULL DEFAULT '',
    source_path       TEXT NOT NULL DEFAULT '',
    verdict           TEXT NOT NULL DEFAULT '',
    files_scanned     INTEGER NOT NULL DEFAULT 0,
    bytes_scanned     INTEGER NOT NULL DEFAULT 0,
    finding_count     INTEGER NOT NULL DEFAULT 0,
    threat_count      INTEGER NOT NULL DEFAULT 0,
    archive_path      TEXT NOT NULL DEFAULT '',
    archive_bytes     INTEGER NOT NULL DEFAULT 0,
    archive_sha256    TEXT NOT NULL DEFAULT '',
    archive_tool      TEXT NOT NULL DEFAULT '',
    archive_encrypted INTEGER NOT NULL DEFAULT 0,
    share_link        TEXT NOT NULL DEFAULT '',
    upload_provider   TEXT NOT NULL DEFAULT '',
    upload_links      TEXT NOT NULL DEFAULT '[]',
    report_html       TEXT NOT NULL DEFAULT '',
    report_text       TEXT NOT NULL DEFAULT '',
    report_csv        TEXT NOT NULL DEFAULT '',
    report_json       TEXT NOT NULL DEFAULT '',
    engines           TEXT NOT NULL DEFAULT '[]',
    app_version       TEXT NOT NULL DEFAULT '',
    recorded_at       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS findings (
    job_id   TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    path     TEXT NOT NULL DEFAULT '',
    sha256   TEXT NOT NULL DEFAULT '',
    size     INTEGER NOT NULL DEFAULT 0,
    severity TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS jobs_started ON jobs(started_at DESC);
CREATE INDEX IF NOT EXISTS jobs_verdict ON jobs(verdict);
CREATE INDEX IF NOT EXISTS findings_job ON findings(job_id);
CREATE INDEX IF NOT EXISTS findings_path ON findings(path);
CREATE INDEX IF NOT EXISTS findings_sha ON findings(sha256);
"""

#: the columns of the jobs table, in the order _insert_job uses
_JOB_COLUMNS = (
    "job_id",
    "started_at",
    "finished_at",
    "duration",
    "label",
    "source_path",
    "verdict",
    "files_scanned",
    "bytes_scanned",
    "finding_count",
    "threat_count",
    "archive_path",
    "archive_bytes",
    "archive_sha256",
    "archive_tool",
    "archive_encrypted",
    "share_link",
    "upload_provider",
    "upload_links",
    "report_html",
    "report_text",
    "report_csv",
    "report_json",
    "engines",
    "app_version",
    "recorded_at",
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Create the schema, or bring an older one up to SCHEMA_VERSION."""
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    current = int(row[0]) if row else 0
    if current < SCHEMA_VERSION:
        # Version 1 is the first schema.  A future version adds its ALTER TABLE
        # steps here, guarded by `if current < N`, and never recreates a table
        # that already holds the user's jobs.
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


class JobHistory:
    """Reads and writes the local job index.

    Every method opens a connection when it needs one: SQLite connections are
    not shareable between the interface's worker thread and the CLI, and the
    index is touched once per job, so a pool would be more machinery than
    traffic.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        config: HistoryConfig | None = None,
    ) -> None:
        self.config = config or HistoryConfig()
        self._explicit = path is not None
        self.path = Path(path).expanduser() if path else default_data_dir() / DB_NAME

    # -- connection -------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=10.0)
        except (OSError, sqlite3.Error) as exc:
            raise HistoryError(f"cannot open the job history at {self.path}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 10000")
            _migrate(conn)
        except sqlite3.Error as exc:
            conn.close()
            raise HistoryError(f"the job history at {self.path} is not usable: {exc}") from exc
        # labels and file names are sensitive: keep the file to its owner
        with contextlib.suppress(OSError):  # pragma: no cover - exotic filesystems
            os.chmod(self.path, 0o600)
        return conn

    # -- writing ----------------------------------------------------------
    def record(self, result: JobResult) -> str:
        """Store (or replace) one job and its findings; returns the job id."""
        if not self.config.enabled:
            return ""
        if not result.job_id:
            return ""
        conn = self._connect()
        try:
            data = _fields_from_result(result)
            with conn:
                conn.execute(
                    f"INSERT OR REPLACE INTO jobs({', '.join(_JOB_COLUMNS)}) VALUES("
                    + ", ".join("?" for _ in _JOB_COLUMNS)
                    + ")",
                    tuple(data[column] for column in _JOB_COLUMNS),
                )
                conn.execute("DELETE FROM findings WHERE job_id = ?", (result.job_id,))
                conn.executemany(
                    "INSERT INTO findings(job_id, path, sha256, size, severity) VALUES(?,?,?,?,?)",
                    [(result.job_id, path, digest, size, severity) for path, digest, size, severity in _findings_from_result(result)],
                )
            # retention is part of writing: a store that only grows is a
            # liability when every row names a file somebody scanned
            self._prune(conn)
        finally:
            conn.close()
        return result.job_id

    def _prune(self, conn: sqlite3.Connection) -> int:
        removed = 0
        max_jobs = max(0, int(self.config.max_jobs or 0))
        if max_jobs:
            stale = conn.execute(
                "SELECT job_id FROM jobs ORDER BY started_at DESC, recorded_at DESC "
                "LIMIT -1 OFFSET ?",
                (max_jobs,),
            ).fetchall()
            with conn:
                for row in stale:
                    conn.execute("DELETE FROM jobs WHERE job_id = ?", (row["job_id"],))
            removed += len(stale)
        days = max(0, int(self.config.max_age_days or 0))
        if days:
            cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
            with conn:
                cursor = conn.execute(
                    "DELETE FROM jobs WHERE started_at != '' AND started_at < ?", (cutoff,)
                )
            removed += max(0, cursor.rowcount)
        if removed:
            log.info("history retention removed %d job(s)", removed)
        return removed

    def delete(self, job_id: str) -> bool:
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            return cursor.rowcount > 0
        finally:
            conn.close()

    def clear(self) -> int:
        """Remove every stored job; returns how many rows went away."""
        conn = self._connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            with conn:
                conn.execute("DELETE FROM jobs")
            return int(count)
        finally:
            conn.close()

    def prune(self) -> int:
        """Apply the retention policy now; returns how many jobs were dropped."""
        conn = self._connect()
        try:
            return self._prune(conn)
        finally:
            conn.close()

    # -- reading ----------------------------------------------------------
    def count(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        finally:
            conn.close()

    def list_jobs(
        self,
        query: str = "",
        *,
        verdict: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[HistoryEntry]:
        """Jobs newest first, optionally filtered by a search string.

        The search covers the label, the source path, the verdict, the job id,
        the archive hash, the share link, and the path or SHA-256 of any
        finding, which is what "find me the stick with invoice.pdf.exe on it"
        actually needs.
        """
        clauses: list[str] = []
        params: list[Any] = []
        text = (query or "").strip()
        if text:
            like = f"%{text}%"
            clauses.append(
                "("
                "label LIKE ? OR source_path LIKE ? OR share_link LIKE ? OR "
                "archive_sha256 LIKE ? OR job_id LIKE ? OR verdict LIKE ? OR "
                "EXISTS(SELECT 1 FROM findings f WHERE f.job_id = jobs.job_id "
                "AND (f.path LIKE ? OR f.sha256 LIKE ?))"
                ")"
            )
            params.extend([like] * 8)
        if verdict:
            clauses.append("verdict = ?")
            params.append(verdict)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs {where} "
            "ORDER BY started_at DESC, recorded_at DESC LIMIT ? OFFSET ?"
        )
        params.extend([max(1, int(limit)), max(0, int(offset))])
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [_entry_from_row(row) for row in rows]

    def get(self, job_id: str) -> HistoryEntry | None:
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {', '.join(_JOB_COLUMNS)} FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            entry = _entry_from_row(row)
            entry.findings = [
                {
                    "path": item["path"],
                    "sha256": item["sha256"],
                    "size": item["size"],
                    "severity": item["severity"],
                }
                for item in conn.execute(
                    "SELECT path, sha256, size, severity FROM findings "
                    "WHERE job_id = ? ORDER BY severity DESC, path",
                    (job_id,),
                ).fetchall()
            ]
            return entry
        finally:
            conn.close()

    # -- housekeeping -----------------------------------------------------
    def database_size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def remove_database(self) -> bool:
        """Delete the database (and its write-ahead log) entirely."""
        removed = False
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(str(self.path) + suffix)
                removed = removed or suffix == ""
            except FileNotFoundError:
                continue
            except OSError as exc:  # pragma: no cover - defensive
                log.warning("cannot delete %s%s: %s", self.path, suffix, exc)
        return removed


def open_history(config: Any) -> JobHistory:
    """The history store a configuration asks for."""
    stored = getattr(config, "history", None) or HistoryConfig()
    return JobHistory(getattr(config, "history_path", None) or None, config=stored)


# ---------------------------------------------------------------------------
# JobResult -> row
# ---------------------------------------------------------------------------


def _fields_from_result(result: JobResult) -> dict[str, Any]:
    archive = result.archive
    scan = result.scan
    engines = [
        {
            "id": status.engine_id,
            "label": status.label,
            "version": status.version,
            "available": bool(status.available),
        }
        for status in (scan.engines if scan else [])
    ]
    reports = result.report_paths or {}
    provider = next((upload.provider for upload in result.uploads if upload.provider), "")
    return {
        "job_id": result.job_id,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "duration": float(result.duration or 0.0),
        "label": result.source_label or "",
        "source_path": result.source_path or "",
        "verdict": result.verdict.value,
        "files_scanned": int(result.files_scanned),
        "bytes_scanned": int(result.bytes_scanned),
        "finding_count": len(result.findings),
        "threat_count": int(result.threat_count),
        "archive_path": archive.primary_path if archive and archive.ok else "",
        "archive_bytes": int(result.archive_bytes),
        "archive_sha256": result.archive_sha256 or "",
        "archive_tool": archive.tool if archive else "",
        "archive_encrypted": 1 if archive and archive.encrypted else 0,
        "share_link": result.share_link or "",
        "upload_provider": provider,
        "upload_links": json.dumps(result.upload_links, ensure_ascii=False),
        "report_html": reports.get("html", ""),
        "report_text": reports.get("text", ""),
        "report_csv": reports.get("csv", ""),
        "report_json": reports.get("json", ""),
        "engines": json.dumps(engines, ensure_ascii=False),
        "app_version": result.app_version or "",
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _findings_from_result(result: JobResult) -> list[tuple[str, str, int, str]]:
    rows: list[tuple[str, str, int, str]] = []
    for finding in result.findings:
        rows.append(
            (
                str(finding.path or ""),
                str(getattr(finding, "sha256", "") or ""),
                int(getattr(finding, "size", 0) or 0),
                str(finding.severity),
            )
        )
    return rows


def _entry_from_row(row: sqlite3.Row) -> HistoryEntry:
    def text(key: str) -> str:
        return str(row[key] or "")

    def number(key: str) -> int:
        try:
            return int(row[key] or 0)
        except (TypeError, ValueError):
            return 0

    def decoded(key: str, fallback: Any) -> Any:
        try:
            value = json.loads(row[key] or "")
        except (TypeError, ValueError):
            return fallback
        return value if isinstance(value, type(fallback)) else fallback

    return HistoryEntry(
        job_id=text("job_id"),
        started_at=text("started_at"),
        finished_at=text("finished_at"),
        duration=float(row["duration"] or 0.0),
        label=text("label"),
        source_path=text("source_path"),
        verdict=text("verdict"),
        files_scanned=number("files_scanned"),
        bytes_scanned=number("bytes_scanned"),
        finding_count=number("finding_count"),
        threat_count=number("threat_count"),
        archive_path=text("archive_path"),
        archive_bytes=number("archive_bytes"),
        archive_sha256=text("archive_sha256"),
        archive_tool=text("archive_tool"),
        archive_encrypted=bool(number("archive_encrypted")),
        share_link=text("share_link"),
        upload_provider=text("upload_provider"),
        upload_links=decoded("upload_links", []),
        report_html=text("report_html"),
        report_text=text("report_text"),
        report_csv=text("report_csv"),
        report_json=text("report_json"),
        engines=decoded("engines", []),
        app_version=text("app_version"),
        recorded_at=text("recorded_at"),
    )


__all__ = [
    "DB_NAME",
    "SCHEMA_VERSION",
    "HistoryEntry",
    "HistoryError",
    "JobHistory",
    "open_history",
]
