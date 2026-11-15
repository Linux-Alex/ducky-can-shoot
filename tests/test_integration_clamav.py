"""Real ClamAV integration: the only test that runs a signature scanner.

Unit tests use the fake `clamscan` and `clamdscan` in tests/fakes, which keeps
the suite hermetic.  This module is the exception: it is marked `integration`,
skips unless a *real* ClamAV binary and a usable signature database are present,
and is meant for the scheduled `clamav.yml` workflow (and for a developer who
wants to check the engine front end against the real thing).

Run it by hand with:

    PYTHONPATH=src python3 -m pytest -q -m integration tests/test_integration_clamav.py

Set `DCS_CLAMAV_INTEGRATION=1` to make a missing ClamAV an error instead of a
skip, which is what the scheduled workflow does so that a broken install cannot
silently turn the check green.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from dcs.core.config import ScanConfig
from dcs.core.events import Verdict
from dcs.core.scanner import Scanner

pytestmark = pytest.mark.integration

EICAR = (
    b"X5O!P%@AP[4" + bytes([92]) + b"PZX54(P^)7CC)7}$"
    b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)


def real_clamscan() -> str:
    """The distribution's clamscan, never the fake in tests/fakes."""
    found = shutil.which("clamscan")
    if not found:
        return ""
    resolved = Path(found).resolve()
    if (Path(__file__).resolve().parent / "fakes") in resolved.parents:
        return ""
    return str(resolved)


@pytest.fixture(scope="module")
def clamscan() -> str:
    found = real_clamscan()
    if not found:
        message = "no real clamscan on this machine"
        if os.environ.get("DCS_CLAMAV_INTEGRATION"):
            pytest.fail(f"DCS_CLAMAV_INTEGRATION is set but there is {message}")
        pytest.skip(message)
    return found


def test_real_clamav_detects_the_eicar_test_file(
    clamscan: str, tmp_path: Path
) -> None:
    """The end to end contract of the ClamAV engine against the real scanner."""
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "eicar.com").write_bytes(EICAR)
    (medium / "readme.txt").write_text("nothing to see here\n")

    config = ScanConfig()
    config.engines = ["clamav"]
    config.clamav.clamscan_binary = clamscan
    config.clamav.prefer_daemon = False
    config.clamav.max_database_age_days = 0

    result = Scanner(config).run(str(medium))

    status = next((item for item in result.engines if item.engine_id == "clamav"), None)
    if status is None or not status.available:
        message = f"clamscan is installed but not usable: {getattr(status, 'detail', '')}"
        if os.environ.get("DCS_CLAMAV_INTEGRATION"):
            pytest.fail(message)
        pytest.skip(message)

    assert result.verdict is Verdict.INFECTED
    threats = [finding.threat for finding in result.findings if finding.engine == "clamav"]
    assert threats, result.findings
    assert any("eicar" in threat.lower() for threat in threats)
    assert result.stats.files_scanned >= 2
