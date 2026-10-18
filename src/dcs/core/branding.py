"""The drawing that reports and emails carry with them.

Both documents are read on machines that have no icon theme of ours - a report
is mailed to somebody else, a report file is opened weeks later - so the
picture has to be inside the document: the HTML report embeds it as a data URI
(that report is self contained by design) and the email carries it as a related
part with a Content-ID, which is how mail clients expect an inline image.

The PNG is a 96 px rendering of the same illustration the theme icons come
from, written by scripts/make-icons.py (`make icons`).
"""

from __future__ import annotations

import base64
import functools
from pathlib import Path

#: the package data directory (hashes.txt lives here too)
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

#: the small rendering that documents use
ICON_PATH = DATA_DIR / "icon.png"

#: the Content-ID the email HTML refers to
CONTENT_ID = "dcs-icon"


@functools.lru_cache(maxsize=1)
def icon_png() -> bytes:
    """The document icon, or b"" when the package is missing it."""
    try:
        return ICON_PATH.read_bytes()
    except OSError:
        return b""


@functools.lru_cache(maxsize=1)
def data_uri() -> str:
    """The icon as a data URI, or "" when there is no icon to embed."""
    raw = icon_png()
    if not raw:
        return ""
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def content_id() -> str:
    """The value for the Content-ID header of the inline email part."""
    return f"<{CONTENT_ID}>"


__all__ = ["CONTENT_ID", "ICON_PATH", "content_id", "data_uri", "icon_png"]
