"""Qt/Kirigami front end.

The GUI is a thin shell: it owns no security logic.  It runs the same
dcs.core.pipeline that the CLI runs, on a worker thread, and turns the event
stream into QAbstractListModel updates that QML can animate.
"""

from __future__ import annotations

__all__: list[str] = []
