"""Notification layer (currently SMTP email)."""

from __future__ import annotations

from dcs.core.notify.smtp import Notifier
from dcs.core.notify.templates import build_subject, render_email_html, render_email_text

__all__ = ["Notifier", "build_subject", "render_email_html", "render_email_text"]
