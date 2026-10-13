"""Ducky Can Shoot (DCS).

A defensive USB triage tool:

    pick a mounted USB stick -> scan every file -> archive the image ->
    upload it to a free host -> email the verdict (and the link when it is safe).

The package is deliberately split so that the GUI is a thin shell around a
headless, testable core (dcs.core).  Nothing in dcs.core imports Qt.
"""

from __future__ import annotations

from dcs.version import APP_ID, APP_NAME, APP_SUMMARY, __version__

__all__ = ["APP_ID", "APP_NAME", "APP_SUMMARY", "__version__"]
