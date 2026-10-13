"""Report generation: the artefact a human actually reads.

The HTML report is fully self contained (inline CSS, no external requests) so
it can be attached to an email, opened from a USB stick or archived next to the
evidence.  A plain text version and a CSV of the findings are produced next to
it, plus a JSON dump for automation.
"""

from __future__ import annotations

import csv
import html
import io
import os
from dataclasses import dataclass
from typing import Any

from dcs.core.config import ReportConfig
from dcs.core.events import Severity, Verdict, severity_rank
from dcs.core.log import get_logger
from dcs.core.result import JobResult
from dcs.core.util import atomic_write_text, human_bytes, human_duration, timestamp_slug
from dcs.version import APP_HOMEPAGE, APP_LICENSE, APP_NAME, APP_SHORT, __version__

log = get_logger("report")

VERDICT_META: dict[Verdict, tuple[str, str, str]] = {
    # verdict: (headline, css class, one line explanation)
    Verdict.CLEAN: (
        "No threats found",
        "clean",
        "Every file could be inspected and nothing malicious was detected.",
    ),
    Verdict.SUSPICIOUS: (
        "Suspicious content found",
        "suspicious",
        "Nothing matched a malware signature, but several files behave like "
        "attack tooling. Treat the medium with care.",
    ),
    Verdict.INFECTED: (
        "Malware detected",
        "infected",
        "At least one file is malicious or is a physical-payload script. "
        "Do not plug this medium into a machine you care about.",
    ),
    Verdict.ERROR: (
        "Scan could not complete",
        "error",
        "The scan did not finish, so no statement about the medium is possible.",
    ),
    Verdict.CANCELLED: (
        "Job cancelled",
        "cancelled",
        "The run was stopped before the scan finished.",
    ),
}

STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px;
  font-family: "Noto Sans", "Inter", system-ui, -apple-system, sans-serif;
  background: #eff0f1; color: #232629; line-height: 1.5;
}
.wrap { max-width: 1040px; margin: 0 auto; }
header { display: flex; align-items: center; gap: 16px; margin-bottom: 24px; }
.logo {
  width: 56px; height: 56px; border-radius: 14px;
  background: linear-gradient(135deg, #3daee9, #1d7fb8);
  color: #fff; font-size: 26px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
h1 { font-size: 22px; margin: 0; }
h2 { font-size: 16px; margin: 32px 0 12px; color: #4d4d4d; text-transform: uppercase; letter-spacing: .06em; }
.sub { color: #7a7d80; font-size: 13px; }
.card { background: #fff; border-radius: 12px; padding: 20px 24px; box-shadow: 0 1px 3px rgba(0,0,0,.12); }
.verdict { border-left: 8px solid #27ae60; }
.verdict.clean { border-color: #27ae60; }
.verdict.suspicious { border-color: #f67400; }
.verdict.infected { border-color: #da4453; }
.verdict.error, .verdict.cancelled { border-color: #7a7d80; }
.verdict h1 { font-size: 26px; display: flex; align-items: center; gap: 12px; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.badge.clean { background: #d5f5e0; color: #116c3b; }
.badge.suspicious { background: #ffe9d0; color: #8a4b00; }
.badge.infected { background: #ffd9dc; color: #9b1c26; }
.badge.info { background: #e3e5e7; color: #4d4d4d; }
.badge.medium { background: #ffe9d0; color: #8a4b00; }
.badge.low { background: #e3e5e7; color: #4d4d4d; }
.badge.high { background: #ffd9dc; color: #9b1c26; }
.badge.critical { background: #da4453; color: #fff; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; }
.stat { background: #fff; border-radius: 10px; padding: 14px 16px; box-shadow: 0 1px 2px rgba(0,0,0,.08); }
.stat .label { font-size: 12px; color: #7a7d80; text-transform: uppercase; letter-spacing: .05em; }
.stat .value { font-size: 22px; font-weight: 600; margin-top: 4px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #e3e5e7; vertical-align: top; }
th { font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: #7a7d80; }
tr:last-child td { border-bottom: none; }
code, .path { font-family: "JetBrains Mono", "Fira Mono", monospace; font-size: 12.5px; word-break: break-all; }
.link { font-size: 15px; word-break: break-all; }
.link a { color: #1d7fb8; }
.note { background: #fdf6e3; border-left: 4px solid #f67400; padding: 10px 14px; border-radius: 6px; margin: 10px 0; font-size: 14px; }
.note.danger { background: #fdeaec; border-color: #da4453; }
.timeline { list-style: none; padding: 0; margin: 0; }
.timeline li { display: flex; gap: 12px; padding: 6px 0; border-bottom: 1px dashed #e3e5e7; font-size: 14px; }
.timeline li:last-child { border-bottom: none; }
.timeline .stage { width: 190px; color: #4d4d4d; }
.timeline .time { width: 90px; text-align: right; color: #7a7d80; font-variant-numeric: tabular-nums; }
footer { margin-top: 32px; color: #7a7d80; font-size: 12px; text-align: center; }
.mono { font-family: "JetBrains Mono", monospace; font-size: 12px; color: #4d4d4d; }
details summary { cursor: pointer; font-weight: 600; margin: 8px 0; }
@media (prefers-color-scheme: dark) {
  body { background: #1b1e20; color: #eff0f1; }
  .card, .stat { background: #232629; box-shadow: none; }
  h2 { color: #bdc3c7; }
  th, td { border-color: #31363b; }
  code, .path, .mono, .sub, .timeline .stage, .timeline .time { color: #bdc3c7; }
  .badge.info, .badge.low { background: #31363b; color: #cfd3d7; }
  .note { background: #2a2620; }
  .note.danger { background: #2c1f21; }
  .link a { color: #3daee9; }
}
"""


@dataclass
class ReportPaths:
    html: str = ""
    text: str = ""
    csv: str = ""
    json: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"html": self.html, "text": self.text, "csv": self.csv, "json": self.json}

    def existing(self) -> dict[str, str]:
        return {key: value for key, value in self.to_dict().items() if value and os.path.exists(value)}


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _severity_badge(severity: Severity) -> str:
    return f'<span class="badge {severity}">{_esc(severity.value)}</span>'


def _verdict_headline(result: JobResult) -> tuple[str, str, str]:
    return VERDICT_META.get(result.verdict, VERDICT_META[Verdict.ERROR])


def render_html(result: JobResult, config: ReportConfig | None = None) -> str:
    """Build the self contained HTML report."""
    config = config or ReportConfig()
    headline, css_class, explanation = _verdict_headline(result)
    scan = result.scan
    device = result.device or {}
    findings = result.findings_sorted[: config.max_findings]

    parts: list[str] = []
    add = parts.append

    add("<!DOCTYPE html>")
    add('<html lang="en"><head><meta charset="utf-8">')
    add(f"<title>{_esc(APP_SHORT)} report - {_esc(result.source_label or result.source_path)}</title>")
    add(f"<style>{STYLE}</style></head><body><div class=\"wrap\">")

    # header
    add('<header><div class="logo">DCS</div><div>')
    add(f"<h1>{_esc(APP_NAME)} - USB triage report</h1>")
    add(
        f'<div class="sub">Job {_esc(result.job_id)} · {_esc(result.started_at)} · '
        f"{_esc(human_duration(result.duration))} total · version {_esc(__version__)}</div>"
    )
    add("</div></header>")

    # verdict
    add(f'<div class="card verdict {css_class}">')
    add(f'<h1><span class="badge {css_class}">{_esc(headline)}</span></h1>')
    add(f"<p>{_esc(explanation)}</p>")
    if result.safe and result.share_link:
        add(
            '<p class="link">Safe copy available here: '
            f'<a href="{_esc(result.share_link)}">{_esc(result.share_link)}</a></p>'
        )
    elif not result.safe and result.threat_count:
        add(
            '<div class="note danger">The archive was <strong>not</strong> uploaded: '
            f"{result.threat_count} threatening file(s) were found. Isolate the medium and, if it "
            "came from an unknown source, do not insert it into another computer.</div>"
        )
    if result.upload_skipped_reason and result.safe:
        add(f'<div class="note">{_esc(result.upload_skipped_reason)}</div>')
    add("</div>")

    # headline numbers
    add("<h2>At a glance</h2><div class=\"grid\">")
    stats = [
        ("Files scanned", f"{result.files_scanned:,}"),
        ("Data scanned", human_bytes(result.bytes_scanned)),
        ("Findings", f"{len(result.findings):,}"),
        ("Threatening files", f"{result.threat_count:,}"),
        ("Archive", human_bytes(result.archive_bytes) if result.archive else "not created"),
        ("Scan duration", human_duration(scan.stats.duration if scan else 0)),
    ]
    for label, value in stats:
        add(f'<div class="stat"><div class="label">{_esc(label)}</div><div class="value">{_esc(value)}</div></div>')
    add("</div>")

    # device / source
    add("<h2>Medium</h2><div class=\"card\"><table>")
    rows = [
        ("Source", result.source_path),
        ("Label", result.source_label or device.get("label", "")),
        ("Device node", device.get("path", "")),
        ("Filesystem", f"{device.get('fstype', '')} {device.get('fsver', '')}".strip()),
        ("Size", human_bytes(device.get("size") or 0) if device.get("size") else ""),
        ("Bus", device.get("transport", "")),
        ("Model", " ".join(filter(None, [device.get("vendor", ""), device.get("model", "")]))),
        ("Serial/UUID", device.get("uuid", "")),
        ("Read only", "yes" if device.get("readOnly") else "no"),
    ]
    for label, value in rows:
        if value:
            add(f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>")
    add("</table></div>")

    # engines
    if scan and scan.engines:
        add("<h2>Engines</h2><div class=\"card\"><table>")
        add("<tr><th>Engine</th><th>Version</th><th>State</th><th>Notes</th></tr>")
        for status in scan.engines:
            state = "used" if status.available else "not available"
            notes = status.detail + ("; " + "; ".join(status.warnings) if status.warnings else "")
            add(
                f"<tr><td>{_esc(status.label)}</td><td>{_esc(status.version or '-')}</td>"
                f"<td>{_esc(state)}</td><td>{_esc(notes)}</td></tr>"
            )
        add("</table>")
        if scan.limited_coverage:
            add(
                '<div class="note">No antivirus engine (ClamAV) took part in this scan, so only the '
                "built-in heuristics ran. They catch behaviour, not signature-variants.</div>"
            )
        add("</div>")

    # findings
    add(f"<h2>Findings ({len(result.findings):,})</h2>")
    if not findings:
        add('<div class="card"><p>Nothing suspicious was found.</p></div>')
    else:
        add('<div class="card"><table><tr><th>Severity</th><th>File</th><th>Finding</th><th>Evidence</th></tr>')
        for finding in findings:
            try:
                severity = Severity(str(finding.severity))
            except ValueError:
                severity = Severity.MEDIUM
            add(
                f"<tr><td>{_severity_badge(severity)}</td>"
                f'<td class="path">{_esc(finding.path)}</td>'
                f"<td>{_esc(finding.threat)}<div class=\"sub\">{_esc(finding.engine)} · {_esc(finding.category)}"
                + (f" · {_esc(finding.sha256[:16])}…" if finding.sha256 else "")
                + "</div></td>"
                f'<td class="mono">{_esc(finding.detail)}</td></tr>'
            )
        add("</table>")
        if len(result.findings) > len(findings):
            add(f'<p class="sub">{len(result.findings) - len(findings)} further findings are in the CSV.</p>')
        add("</div>")

    # archive + upload
    add("<h2>Archive and share</h2><div class=\"card\"><table>")
    if result.archive and result.archive.ok:
        add(f"<tr><th>Archive</th><td class='path'>{_esc(result.archive.primary_path)}</td></tr>")
        add(f"<tr><th>Volumes</th><td>{len(result.archive.parts)}</td></tr>")
        add(
            f"<tr><th>Compressed</th><td>{_esc(human_bytes(result.archive.archive_bytes))} "
            f"from {_esc(human_bytes(result.archive.source_bytes))} "
            f"({result.archive.ratio:.0%})</td></tr>"
        )
        add(f"<tr><th>Tool</th><td>{_esc(result.archive.tool)}</td></tr>")
        if result.archive_sha256:
            add(
                f"<tr><th>SHA-256</th><td class='path'>{_esc(result.archive_sha256)}</td></tr>"
            )
    else:
        reason = result.archive.error if result.archive else result.upload_skipped_reason
        add(f"<tr><th>Archive</th><td>{_esc(reason or 'not created')}</td></tr>")

    if result.uploads:
        for upload in result.uploads:
            state = "uploaded" if upload.ok else f"failed: {upload.error}"
            link = f'<a href="{_esc(upload.link)}">{_esc(upload.link)}</a>' if upload.ok else ""
            add(
                f"<tr><th>{_esc(upload.provider)}</th><td class='path'>{_esc(os.path.basename(upload.path))} "
                f"- {_esc(state)}<br>{link}</td></tr>"
            )
    else:
        add(f"<tr><th>Upload</th><td>{_esc(result.upload_skipped_reason or 'not attempted')}</td></tr>")
    add("</table></div>")

    # email
    if result.email:
        add("<h2>Notification</h2><div class=\"card\"><table>")
        add(f"<tr><th>Recipients</th><td>{_esc(', '.join(result.email.recipients) or '-')}</td></tr>")
        add(f"<tr><th>Subject</th><td>{_esc(result.email.subject)}</td></tr>")
        add(
            f"<tr><th>Delivery</th><td>{_esc('dry run (not sent)' if result.email.dry_run else ('sent' if result.email.sent else 'failed'))}"
            + (f" - {_esc(result.email.error)}" if result.email.error else "")
            + "</td></tr>"
        )
        add("</table></div>")

    # timeline
    if result.timings:
        add("<h2>Timeline</h2><div class=\"card\"><ul class=\"timeline\">")
        for timing in result.timings:
            add(
                f'<li><span class="stage">{_esc(timing.stage)}</span>'
                f'<span class="time">{_esc(human_duration(timing.seconds))}</span>'
                f"<span>{_esc(timing.state)} - {_esc(timing.message)}</span></li>"
            )
        add("</ul></div>")

    # warnings and appendix
    if result.warnings:
        add("<h2>Notes</h2><div class=\"card\">")
        for warning in result.warnings:
            add(f'<div class="note">{_esc(warning)}</div>')
        add("</div>")

    if scan and (scan.unreadable or scan.skipped_large):
        add("<h2>Coverage gaps</h2><div class=\"card\">")
        if scan.skipped_large:
            add(f"<p>{len(scan.skipped_large)} file(s) were larger than the configured limit and were not scanned.</p>")
            add("<ul>")
            for path, size in scan.skipped_large[:10]:
                add(f'<li class="path">{_esc(path)} ({_esc(human_bytes(size))})</li>')
            add("</ul>")
        if scan.unreadable:
            add(f"<p>{len(scan.unreadable)} path(s) could not be read.</p>")
            add("<ul>")
            for item in scan.unreadable[:10]:
                add(f'<li class="path">{_esc(item)}</li>')
            add("</ul>")
        add("</div>")

    if scan is not None and scan.file_list:
        add(f"<h2>Files scanned ({len(scan.file_list):,})</h2><div class=\"card\"><details>")
        add("<summary>Show the list of files that were inspected</summary><ul>")
        for name in scan.file_list:
            add(f'<li class="path">{_esc(name)}</li>')
        add("</ul></details></div>")

    add("<footer>")
    add(
        f"{_esc(APP_NAME)} {_esc(__version__)} · {_esc(APP_LICENSE)} · "
        f'<a href="{_esc(APP_HOMEPAGE)}">{_esc(APP_HOMEPAGE)}</a><br>'
        "Generated locally: no file content ever leaves this machine except the archive you chose to upload."
    )
    add("</footer></div></body></html>")
    return "\n".join(parts)


def render_text(result: JobResult, config: ReportConfig | None = None) -> str:
    config = config or ReportConfig()
    headline, _, explanation = _verdict_headline(result)
    lines = [
        f"{APP_NAME} {__version__} - USB triage report",
        "=" * 68,
        f"Job          : {result.job_id}",
        f"Started      : {result.started_at}",
        f"Finished     : {result.finished_at}",
        f"Duration     : {human_duration(result.duration)}",
        f"Source       : {result.source_path}",
        f"Label        : {result.source_label or result.device.get('label', '')}",
        "",
        f"VERDICT      : {headline.upper()} [{result.verdict.value}]",
        explanation,
        "",
        f"Files scanned: {result.files_scanned:,}",
        f"Data scanned : {human_bytes(result.bytes_scanned)}",
        f"Findings     : {len(result.findings):,} ({result.threat_count} threatening)",
        f"Archive      : {human_bytes(result.archive_bytes) if result.archive and result.archive.ok else 'not created'}",
        f"Share link   : {result.share_link or (result.upload_skipped_reason or 'not available')}",
        "",
        "FINDINGS",
        "-" * 68,
    ]
    if not result.findings:
        lines.append("none")
    for finding in result.findings_sorted[: config.max_findings]:
        lines.append(f"[{str(finding.severity).upper():8}] {finding.threat}")
        lines.append(f"           {finding.path}")
        if finding.detail:
            lines.append(f"           {finding.detail}")
        if finding.sha256:
            lines.append(f"           sha256 {finding.sha256}")
    if result.scan is not None and result.scan.file_list:
        lines.append("")
        lines.append(f"FILES SCANNED ({len(result.scan.file_list):,})")
        lines.append("-" * 68)
        lines.extend(f"  {name}" for name in result.scan.file_list)
    lines.append("")
    lines.append("TIMELINE")
    lines.append("-" * 68)
    for timing in result.timings:
        lines.append(f"{timing.stage:16} {human_duration(timing.seconds):>8}  {timing.state:9} {timing.message}")
    if result.warnings:
        lines.append("")
        lines.append("NOTES")
        lines.append("-" * 68)
        lines.extend(f"- {warning}" for warning in result.warnings)
    lines.append("")
    lines.append(f"{APP_NAME} - {APP_LICENSE} - {APP_HOMEPAGE}")
    return "\n".join(lines)


def render_csv(result: JobResult) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["severity", "engine", "category", "threat", "path", "size", "sha256", "detail"])
    ordered = sorted(result.findings, key=lambda f: (-severity_rank(f.severity), f.path))
    for finding in ordered:
        writer.writerow(
            [
                str(finding.severity),
                finding.engine,
                finding.category,
                finding.threat,
                finding.path,
                finding.size,
                finding.sha256,
                finding.detail,
            ]
        )
    return buffer.getvalue()


def write_reports(
    result: JobResult,
    config: ReportConfig,
    directory: str,
    *,
    basename: str = "",
) -> ReportPaths:
    """Write every configured report file and return their paths."""
    paths = ReportPaths()
    if not config.enabled:
        return paths
    os.makedirs(directory, exist_ok=True)
    stem = basename or f"DCS_{result.source_label or 'usb'}_{timestamp_slug()}"
    stem = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in stem)

    try:
        paths.html = atomic_write_text(os.path.join(directory, f"{stem}.html"), render_html(result, config))
        paths.text = atomic_write_text(os.path.join(directory, f"{stem}.txt"), render_text(result, config))
        paths.csv = atomic_write_text(os.path.join(directory, f"{stem}.findings.csv"), render_csv(result))
        if config.write_json:
            import json

            paths.json = atomic_write_text(
                os.path.join(directory, f"{stem}.json"),
                json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
            )
    except OSError as exc:
        log.error("could not write the reports to %s: %s", directory, exc)
    return paths
