"""Headless, Qt-free core of Ducky Can Shoot.

Modules
-------
config    : layered TOML configuration with secret resolution
events    : the progress/event model shared by CLI and GUI
device    : removable-media discovery and mounting (lsblk + udisks2)
scanner   : pluggable antivirus engines (ClamAV, built-in heuristics)
archiver  : ZIP/7z image creation with progress and optional splitting
upload    : cloud upload adapters (gofile.io and friends)
report    : HTML/text/CSV report generation
notify    : SMTP delivery of the verdict email
pipeline  : orchestration of the whole job with a single event stream
"""

from __future__ import annotations

__all__: list[str] = []
