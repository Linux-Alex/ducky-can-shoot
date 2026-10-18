"""Email bodies.

The HTML is deliberately old fashioned (tables, inline styles) because mail
clients are not browsers: anything modern silently degrades, and this message
has to stay readable in a corporate Outlook.
"""

from __future__ import annotations

import html
from typing import Any

from dcs.core import branding
from dcs.core.config import EmailConfig
from dcs.core.events import Severity, Verdict, severity_rank
from dcs.core.result import JobResult
from dcs.core.util import human_bytes, human_duration
from dcs.version import APP_HOMEPAGE, APP_NAME, __version__

COLORS = {
    Verdict.CLEAN: ("#116c3b", "#d5f5e0", "#27ae60"),
    Verdict.SUSPICIOUS: ("#8a4b00", "#ffe9d0", "#f67400"),
    Verdict.INFECTED: ("#9b1c26", "#ffd9dc", "#da4453"),
    Verdict.ERROR: ("#4d4d4d", "#e3e5e7", "#7a7d80"),
    Verdict.CANCELLED: ("#4d4d4d", "#e3e5e7", "#7a7d80"),
}

SUBJECT_HEADS = {
    Verdict.CLEAN: "SAFE",
    Verdict.SUSPICIOUS: "SUSPICIOUS",
    Verdict.INFECTED: "INFECTED",
    Verdict.ERROR: "SCAN FAILED",
    Verdict.CANCELLED: "CANCELLED",
}


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def build_subject(config: EmailConfig, result: JobResult) -> str:
    values = {
        "verdict": SUBJECT_HEADS.get(result.verdict, "UNKNOWN"),
        "device": result.source_label or result.source_path,
        "files": result.files_scanned,
        "threats": result.threat_count,
        "job": result.job_id,
        "link": result.share_link,
    }
    template = config.subject_template or "[DCS] {verdict} - {device}"
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return f"[DCS] {values['verdict']} - {values['device']}"


def _finding_rows(result: JobResult, limit: int = 25) -> str:
    findings = list(result.findings_sorted)[:limit]
    if not findings:
        return '<tr><td style="padding:12px;color:#4d4d4d;">Nothing suspicious was found.</td></tr>'
    rows = []
    for finding in findings:
        try:
            severity = Severity(str(finding.severity))
        except ValueError:
            severity = Severity.MEDIUM
        colour = {
            Severity.CRITICAL: "#da4453",
            Severity.HIGH: "#da4453",
            Severity.MEDIUM: "#f67400",
            Severity.LOW: "#7a7d80",
            Severity.INFO: "#7a7d80",
        }[severity]
        rows.append(
            "<tr>"
            f'<td style="padding:8px;border-bottom:1px solid #e3e5e7;color:{colour};font-weight:600;'
            f'text-transform:uppercase;font-size:12px;">{_esc(severity.value)}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #e3e5e7;font-family:monospace;font-size:12px;'
            f'word-break:break-all;">{_esc(finding.path)}<br>'
            f'<span style="color:#7a7d80;">{_esc(finding.threat)}</span></td>'
            "</tr>"
        )
    if len(result.findings) > limit:
        rows.append(
            f'<tr><td colspan="2" style="padding:8px;color:#7a7d80;font-size:12px;">'
            f"+ {len(result.findings) - limit} more findings in the attached report</td></tr>"
        )
    return "".join(rows)


def _brand_mark() -> str:
    """The icon, inline, for the header of the message.

    A mail client never loads a picture from our disk, so the image travels with
    the message as a related part (see Notifier.build_message) and this tag
    points at its Content-ID.  Without the icon the header simply has none.
    """
    if not branding.icon_png():
        return ""
    return (
        f'<img src="cid:{branding.CONTENT_ID}" width="26" height="26" alt="" '
        'style="vertical-align:-7px;margin-right:9px;border-radius:6px;">'
    )


def render_email_html(result: JobResult) -> str:
    text_colour, background, accent = COLORS.get(result.verdict, COLORS[Verdict.ERROR])
    head = SUBJECT_HEADS.get(result.verdict, "UNKNOWN")
    device = result.source_label or result.source_path

    intro = {
        Verdict.CLEAN: "The medium was scanned and nothing malicious was found.",
        Verdict.SUSPICIOUS: "The medium carries files that behave like attack tooling. Treat it with care.",
        Verdict.INFECTED: "The medium is compromised. Do not insert it into another computer.",
        Verdict.ERROR: "The scan could not be completed, so the medium cannot be cleared.",
        Verdict.CANCELLED: "The scan was cancelled before it finished.",
    }.get(result.verdict, "")

    link_block = ""
    if result.share_link:
        link_block = (
            '<tr><td style="padding:16px 24px 0 24px;">'
            '<div style="border:1px solid #e3e5e7;border-radius:8px;padding:14px;">'
            '<div style="font-size:12px;color:#7a7d80;text-transform:uppercase;letter-spacing:.05em;">'
            "Archive download</div>"
            f'<div style="margin-top:6px;word-break:break-all;"><a href="{_esc(result.share_link)}" '
            f'style="color:#1d7fb8;font-size:15px;">{_esc(result.share_link)}</a></div>'
            '<div style="margin-top:8px;font-size:12px;color:#7a7d80;">'
            "Anyone with this link can download the archive. The host deletes it automatically after "
            "its retention period.</div></div></td></tr>"
        )
    elif result.upload_skipped_reason:
        link_block = (
            '<tr><td style="padding:16px 24px 0 24px;">'
            f'<div style="border-left:4px solid {accent};background:#fdf6e3;padding:10px 14px;'
            f'border-radius:6px;font-size:13px;">{_esc(result.upload_skipped_reason)}</div></td></tr>'
        )

    warnings = ""
    if result.warnings:
        items = "".join(f"<li>{_esc(w)}</li>" for w in result.warnings[:6])
        warnings = (
            '<tr><td style="padding:16px 24px 0 24px;">'
            '<div style="font-size:13px;color:#4d4d4d;"><strong>Notes</strong>'
            f'<ul style="margin:6px 0 0 18px;padding:0;">{items}</ul></div></td></tr>'
        )

    rows = [
        ("Files scanned", f"{result.files_scanned:,}"),
        ("Data scanned", human_bytes(result.bytes_scanned)),
        ("Threatening files", f"{result.threat_count:,}"),
        ("Findings", f"{len(result.findings):,}"),
        ("Archive", human_bytes(result.archive_bytes) if result.archive and result.archive.ok else "not created"),
        ("Duration", human_duration(result.duration)),
    ]
    stat_rows = "".join(
        '<tr>'
        f'<td style="padding:4px 0;color:#7a7d80;font-size:13px;">{_esc(label)}</td>'
        f'<td style="padding:4px 0;text-align:right;font-weight:600;">{_esc(value)}</td>'
        "</tr>"
        for label, value in rows
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;background:#eff0f1;font-family:'Noto Sans',Arial,sans-serif;color:#232629;">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:680px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;">
  <tr><td style="background:{accent};padding:18px 24px;color:#ffffff;">
    <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;opacity:.85;">{_brand_mark()}{_esc(APP_NAME)}</div>
    <div style="font-size:22px;font-weight:700;margin-top:4px;">{_esc(head)}</div>
    <div style="font-size:13px;opacity:.9;margin-top:2px;">{_esc(device)}</div>
  </td></tr>
  <tr><td style="padding:18px 24px 0 24px;">
    <p style="margin:0;font-size:14px;">{_esc(intro)}</p>
    <div style="margin-top:12px;background:{background};color:{text_colour};border-radius:8px;padding:10px 14px;font-size:13px;">
      Verdict: <strong>{_esc(result.verdict.value)}</strong> · job {_esc(result.job_id)} · {_esc(result.finished_at)}
    </div>
  </td></tr>
  {link_block}
  <tr><td style="padding:16px 24px 0 24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{stat_rows}</table>
  </td></tr>
  <tr><td style="padding:16px 24px 0 24px;">
    <div style="font-size:12px;color:#7a7d80;text-transform:uppercase;letter-spacing:.05em;">Findings</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;">{_finding_rows(result)}</table>
  </td></tr>
  {warnings}
  <tr><td style="padding:20px 24px;color:#7a7d80;font-size:12px;border-top:1px solid #e3e5e7;margin-top:16px;">
    {_esc(APP_NAME)} {_esc(__version__)} · <a href="{_esc(APP_HOMEPAGE)}" style="color:#1d7fb8;">{_esc(APP_HOMEPAGE)}</a><br>
    The scan ran locally on the machine that owns the medium; only the archive you see above was uploaded.
  </td></tr>
</table>
</body></html>"""


def render_email_text(result: JobResult) -> str:
    head = SUBJECT_HEADS.get(result.verdict, "UNKNOWN")
    lines = [
        f"{APP_NAME} - {head}",
        "=" * 60,
        f"Medium       : {result.source_label or result.source_path}",
        f"Verdict      : {result.verdict.value}",
        f"Files scanned: {result.files_scanned:,} ({human_bytes(result.bytes_scanned)})",
        f"Threats      : {result.threat_count:,}   Findings: {len(result.findings):,}",
        f"Duration     : {human_duration(result.duration)}",
        f"Job          : {result.job_id}",
    ]
    if result.share_link:
        lines += ["", f"Archive download: {result.share_link}",
                  "Anyone with this link can download the archive."]
    elif result.upload_skipped_reason:
        lines += ["", f"Not uploaded: {result.upload_skipped_reason}"]
    if result.findings:
        lines += ["", "Top findings", "-" * 60]
        for finding in result.findings_sorted[:25]:
            if severity_rank(finding.severity) < severity_rank(Severity.LOW):
                continue
            lines.append(f"[{str(finding.severity).upper():8}] {finding.threat}")
            lines.append(f"           {finding.path}")
    if result.warnings:
        lines += ["", "Notes", "-" * 60]
        lines += [f"- {warning}" for warning in result.warnings]
    lines += ["", f"{APP_NAME} - {APP_HOMEPAGE}"]
    return "\n".join(lines)
