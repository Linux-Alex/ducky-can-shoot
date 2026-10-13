"""SMTP delivery of the verdict email.

Kept deliberately boring: smtplib, STARTTLS or implicit TLS, optional
authentication, and a --dry-run mode that writes the message to disk as .eml so
the whole pipeline can be exercised (and reviewed) without sending anything.
"""

from __future__ import annotations

import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from dcs.core.config import EmailConfig
from dcs.core.log import get_logger
from dcs.core.notify.templates import build_subject, render_email_html, render_email_text
from dcs.core.result import EmailResult, JobResult
from dcs.version import APP_NAME, __version__

log = get_logger("notify.smtp")


class Notifier:
    """Builds and sends the notification message."""

    def __init__(self, config: EmailConfig, *, password: str = "", username: str = "") -> None:
        self.config = config
        self.password = password
        self.username = username or config.username

    # -- message construction --------------------------------------------
    def build_message(
        self,
        result: JobResult,
        *,
        attachments: list[str] | None = None,
    ) -> EmailMessage:
        config = self.config
        message = EmailMessage()
        message["Subject"] = build_subject(config, result)
        message["From"] = formataddr((config.from_name or APP_NAME, config.from_addr))
        recipients = list(dict.fromkeys(config.to_addrs + config.cc_addrs + config.bcc_addrs))
        message["To"] = ", ".join(config.to_addrs) or config.from_addr
        if config.cc_addrs:
            message["Cc"] = ", ".join(config.cc_addrs)
        if config.reply_to:
            message["Reply-To"] = config.reply_to
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=(config.from_addr.split("@")[-1] or "dcs.local"))
        message["X-DCS-Job"] = result.job_id
        message["X-DCS-Verdict"] = str(result.verdict)
        if not recipients:
            recipients = [config.from_addr]

        message.set_content(render_email_text(result))
        message.add_alternative(render_email_html(result), subtype="html")

        for path in attachments or []:
            if not path or not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as handle:
                    data = handle.read()
            except OSError as exc:
                log.warning("cannot attach %s: %s", path, exc)
                continue
            if path.endswith(".html"):
                maintype, subtype = "text", "html"
            elif path.endswith(".csv"):
                maintype, subtype = "text", "csv"
            elif path.endswith(".json"):
                maintype, subtype = "application", "json"
            else:
                maintype, subtype = "application", "octet-stream"
            message.add_attachment(data, maintype=maintype, subtype=subtype, filename=os.path.basename(path))
        return message

    # -- delivery ---------------------------------------------------------
    def send(
        self,
        result: JobResult,
        *,
        attachments: list[str] | None = None,
        dry_run: bool = False,
    ) -> EmailResult:
        started = time.monotonic()
        config = self.config
        outcome = EmailResult(
            recipients=list(dict.fromkeys(config.to_addrs + config.cc_addrs + config.bcc_addrs)),
            subject=build_subject(config, result),
            server=f"{config.smtp_host}:{config.smtp_port}",
        )

        if not config.enabled:
            outcome.error = "email notifications are disabled in the configuration"
            return outcome

        message = self.build_message(result, attachments=attachments)
        outcome.body_preview = render_email_text(result)[:4000]
        outcome.duration = time.monotonic() - started

        if dry_run:
            directory = config.dry_run_dir or os.path.join(
                os.path.expanduser("~"), ".local", "share", "dcs", "outbox"
            )
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, f"{result.job_id}-{build_subject(config, result)[:40]}.eml".replace("/", "_"))
            try:
                with open(path, "wb") as handle:
                    handle.write(message.as_bytes())
            except OSError as exc:
                outcome.error = f"could not write the dry run message: {exc}"
                return outcome
            outcome.ok = True
            outcome.dry_run = True
            outcome.message_path = path
            outcome.duration = time.monotonic() - started
            log.info("dry run: message written to %s", path)
            return outcome

        if not config.smtp_host or not config.from_addr:
            outcome.error = "SMTP host or sender address is not configured"
            return outcome

        try:
            client = self._connect()
        except (OSError, smtplib.SMTPException, ssl.SSLError) as exc:
            outcome.error = f"cannot connect to {config.smtp_host}:{config.smtp_port}: {exc}"
            log.error(outcome.error)
            outcome.duration = time.monotonic() - started
            return outcome

        try:
            # The envelope recipients are passed explicitly: a Bcc header is not
            # written (that would defeat the point), so smtplib cannot derive
            # them from the headers and they would silently receive nothing.
            client.send_message(message, to_addrs=outcome.recipients or [config.from_addr])
            outcome.ok = True
            outcome.sent = True
            log.info("verdict email sent to %s", ", ".join(outcome.recipients))
        except (OSError, smtplib.SMTPException) as exc:
            outcome.error = f"delivery failed: {exc}"
            log.error(outcome.error)
        finally:
            try:
                client.quit()
            except (OSError, smtplib.SMTPException):
                client.close()
        outcome.duration = time.monotonic() - started
        return outcome

    # -- plumbing ---------------------------------------------------------
    def _context(self) -> ssl.SSLContext:
        if self.config.insecure_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        return ssl.create_default_context()

    def _connect(self) -> smtplib.SMTP:
        config = self.config
        security = config.security.lower()
        if security in ("ssl", "tls"):
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                config.smtp_host, config.smtp_port, timeout=config.timeout_s, context=self._context()
            )
        else:
            client = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.timeout_s)
            client.ehlo()
            if security in ("starttls", "tls_starttls", ""):
                client.starttls(context=self._context())
                client.ehlo()
        if self.username and self.password:
            client.login(self.username, self.password)
        elif self.username:
            log.warning("an SMTP username is set but no password was resolved; skipping authentication")
        return client

    def test_connection(self) -> tuple[bool, str]:
        """Used by the GUI settings page and by dcs doctor."""
        config = self.config
        if not config.smtp_host:
            return False, "no SMTP host configured"
        try:
            client = self._connect()
        except (OSError, smtplib.SMTPException, ssl.SSLError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        try:
            code, message = client.noop()
            return code == 250, f"server replied {code} {message.decode(errors='replace') if isinstance(message, bytes) else message}"
        except (OSError, smtplib.SMTPException) as exc:
            return False, str(exc)
        finally:
            try:
                client.quit()
            except (OSError, smtplib.SMTPException):
                client.close()


NOTIFIER_VERSION = __version__
