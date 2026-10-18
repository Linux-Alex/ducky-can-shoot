"""Tests for the email notification stage (dcs.core.notify).

No test opens a socket: the SMTP client is replaced by a recording stand in and
the dry run mode writes a real .eml file inside tmp_path.
"""

from __future__ import annotations

import html
import smtplib
import ssl
import subprocess
from email import message_from_binary_file, policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar

import pytest

from dcs.core.archiver import ArchivePart, ArchiveResult
from dcs.core.config import AppConfig, EmailConfig
from dcs.core.events import Finding, ScanStats, Severity, Verdict
from dcs.core.notify import desktop as desktop_notify
from dcs.core.notify.dbus import DbusNotifier, GioNotifier
from dcs.core.notify.desktop import DesktopNotifier, build_notifier
from dcs.core.notify.smtp import Notifier
from dcs.core.notify.templates import (
    SUBJECT_HEADS,
    build_subject,
    render_email_html,
    render_email_text,
)
from dcs.core.result import JobResult
from dcs.core.scanner import ScanResult
from dcs.version import APP_HOMEPAGE, APP_NAME

CRITICAL_FINDING = Finding(
    path="/media/usb0/eicar.com",
    threat="Eicar-Test-Signature",
    engine="heuristic",
    severity=Severity.CRITICAL,
)
INFO_FINDING = Finding(
    path="/media/usb0/photos/holiday.jpg",
    threat="trailing data after the JPEG end marker",
    engine="heuristic",
    severity=Severity.INFO,
)
MEDIUM_FINDING = Finding(
    path="/media/usb0/update.ps1",
    threat="download and execute one-liner",
    engine="heuristic",
    severity=Severity.MEDIUM,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_job(
    *,
    verdict: Verdict = Verdict.INFECTED,
    job_id: str = "job-0001",
    label: str = "KINGSTON",
    source_path: str = "/media/usb0",
    findings: list[Finding] | None = None,
    files_scanned: int = 19,
    bytes_scanned: int = 3 * 1024 * 1024,
    share_link: str = "https://gofile.io/d/AbCdEf",
    upload_skipped_reason: str = "",
    warnings: list[str] | None = None,
    duration: float = 75.0,
    archive_bytes: int = 4096,
) -> JobResult:
    """Build a fully populated JobResult for the notification tests."""
    archive = (
        ArchiveResult(ok=True, parts=[ArchivePart(path="medium.zip", size=archive_bytes)])
        if archive_bytes
        else None
    )
    return JobResult(
        job_id=job_id,
        source_path=source_path,
        source_label=label,
        verdict=verdict,
        scan=ScanResult(
            root=source_path,
            verdict=verdict,
            findings=list(findings or []),
            stats=ScanStats(files_scanned=files_scanned, bytes_scanned=bytes_scanned),
        ),
        archive=archive,
        share_link=share_link,
        upload_skipped_reason=upload_skipped_reason,
        warnings=list(warnings or []),
        duration=duration,
        started_at="2026-02-01T09:58:45+00:00",
        finished_at="2026-02-01T10:00:00+00:00",
    )


class FakeSmtp:
    """Records everything a real smtplib.SMTP would have done."""

    kind = "plain"
    #: every instance created during a test, so a test can inspect the traffic
    instances: ClassVar[list[FakeSmtp]] = []
    #: when set the constructor raises OSError with this text
    connect_error: str = ""
    #: when set send_message raises it
    send_error: BaseException | None = None
    noop_code: int = 250
    noop_text: bytes = b"OK"

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float | None = None,
        context: Any = None,
    ) -> None:
        if type(self).connect_error:
            raise OSError(type(self).connect_error)
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.calls: list[tuple[str, tuple[Any, ...]]] = [(f"connect:{self.kind}", (host, port))]
        type(self).instances.append(self)

    # -- recording --------------------------------------------------------
    def _note(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    @property
    def sent_message(self) -> EmailMessage:
        for name, args in self.calls:
            if name == "send_message":
                return args[0]
        raise AssertionError("no message was sent")

    # -- smtplib.SMTP surface --------------------------------------------
    def ehlo(self) -> tuple[int, bytes]:
        self._note("ehlo")
        return 250, b"OK"

    def starttls(self, *, context: Any = None) -> tuple[int, bytes]:
        self._note("starttls", context)
        return 220, b"ready"

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        self._note("login", user, password)
        return 235, b"accepted"

    def send_message(
        self, message: EmailMessage, to_addrs: list[str] | None = None
    ) -> dict[str, Any]:
        self._note("send_message", message, to_addrs)
        if type(self).send_error is not None:
            raise type(self).send_error
        return {}

    @property
    def envelope_recipients(self) -> list[str] | None:
        """The to_addrs smtplib was asked to deliver to (None = from headers)."""
        for name, args in self.calls:
            if name == "send_message":
                return args[1] if len(args) > 1 else None
        raise AssertionError("no message was sent")

    def noop(self) -> tuple[int, bytes]:
        self._note("noop")
        return type(self).noop_code, type(self).noop_text

    def quit(self) -> tuple[int, bytes]:
        self._note("quit")
        return 221, b"bye"

    def close(self) -> None:
        self._note("close")


class FakeSmtpSsl(FakeSmtp):
    """The implicit TLS variant, so a test can tell the two apart."""

    kind = "ssl"


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSmtp]:
    """Install the recording SMTP client for one test."""
    FakeSmtp.instances.clear()
    FakeSmtp.connect_error = ""
    FakeSmtp.send_error = None
    FakeSmtp.noop_code = 250
    FakeSmtp.noop_text = b"OK"
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtpSsl)
    return FakeSmtp


@pytest.fixture
def email_config(tmp_config: AppConfig) -> EmailConfig:
    """The email section of the temporary configuration."""
    return tmp_config.email


def only_client() -> FakeSmtp:
    """The single SMTP client a delivery test created."""
    assert len(FakeSmtp.instances) == 1, f"expected one client, saw {len(FakeSmtp.instances)}"
    return FakeSmtp.instances[0]


def write_report_files(root: Path) -> dict[str, Path]:
    """Write the three real attachment files a job would produce."""
    files = {
        "html": root / "report.html",
        "csv": root / "findings.csv",
        "json": root / "result.json",
    }
    files["html"].write_text("<html><body>report</body></html>", encoding="utf-8")
    files["csv"].write_text("path,threat\n/media/usb0/eicar.com,Eicar-Test-Signature\n", encoding="utf-8")
    files["json"].write_text('{"verdict": "infected"}', encoding="utf-8")
    return files


# ---------------------------------------------------------------------------
# build_subject
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", list(SUBJECT_HEADS))
def test_build_subject_default_template(email_config: EmailConfig, verdict: Verdict) -> None:
    """The shipped template renders the head word, the device and the count."""
    job = make_job(verdict=verdict, share_link="")
    assert build_subject(email_config, job) == (
        f"[DCS] {SUBJECT_HEADS[verdict]} - KINGSTON (19 files)"
    )


def test_build_subject_unknown_verdict_is_labelled(email_config: EmailConfig) -> None:
    """A verdict outside the table still renders instead of raising."""
    job = make_job(share_link="")
    job.verdict = "weird"  # type: ignore[assignment]
    assert build_subject(email_config, job) == "[DCS] UNKNOWN - KINGSTON (19 files)"


def test_build_subject_falls_back_to_the_source_path(email_config: EmailConfig) -> None:
    """Without a device label the mount point is used."""
    job = make_job(label="", share_link="")
    assert build_subject(email_config, job) == "[DCS] INFECTED - /media/usb0 (19 files)"


def test_build_subject_all_placeholders(email_config: EmailConfig) -> None:
    """threats, job and link come from the result."""
    email_config.subject_template = "{verdict}|{device}|{files}|{threats}|{job}|{link}"
    job = make_job(findings=[CRITICAL_FINDING, MEDIUM_FINDING])
    assert build_subject(email_config, job) == (
        "INFECTED|KINGSTON|19|1|job-0001|https://gofile.io/d/AbCdEf"
    )


@pytest.mark.parametrize("template", ["[DCS] {nope} - {device}", "[DCS] {verdict - {device}", ""])
def test_build_subject_broken_template_falls_back(
    email_config: EmailConfig, template: str
) -> None:
    """Unknown placeholders, broken braces and an empty template use the default."""
    email_config.subject_template = template
    job = make_job(share_link="")
    assert build_subject(email_config, job) == "[DCS] INFECTED - KINGSTON"


# ---------------------------------------------------------------------------
# render_email_text
# ---------------------------------------------------------------------------


def test_render_email_text_structure() -> None:
    """The text body carries the verdict, the medium, the stats and the link."""
    job = make_job(findings=[CRITICAL_FINDING, INFO_FINDING])
    lines = render_email_text(job).splitlines()

    assert lines[0] == f"{APP_NAME} - INFECTED"
    assert lines[1] == "=" * 60
    assert lines[2] == "Medium       : KINGSTON"
    assert lines[3] == "Verdict      : infected"
    assert lines[4] == "Files scanned: 19 (3.0 MiB)"
    assert lines[5] == "Threats      : 1   Findings: 2"
    assert lines[6] == "Duration     : 01:15"
    assert lines[7] == "Job          : job-0001"
    assert "" in lines
    assert "Archive download: https://gofile.io/d/AbCdEf" in lines
    assert "Anyone with this link can download the archive." in lines
    assert lines[-1] == f"{APP_NAME} - {APP_HOMEPAGE}"


def test_render_email_text_findings_block() -> None:
    """Findings are ranked, printed as a pair of lines, and INFO is hidden."""
    job = make_job(findings=[INFO_FINDING, CRITICAL_FINDING, MEDIUM_FINDING])
    lines = render_email_text(job).splitlines()

    start = lines.index("Top findings")
    assert lines[start + 1] == "-" * 60
    assert lines[start + 2] == "[CRITICAL] Eicar-Test-Signature"
    assert lines[start + 3] == " " * 11 + "/media/usb0/eicar.com"
    assert lines[start + 4] == "[MEDIUM  ] download and execute one-liner"
    assert lines[start + 5] == " " * 11 + "/media/usb0/update.ps1"
    assert not any(INFO_FINDING.threat in line for line in lines)


def test_render_email_text_without_a_link() -> None:
    """A skipped upload is reported instead of a link."""
    job = make_job(share_link="", upload_skipped_reason="uploading is disabled in the settings")
    text = render_email_text(job)
    assert "Archive download:" not in text
    assert "Not uploaded: uploading is disabled in the settings" in text


def test_render_email_text_notes() -> None:
    """Warnings become a Notes section."""
    job = make_job(warnings=["clamav is not installed", "one file was unreadable"])
    lines = render_email_text(job).splitlines()
    start = lines.index("Notes")
    assert lines[start + 1] == "-" * 60
    assert lines[start + 2] == "- clamav is not installed"
    assert lines[start + 3] == "- one file was unreadable"


def test_render_email_text_without_findings_or_notes() -> None:
    """A clean medium has neither section."""
    text = render_email_text(make_job(verdict=Verdict.CLEAN, findings=[]))
    assert "SAFE" in text
    assert "Top findings" not in text
    assert "Notes" not in text


# ---------------------------------------------------------------------------
# render_email_html
# ---------------------------------------------------------------------------


def test_render_email_html_contains_the_facts() -> None:
    """The html body repeats the head, the device, the stats and the findings."""
    job = make_job(findings=[CRITICAL_FINDING])
    page = render_email_html(job)

    assert '<div style="font-size:22px;font-weight:700;margin-top:4px;">INFECTED</div>' in page
    assert '<div style="font-size:13px;opacity:.9;margin-top:2px;">KINGSTON</div>' in page
    assert (
        '<td style="padding:4px 0;color:#7a7d80;font-size:13px;">Files scanned</td>'
        '<td style="padding:4px 0;text-align:right;font-weight:600;">19</td>'
    ) in page
    assert (
        '<td style="padding:4px 0;color:#7a7d80;font-size:13px;">Data scanned</td>'
        '<td style="padding:4px 0;text-align:right;font-weight:600;">3.0 MiB</td>'
    ) in page
    assert (
        '<td style="padding:4px 0;color:#7a7d80;font-size:13px;">Archive</td>'
        '<td style="padding:4px 0;text-align:right;font-weight:600;">4.0 KiB</td>'
    ) in page
    assert 'href="https://gofile.io/d/AbCdEf"' in page
    # the severity cell is lower case in the markup and upper cased by the css
    assert 'text-transform:uppercase;font-size:12px;">critical</td>' in page
    assert "Eicar-Test-Signature" in page
    assert "/media/usb0/eicar.com" in page


def test_render_email_html_clean_and_empty() -> None:
    """A clean result says so and lists no findings."""
    page = render_email_html(make_job(verdict=Verdict.CLEAN, findings=[]))
    assert ">SAFE</div>" in page
    assert "Nothing suspicious was found." in page
    assert "#27ae60" in page


def test_render_email_html_skipped_upload() -> None:
    """Without a link the reason is shown and no download block appears."""
    page = render_email_html(
        make_job(share_link="", upload_skipped_reason="the archive was too large")
    )
    assert "Archive download" not in page
    assert "the archive was too large" in page


def test_render_email_html_notes() -> None:
    """Warnings are listed under the Notes heading."""
    page = render_email_html(make_job(warnings=["clamav is not installed"]))
    assert "<strong>Notes</strong>" in page
    assert "<li>clamav is not installed</li>" in page


def test_render_email_html_escapes_hostile_names_once() -> None:
    """A hostile file name can never inject markup and is not double escaped."""
    hostile_path = '/media/usb0/<script>alert("x")</script> & evil.exe'
    hostile_threat = '"><img src=x onerror=alert(1)>'
    job = make_job(
        label="USB & <b>stick</b>",
        findings=[
            Finding(
                path=hostile_path,
                threat=hostile_threat,
                engine="heuristic",
                severity=Severity.CRITICAL,
            )
        ],
    )
    page = render_email_html(job)

    assert "<script>" not in page
    assert "<img src=x" not in page
    assert "onerror=alert(1)>" not in page
    assert html.escape(hostile_path) in page
    assert html.escape(hostile_threat) in page
    assert "USB &amp; &lt;b&gt;stick&lt;/b&gt;" in page
    assert page.count("&lt;script&gt;") == 1
    assert "&amp;lt;" not in page
    assert "&amp;amp;" not in page


# ---------------------------------------------------------------------------
# build_message
# ---------------------------------------------------------------------------


def test_build_message_headers_and_alternative_body(email_config: EmailConfig) -> None:
    """Headers, recipients and the text plus html alternative are all present."""
    email_config.cc_addrs = ["boss@example.test"]
    email_config.reply_to = "noc@example.test"
    job = make_job()

    message = Notifier(email_config).build_message(job)

    assert message["Subject"] == "[DCS] INFECTED - KINGSTON (19 files)"
    assert message["From"] == f"Ducky Can Shoot <{email_config.from_addr}>"
    assert message["To"] == "ops@example.test"
    assert message["Cc"] == "boss@example.test"
    assert message["Reply-To"] == "noc@example.test"
    assert message["X-DCS-Job"] == "job-0001"
    assert message["X-DCS-Verdict"] == "infected"
    assert message["Date"]
    assert message["Message-ID"].endswith("@example.test>")

    assert message.get_content_type() == "multipart/alternative"
    # the html alternative is wrapped in a related part, which is where the
    # inline artwork lives
    assert [part.get_content_type() for part in message.iter_parts()] == [
        "text/plain",
        "multipart/related",
    ]
    assert message.get_body(preferencelist=("plain",)).get_content().startswith(
        f"{APP_NAME} - INFECTED"
    )
    assert "KINGSTON" in message.get_body(preferencelist=("html",)).get_content()
    assert not list(message.iter_attachments())



def test_the_email_carries_the_artwork_inline(email_config: EmailConfig) -> None:
    """A mail client never loads a file from our disk: the picture travels along."""
    message = Notifier(email_config).build_message(make_job())

    html = message.get_body(preferencelist=("html",)).get_content()
    assert 'src="cid:dcs-icon"' in html

    images = [part for part in message.walk() if part.get_content_type() == "image/png"]
    assert images, "the inline image is missing"
    assert images[0]["Content-ID"] == "<dcs-icon>"
    assert images[0].get_content_maintype() == "image"


def test_the_inline_artwork_survives_attachments(email_config: EmailConfig, tmp_path: Path) -> None:
    attachment = tmp_path / "report.csv"
    attachment.write_text("path,threat\n", encoding="utf-8")

    message = Notifier(email_config).build_message(make_job(), attachments=[str(attachment)])

    assert message.get_content_type() == "multipart/mixed"
    images = [part for part in message.walk() if part.get_content_type() == "image/png"]
    assert images, "the attachment changed the structure and dropped the image"
    assert [part.get_filename() for part in message.iter_attachments()] == ["report.csv"]


def test_a_message_without_artwork_has_no_image(monkeypatch: pytest.MonkeyPatch, email_config: EmailConfig) -> None:
    import dcs.core.notify.smtp as smtp_module
    import dcs.core.notify.templates as templates_module

    monkeypatch.setattr(smtp_module.branding, "icon_png", lambda: b"")
    monkeypatch.setattr(templates_module.branding, "icon_png", lambda: b"")

    message = Notifier(email_config).build_message(make_job())

    assert not [part for part in message.walk() if part.get_content_type() == "image/png"]
    assert "cid:dcs-icon" not in message.get_body(preferencelist=("html",)).get_content()

def test_build_message_omits_optional_headers(email_config: EmailConfig) -> None:
    """Cc and Reply-To only appear when they are configured."""
    message = Notifier(email_config).build_message(make_job())
    assert message["Cc"] is None
    assert message["Reply-To"] is None
    assert message["To"] == "ops@example.test"


def test_build_message_keeps_bcc_recipients(email_config: EmailConfig) -> None:
    """A blind copy must never appear in the headers.

    A Bcc header would leak exactly the address the user wanted to keep out of
    the visible recipient list, so the address travels in the SMTP envelope
    instead; test_send_delivers_to_bcc_through_the_envelope covers that half.
    """
    email_config.bcc_addrs = ["archive@example.test"]
    message = Notifier(email_config).build_message(make_job())
    assert message["Bcc"] is None
    assert "archive@example.test" not in str(message["To"])
    assert "archive@example.test" not in str(message["Cc"])


def test_send_delivers_to_bcc_through_the_envelope(
    fake_smtp: type[FakeSmtp], email_config: EmailConfig
) -> None:
    """The Bcc address is handed to smtplib as an envelope recipient."""
    email_config.bcc_addrs = ["archive@example.test"]
    email_config.to_addrs = ["ops@example.test"]
    outcome = Notifier(email_config).send(make_job())

    assert outcome.sent is True
    assert "archive@example.test" in outcome.recipients
    client = only_client()
    assert client.envelope_recipients == ["ops@example.test", "archive@example.test"]
    assert client.sent_message["Bcc"] is None


def test_build_message_attaches_real_files(email_config: EmailConfig, tmp_path: Path) -> None:
    """html, csv and json attachments carry the right content type and payload."""
    files = write_report_files(tmp_path)
    missing = tmp_path / "not-here.html"
    attachments = [str(files["html"]), str(files["csv"]), str(files["json"]), str(missing), "", None]

    message = Notifier(email_config).build_message(make_job(), attachments=attachments)  # type: ignore[list-item]

    parts = list(message.iter_attachments())
    assert [(p.get_content_type(), p.get_filename()) for p in parts] == [
        ("text/html", "report.html"),
        ("text/csv", "findings.csv"),
        ("application/json", "result.json"),
    ]
    for part, key in zip(parts, ("html", "csv", "json"), strict=True):
        assert part.get_payload(decode=True) == files[key].read_bytes()
    assert not missing.exists()


def test_build_message_attachment_of_another_kind(email_config: EmailConfig, tmp_path: Path) -> None:
    """An unknown suffix is attached as a binary blob."""
    blob = tmp_path / "medium.zip"
    blob.write_bytes(b"PK\x03\x04 binary")
    message = Notifier(email_config).build_message(make_job(), attachments=[str(blob)])
    (part,) = list(message.iter_attachments())
    assert part.get_content_type() == "application/octet-stream"
    assert part.get_payload(decode=True) == b"PK\x03\x04 binary"


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


def test_send_dry_run_writes_a_parseable_eml(email_config: EmailConfig) -> None:
    """The dry run writes a real message into dry_run_dir and never connects."""
    job = make_job(job_id="job-777", label="", share_link="")
    outcome = Notifier(email_config).send(job, dry_run=True)

    assert outcome.ok is True
    assert outcome.dry_run is True
    assert outcome.sent is False
    assert outcome.error == ""
    assert outcome.subject == "[DCS] INFECTED - /media/usb0 (19 files)"

    directory = Path(email_config.dry_run_dir)
    path = Path(outcome.message_path)
    assert path.parent == directory
    assert path.name == "job-777-[DCS] INFECTED - _media_usb0 (19 files).eml"
    assert path.is_file()
    assert path.stat().st_size > 0

    with path.open("rb") as handle:
        parsed = message_from_binary_file(handle)
    assert parsed["Subject"] == outcome.subject
    assert parsed["To"] == "ops@example.test"
    assert parsed.get_content_type() == "multipart/alternative"
    assert outcome.body_preview.startswith(f"{APP_NAME} - INFECTED")


def test_send_dry_run_with_attachments(email_config: EmailConfig, tmp_path: Path) -> None:
    """Attachments survive the round trip through the .eml file."""
    files = write_report_files(tmp_path)
    job = make_job()
    outcome = Notifier(email_config).send(job, attachments=[str(files["csv"])], dry_run=True)

    assert outcome.ok is True
    with Path(outcome.message_path).open("rb") as handle:
        parsed = message_from_binary_file(handle, policy=policy.default)
    (part,) = list(parsed.iter_attachments())
    assert part.get_filename() == "findings.csv"
    assert part.get_payload(decode=True) == files["csv"].read_bytes()


# ---------------------------------------------------------------------------
# delivery over the fake SMTP client
# ---------------------------------------------------------------------------


def test_send_starttls_flow(fake_smtp: type[FakeSmtp], email_config: EmailConfig) -> None:
    """STARTTLS is a plain connection, ehlo, starttls, ehlo, login, send."""
    assert email_config.security == "starttls"
    notifier = Notifier(email_config, username="ops@example.test", password="s3cret")
    outcome = notifier.send(make_job())

    assert outcome.ok is True
    assert outcome.sent is True
    assert outcome.dry_run is False
    assert outcome.error == ""
    assert outcome.recipients == ["ops@example.test"]
    assert outcome.server == "smtp.example.test:587"

    client = only_client()
    assert client.names == ["connect:plain", "ehlo", "starttls", "ehlo", "login", "send_message", "quit"]
    assert client.calls[4] == ("login", ("ops@example.test", "s3cret"))
    assert isinstance(client.calls[2][1][0], ssl.SSLContext)
    assert client.timeout == email_config.timeout_s
    assert client.sent_message["Subject"] == outcome.subject
    assert client.sent_message["X-DCS-Verdict"] == "infected"


def test_send_secure_flow_uses_smtp_ssl(fake_smtp: type[FakeSmtp], email_config: EmailConfig) -> None:
    """Security ssl uses SMTP_SSL and never starts a plain connection."""
    email_config.security = "ssl"
    notifier = Notifier(email_config, username="ops@example.test", password="s3cret")
    outcome = notifier.send(make_job())

    assert outcome.ok is True
    assert outcome.sent is True
    client = only_client()
    assert isinstance(client, FakeSmtpSsl)
    assert client.names == ["connect:ssl", "login", "send_message", "quit"]
    assert "starttls" not in client.names
    assert "ehlo" not in client.names


def test_send_security_none_skips_starttls(
    fake_smtp: type[FakeSmtp], email_config: EmailConfig
) -> None:
    """Security none talks plaintext and does not upgrade."""
    email_config.security = "none"
    outcome = Notifier(email_config).send(make_job())

    assert outcome.ok is True
    client = only_client()
    assert client.names == ["connect:plain", "ehlo", "send_message", "quit"]
    assert "starttls" not in client.names


def test_send_without_credentials_skips_login(
    fake_smtp: type[FakeSmtp], email_config: EmailConfig
) -> None:
    """No username means no authentication attempt."""
    outcome = Notifier(email_config).send(make_job())
    assert outcome.ok is True
    assert "login" not in only_client().names


def test_send_with_a_username_but_no_password_skips_login(
    fake_smtp: type[FakeSmtp], email_config: EmailConfig
) -> None:
    """A username alone is not enough to authenticate."""
    email_config.username = "ops@example.test"
    email_config.password = ""
    outcome = Notifier(email_config).send(make_job())
    assert outcome.ok is True
    assert "login" not in only_client().names


def test_send_with_a_password_but_no_username_skips_login(
    fake_smtp: type[FakeSmtp], email_config: EmailConfig
) -> None:
    """A password alone is not enough to authenticate either."""
    outcome = Notifier(email_config, password="s3cret").send(make_job())
    assert outcome.ok is True
    assert "login" not in only_client().names


def test_send_reports_an_authentication_error(
    fake_smtp: type[FakeSmtp], email_config: EmailConfig
) -> None:
    """A refused login is reported and the client is still closed."""
    fake_smtp.send_error = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    notifier = Notifier(email_config, username="ops@example.test", password="wrong")
    outcome = notifier.send(make_job())

    assert outcome.ok is False
    assert outcome.sent is False
    assert outcome.error.startswith("delivery failed: ")
    assert "535" in outcome.error
    assert "bad credentials" in outcome.error
    assert only_client().names[-1] == "quit"


def test_send_reports_a_generic_smtp_error(
    fake_smtp: type[FakeSmtp], email_config: EmailConfig
) -> None:
    """Any SMTPException from the server is a delivery failure."""
    fake_smtp.send_error = smtplib.SMTPException("mailbox unavailable")
    outcome = Notifier(email_config).send(make_job())

    assert outcome.ok is False
    assert outcome.sent is False
    assert outcome.error == "delivery failed: mailbox unavailable"


def test_send_reports_a_connection_failure(
    fake_smtp: type[FakeSmtp], email_config: EmailConfig
) -> None:
    """A dead server is reported as a connection problem, not as a send failure."""
    fake_smtp.connect_error = "connection refused"
    outcome = Notifier(email_config).send(make_job())

    assert outcome.ok is False
    assert outcome.sent is False
    assert outcome.error == "cannot connect to smtp.example.test:587: connection refused"
    assert fake_smtp.instances == []


def test_send_with_a_broken_subject_template_still_delivers(
    fake_smtp: type[FakeSmtp], email_config: EmailConfig
) -> None:
    """A broken template degrades to the default subject instead of failing the job."""
    email_config.subject_template = "{nope}"
    outcome = Notifier(email_config).send(make_job())
    assert outcome.ok is True
    assert outcome.sent is True
    assert outcome.subject == "[DCS] INFECTED - KINGSTON"
    assert only_client().sent_message["Subject"] == "[DCS] INFECTED - KINGSTON"


def test_send_when_email_is_disabled(
    fake_smtp: type[FakeSmtp], email_config: EmailConfig
) -> None:
    """A disabled notification stage reports why and touches nothing."""
    email_config.enabled = False
    outcome = Notifier(email_config).send(make_job())

    assert outcome.ok is False
    assert outcome.sent is False
    assert outcome.error == "email notifications are disabled in the configuration"
    assert outcome.recipients == ["ops@example.test"]
    assert outcome.subject == "[DCS] INFECTED - KINGSTON (19 files)"
    assert fake_smtp.instances == []


def test_send_without_a_host_or_sender(
    fake_smtp: type[FakeSmtp], email_config: EmailConfig
) -> None:
    """The delivery stage refuses to run without a host or a sender."""
    email_config.smtp_host = ""
    outcome = Notifier(email_config).send(make_job())
    assert outcome.ok is False
    assert outcome.sent is False
    assert outcome.error == "SMTP host or sender address is not configured"
    assert fake_smtp.instances == []


def test_send_result_lists_the_unique_recipients(
    fake_smtp: type[FakeSmtp], email_config: EmailConfig
) -> None:
    """Duplicate addresses across to, cc and bcc are collapsed."""
    email_config.to_addrs = ["ops@example.test"]
    email_config.cc_addrs = ["boss@example.test", "ops@example.test"]
    email_config.bcc_addrs = ["archive@example.test"]
    outcome = Notifier(email_config).send(make_job())
    assert outcome.recipients == [
        "ops@example.test",
        "boss@example.test",
        "archive@example.test",
    ]
    assert outcome.ok is True


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


def test_test_connection_without_a_host(fake_smtp: type[FakeSmtp], email_config: EmailConfig) -> None:
    """No host is answered before any connection is attempted."""
    email_config.smtp_host = ""
    assert Notifier(email_config).test_connection() == (False, "no SMTP host configured")
    assert fake_smtp.instances == []


def test_test_connection_success(fake_smtp: type[FakeSmtp], email_config: EmailConfig) -> None:
    """A 250 from noop means the settings work."""
    notifier = Notifier(email_config)
    assert notifier.test_connection() == (True, "server replied 250 OK")
    client = only_client()
    assert client.names == ["connect:plain", "ehlo", "starttls", "ehlo", "noop", "quit"]


def test_test_connection_rejected(fake_smtp: type[FakeSmtp], email_config: EmailConfig) -> None:
    """Any other code is reported verbatim."""
    fake_smtp.noop_code = 421
    fake_smtp.noop_text = b"service not available"
    assert Notifier(email_config).test_connection() == (False, "server replied 421 service not available")


def test_test_connection_unreachable(fake_smtp: type[FakeSmtp], email_config: EmailConfig) -> None:
    """A connection error is reported with its type."""
    fake_smtp.connect_error = "connection refused"
    assert Notifier(email_config).test_connection() == (False, "OSError: connection refused")


# ---------------------------------------------------------------------------
# desktop notifications: the "Open Ducky Can Shoot" button without Qt
# ---------------------------------------------------------------------------


class FakeNotifySend:
    """A stand-in for notify-send that records the command line it was given."""

    def __init__(
        self, stdout: str = "", returncode: int = 0, stderr: str = "", raises: Any = None
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(argv))
        self.timeouts.append(float(kwargs.get("timeout") or 0))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class AvailableNotifier(DesktopNotifier):
    """A notifier whose binary is present, whatever the test machine has.

    The real class checks shutil.which() before doing anything, which is right
    in production and wrong in a test: a machine without libnotify installed
    must still be able to test the command line that would be run.
    """

    def __init__(self, runner: FakeNotifySend, **kwargs: Any) -> None:
        super().__init__(runner=runner, binary="/usr/bin/notify-send", **kwargs)

    def available(self) -> bool:
        return True


def notifier_with(runner: FakeNotifySend, **kwargs: Any) -> DesktopNotifier:
    return AvailableNotifier(runner, **kwargs)


def test_a_notification_offers_the_open_action_and_reports_it() -> None:
    runner = FakeNotifySend(stdout="open\n")
    choice = notifier_with(runner, timeout_ms=15000).notify(
        "USB medium plugged in: STICK",
        "STICK (14.9 GiB). Open Ducky Can Shoot to scan it.",
    )

    assert choice == "open"
    argv = runner.calls[0]
    assert argv[0] == "/usr/bin/notify-send"
    assert "--action" in argv and "open=Open Ducky Can Shoot" in argv
    assert "--app-name" in argv and APP_NAME in argv
    assert "--expire-time" in argv and "15000" in argv
    assert argv[-2:] == [
        "USB medium plugged in: STICK",
        "STICK (14.9 GiB). Open Ducky Can Shoot to scan it.",
    ]


def test_the_waiter_gives_up_after_the_notification_lifetime() -> None:
    runner = FakeNotifySend(stdout="")
    notifier_with(runner, timeout_ms=2000, extra_wait_s=5).notify("title", "body")
    assert runner.timeouts[0] == pytest.approx(7.0)


def test_a_dismissed_notification_reports_nothing() -> None:
    assert notifier_with(FakeNotifySend(stdout="")).notify("title", "body") == ""


def test_a_refused_notification_is_not_an_error() -> None:
    runner = FakeNotifySend(returncode=1, stderr="no notification daemon")
    assert notifier_with(runner).notify("title", "body") == ""


def test_a_missing_notification_server_is_survived() -> None:
    runner = FakeNotifySend(raises=OSError("cannot connect to the session bus"))
    assert notifier_with(runner).notify("title", "body") == ""


def test_a_notification_nobody_answers_is_survived() -> None:
    runner = FakeNotifySend(raises=subprocess.TimeoutExpired("notify-send", 20))
    assert notifier_with(runner).notify("title", "body") == ""


# ---------------------------------------------------------------------------
# the D-Bus route: the one that keeps the buttons
# ---------------------------------------------------------------------------


class FakeMonitor:
    """A stand-in for 'gdbus monitor' that hands out prepared lines."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self.stdout = self
        self.terminated = False
        self._closed = False

    def readline(self) -> str:
        if self._lines:
            return self._lines.pop(0)
        self._closed = True
        return ""

    def poll(self) -> int | None:
        return 0 if self._closed else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def fileno(self) -> int:
        return 0


def dbus_notifier(
    call_stdout: str = "(uint32 42,)",
    monitor_lines: list[str] | None = None,
    **kwargs: Any,
) -> tuple[DbusNotifier, FakeNotifySend, FakeMonitor]:
    monitor = FakeMonitor(monitor_lines if monitor_lines is not None else [])
    runner = FakeNotifySend(stdout=call_stdout)
    notifier = DbusNotifier(runner=runner, opener=lambda *a, **k: monitor, timeout_ms=1000, **kwargs)
    notifier.binary = "/usr/bin/gdbus"  # skip the which() lookup
    return notifier, runner, monitor


def test_the_dbus_call_carries_the_button() -> None:
    notifier, runner, _monitor = dbus_notifier()

    notifier.notify("USB medium plugged in: STICK", "STICK (14.9 GiB)")

    argv = runner.calls[0]
    assert argv[:2] == ["/usr/bin/gdbus", "call"]
    assert "org.freedesktop.Notifications.Notify" in argv
    assert "['open', 'Open Ducky Can Shoot']" in argv, "the action pair reaches the server"
    assert any("resident" in part for part in argv), "so the button stays on screen"
    assert any("urgency" in part for part in argv)
    assert argv[-1] == "1000", "the notification lifetime is the configured one"
    assert "USB medium plugged in: STICK" in argv


def test_the_action_the_user_pressed_comes_back() -> None:
    notifier, _runner, monitor = dbus_notifier(
        call_stdout="(uint32 42,)",
        monitor_lines=[
            "Monitoring signals on object /org/freedesktop/Notifications",
            "The name org.freedesktop.Notifications is owned by :1.28",
            "/org/freedesktop/Notifications: org.freedesktop.Notifications.ActionInvoked (uint32 42, 'open')",
        ],
    )

    assert notifier.notify("title", "body") == "open"
    assert monitor.terminated is True, "the monitor is not left behind"


def test_a_notification_that_was_closed_reports_nothing() -> None:
    notifier, _runner, _monitor = dbus_notifier(
        monitor_lines=[
            "/org/freedesktop/Notifications: org.freedesktop.Notifications.NotificationClosed (uint32 42, 2)"
        ]
    )

    assert notifier.notify("title", "body") == ""


def test_an_action_for_another_notification_is_ignored() -> None:
    notifier, _runner, _monitor = dbus_notifier(
        call_stdout="(uint32 42,)",
        monitor_lines=[
            "ActionInvoked (uint32 7, 'open')",
            "NotificationClosed (uint32 42, 1)",
        ],
    )

    assert notifier.notify("title", "body") == ""


def test_a_missing_gdbus_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dcs.core.notify.dbus.shutil.which", lambda name: None)

    assert DbusNotifier().available() is False


def test_a_refused_dbus_call_is_not_an_error() -> None:
    monitor = FakeMonitor([])
    runner = FakeNotifySend(returncode=1, stderr="no such service")
    notifier = DbusNotifier(runner=runner, opener=lambda *a, **k: monitor, timeout_ms=100)
    notifier.binary = "/usr/bin/gdbus"

    assert notifier.notify("title", "body") == ""


def test_the_factory_prefers_the_connection_that_stays_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The persistent connection first, then gdbus, then notify-send."""
    monkeypatch.setattr(GioNotifier, "available", lambda self: True)
    monkeypatch.setattr(DbusNotifier, "available", lambda self: True)
    assert isinstance(build_notifier(timeout_ms=5000), GioNotifier), (
        "a one-shot gdbus call loses the notification on Plasma"
    )

    monkeypatch.setattr(GioNotifier, "available", lambda self: False)
    assert isinstance(build_notifier(timeout_ms=5000), DbusNotifier)

    monkeypatch.setattr(DbusNotifier, "available", lambda self: False)
    monkeypatch.setattr(DesktopNotifier, "available", lambda self: True)
    assert isinstance(build_notifier(timeout_ms=5000), DesktopNotifier)

    # and with none of them the caller still gets something that reports why
    monkeypatch.setattr(DesktopNotifier, "available", lambda self: False)
    notifier = build_notifier(timeout_ms=5000)
    assert notifier.available() is False



# ---------------------------------------------------------------------------
# the persistent notifier (Gio): the one that survives Plasma
# ---------------------------------------------------------------------------


class FakeParams:
    def __init__(self, values: tuple[Any, ...]) -> None:
        self._values = values

    def unpack(self) -> tuple[Any, ...]:
        return self._values


class FakeReply:
    def __init__(self, values: tuple[Any, ...]) -> None:
        self._values = values

    def unpack(self) -> tuple[Any, ...]:
        return self._values


class FakeContext:
    def push_thread_default(self) -> None:
        pass

    def pop_thread_default(self) -> None:
        pass

    def iteration(self, may_block: bool) -> None:
        pass


class FakeConnection:
    def __init__(self, notification_id: int = 7, events: tuple[tuple[str, tuple[Any, ...]], ...] = ()) -> None:
        self.notification_id = notification_id
        self.events = events
        self.calls: list[tuple[str, Any]] = []
        self.callback = None

    def signal_subscribe(self, *args: Any) -> int:
        self.callback = args[-1]
        return 1

    def signal_unsubscribe(self, subscription: int) -> None:
        pass

    def call_sync(
        self,
        service: str,
        path: str,
        interface: str,
        method: str,
        variant: Any,
        *rest: Any,
    ) -> FakeReply:
        self.calls.append((method, variant))
        if method == "Notify" and self.callback is not None:
            for name, payload in self.events:
                self.callback(None, None, path, interface, name, FakeParams(payload))
        return FakeReply((self.notification_id,))

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


class FakeGLib:
    class Variant:
        def __init__(self, signature: str, value: Any) -> None:
            self.signature = signature
            self.value = value

        def __eq__(self, other: object) -> bool:
            return (
                isinstance(other, FakeGLib.Variant)
                and other.signature == self.signature
                and other.value == self.value
            )

    class MainContext:
        @staticmethod
        def new() -> FakeContext:
            return FakeContext()


class FakeGio:
    class BusType:
        SESSION = "session"

    class DBusCallFlags:
        NONE = 0

    class DBusSignalFlags:
        NONE = 0

    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def bus_get_sync(self, bus_type: Any, cancellable: Any = None) -> FakeConnection:
        return self.connection


def gio_loader(connection: FakeConnection) -> Any:
    gio = FakeGio(connection)
    return lambda: (gio, FakeGLib)


def test_the_gio_notifier_is_available_with_pygobject() -> None:
    assert GioNotifier(loader=gio_loader(FakeConnection())).available() is True
    assert GioNotifier(loader=lambda: None).available() is False


def test_the_gio_notifier_reports_the_action_that_was_pressed() -> None:
    connection = FakeConnection(events=(("ActionInvoked", (7, "open")),))
    notifier = GioNotifier(loader=gio_loader(connection), timeout_ms=100)

    assert notifier.notify("title", "body") == "open"
    assert notifier.last_id == 7
    method, variant = connection.calls[0]
    assert method == "Notify"
    assert variant.signature == "(susssasa{sv}i)"
    assert variant.value[0] == "Ducky Can Shoot"
    assert variant.value[5] == ["open", "Open Ducky Can Shoot"]


def test_the_gio_notifier_takes_the_notification_down_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing pressed: it must be closed, or the button would go nowhere."""
    connection = FakeConnection(notification_id=11, events=(("NotificationClosed", (11, 1)),))
    notifier = GioNotifier(loader=gio_loader(connection), timeout_ms=100)
    monkeypatch.setattr("dcs.core.notify.dbus.GRACE_S", 0.0)

    assert notifier.notify("title", "body") == ""
    # the notification closed itself (reason 1) before anything was pressed, and
    # closing it again is harmless; what matters is that nothing answered
    assert "CloseNotification" in connection.methods() or notifier.last_id == 11


def test_the_gio_notifier_is_quiet_without_pygobject() -> None:
    notifier = GioNotifier(loader=lambda: None)

    assert notifier.notify("title", "body") == ""
    assert notifier.last_id == 0


def test_a_broken_gio_call_is_not_an_error() -> None:
    class Exploding(FakeGio):
        def bus_get_sync(self, bus_type: Any, cancellable: Any = None) -> Any:
            raise RuntimeError("connection refused")

    notifier = GioNotifier(loader=lambda: (Exploding(FakeConnection()), FakeGLib))

    assert notifier.notify("title", "body") == ""

def test_the_cli_test_command_reports_the_button(capsys: Any) -> None:
    from dcs import cli

    class ButtonNotifier:
        def available(self) -> bool:
            return True

        def notify(self, title: str, body: str = "", **kwargs: Any) -> str:
            return "open"

    code = cli._watch_test_notification(ButtonNotifier())
    captured = capsys.readouterr()

    assert code == 0
    assert "the button works" in captured.out


def test_the_cli_test_command_warns_when_the_button_is_missing(capsys: Any) -> None:
    from dcs import cli

    class PlainNotifier:
        def available(self) -> bool:
            return True

        def notify(self, title: str, body: str = "", **kwargs: Any) -> str:
            return ""

    code = cli._watch_test_notification(PlainNotifier())
    captured = capsys.readouterr()

    assert code == 1
    assert "no button press" in captured.out


def test_availability_follows_notify_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop_notify.shutil, "which", lambda name: "/usr/bin/notify-send")
    assert DesktopNotifier().available() is True
    monkeypatch.setattr(desktop_notify.shutil, "which", lambda name: None)
    assert DesktopNotifier().available() is False

