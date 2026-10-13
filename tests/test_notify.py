"""Tests for the email notification stage (dcs.core.notify).

No test opens a socket: the SMTP client is replaced by a recording stand in and
the dry run mode writes a real .eml file inside tmp_path.
"""

from __future__ import annotations

import html
import smtplib
import ssl
from email import message_from_binary_file, policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from dcs.core.archiver import ArchivePart, ArchiveResult
from dcs.core.config import AppConfig, EmailConfig
from dcs.core.events import Finding, ScanStats, Severity, Verdict
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
    instances: list[FakeSmtp] = []
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

    def send_message(self, message: EmailMessage) -> dict[str, Any]:
        self._note("send_message", message)
        if type(self).send_error is not None:
            raise type(self).send_error
        return {}

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
    assert [part.get_content_type() for part in message.iter_parts()] == ["text/plain", "text/html"]
    assert message.get_body(preferencelist=("plain",)).get_content().startswith(
        f"{APP_NAME} - INFECTED"
    )
    assert "KINGSTON" in message.get_body(preferencelist=("html",)).get_content()
    assert not list(message.iter_attachments())


def test_build_message_omits_optional_headers(email_config: EmailConfig) -> None:
    """Cc and Reply-To only appear when they are configured."""
    message = Notifier(email_config).build_message(make_job())
    assert message["Cc"] is None
    assert message["Reply-To"] is None
    assert message["To"] == "ops@example.test"


@pytest.mark.xfail(reason="configured bcc addrs are counted as recipients but never added to the message")
def test_build_message_keeps_bcc_recipients(email_config: EmailConfig) -> None:
    """A configured BCC must reach the envelope, not only the log line."""
    email_config.bcc_addrs = ["archive@example.test"]
    message = Notifier(email_config).build_message(make_job())
    assert message["Bcc"] == "archive@example.test"


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
