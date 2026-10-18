"""Hash intelligence: local block lists, the reputation lookups and their cache.

No test reaches the network: `intel.http_json` is replaced, and the provider
classes are exercised through it so their parsing is covered too.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from dcs.core.config import IntelConfig, ScanConfig
from dcs.core.events import NullSink, Severity, Verdict
from dcs.core.scanner import Scanner
from dcs.core.scanner import intel as intel_module
from dcs.core.scanner.base import ScanContext
from dcs.core.scanner.intel import Answer, AnswerCache, IntelEngine, MetaDefender, VirusTotal
from dcs.core.util import sha256_file


# -- helpers ---------------------------------------------------------------
def context(root: Path) -> ScanContext:
    return ScanContext(root=str(root), config=ScanConfig(), cancel=threading.Event(), sink=NullSink())


def run(engine: IntelEngine, root: Path) -> list:
    return list(engine.scan_tree(str(root), context(root)))


def vt_payload(*, malicious: int = 2, suspicious: int = 0) -> dict[str, Any]:
    results = {
        "Engine-A": {"category": "malicious", "result": "Win32.Trojan.Fake"},
        "Engine-B": {"category": "malicious", "result": "Trojan.Generic.123"},
        "Engine-C": {"category": "clean", "result": None},
    }
    if malicious == 0:
        results = {"Engine-C": {"category": "clean", "result": None}}
    return {
        "data": {
            "attributes": {
                "sha256": "0" * 64,
                "last_analysis_stats": {
                    "malicious": malicious,
                    "suspicious": suspicious,
                    "harmless": 60,
                    "undetected": 8,
                },
                "last_analysis_results": results,
            }
        }
    }


def md_payload(detected: int = 1) -> dict[str, Any]:
    return {
        "scan_results": {
            "scan_all_result_a": "Infected" if detected else "Clean",
            "total_avs": 27,
            "total_detected_avs": detected,
            "scan_details": {"Engine-A": {"threat_found": "EICAR-Test-File" if detected else ""}},
        }
    }


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace the HTTP layer and record every request."""
    seen: list[tuple[str, str]] = []

    def fake_http(method: str, url: str, **kwargs: Any) -> tuple[int, bytes]:
        seen.append((method, url))
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(intel_module, "http_json", fake_http)
    return seen


def engine_for(tmp_path: Path, **overrides: Any) -> IntelEngine:
    values: dict[str, Any] = {
        "cache_dir": str(tmp_path / "cache"),
        "rate_per_minute": 1000,
        "max_lookups": 0,
    }
    values.update(overrides)
    return IntelEngine(IntelConfig(**values))  # type: ignore[arg-type]


# -- local lists -----------------------------------------------------------
def test_an_extra_hash_list_is_read(tmp_path: Path) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    sample = medium / "setup.exe"
    sample.write_bytes(b"MZ fake")
    listing = tmp_path / "blocklist.txt"
    listing.write_text(f"# name\n{sha256_file(str(sample))}  Fake.Family\n")

    findings = run(engine_for(tmp_path, hash_files=[str(listing)]), medium)

    assert [finding.threat for finding in findings] == ["known malware hash: Fake.Family"]
    assert findings[0].severity is Severity.CRITICAL
    assert findings[0].engine == "intel"
    assert findings[0].sha256 == sha256_file(str(sample))


def test_a_hash_hit_drives_the_verdict(tmp_path: Path) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    sample = medium / "setup.exe"
    sample.write_bytes(b"MZ fake")
    listing = tmp_path / "blocklist.txt"
    listing.write_text(f"{sha256_file(str(sample))} Fake\n")
    config = ScanConfig(
        engines=["heuristic", "intel"],
        intel=IntelConfig(enabled=True, hash_files=[str(listing)], cache_dir=str(tmp_path / "c")),
    )

    result = Scanner(config).run(str(medium))

    assert result.verdict is Verdict.INFECTED
    # both the heuristic engine and this one know the hash, but the finding is
    # reported once because they use the same wording
    assert [finding.threat for finding in result.findings].count("known malware hash: Fake") == 1


# -- lookups ---------------------------------------------------------------
def test_lookup_off_makes_no_requests(tmp_path: Path, calls: list) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "holiday.txt").write_text("sun")

    findings = run(engine_for(tmp_path, virustotal_api_key="key"), medium)

    assert findings == []
    assert calls == []


def test_a_detection_becomes_a_high_severity_finding(
    tmp_path: Path, calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "setup.exe").write_bytes(b"MZ fake")

    def fake_http(method: str, url: str, **kwargs: Any) -> tuple[int, bytes]:
        calls.append((method, url))
        return 200, json.dumps(vt_payload(malicious=2)).encode()

    monkeypatch.setattr(intel_module, "http_json", fake_http)
    engine = engine_for(tmp_path, lookup="hash", virustotal_api_key="key")
    findings = run(engine, medium)

    assert len(findings) == 1
    assert findings[0].severity is Severity.HIGH
    assert findings[0].threat.startswith("reputation: ")
    assert "virustotal: 2/70" in findings[0].detail
    assert findings[0].sha256 == sha256_file(str(medium / "setup.exe"))


def test_a_clean_answer_is_quiet_and_cached(
    tmp_path: Path, calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "a.txt").write_text("same content")
    (medium / "b.txt").write_text("same content")

    def fake_http(method: str, url: str, **kwargs: Any) -> tuple[int, bytes]:
        calls.append((method, url))
        return 200, json.dumps(vt_payload(malicious=0)).encode()

    monkeypatch.setattr(intel_module, "http_json", fake_http)
    engine = engine_for(tmp_path, lookup="hash", virustotal_api_key="key")

    assert run(engine, medium) == []
    # two files, one hash, one lookup
    assert len(calls) == 1


def test_a_cached_answer_survives_a_new_engine(
    tmp_path: Path, calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "setup.exe").write_bytes(b"MZ fake")

    def fake_http(method: str, url: str, **kwargs: Any) -> tuple[int, bytes]:
        calls.append((method, url))
        return 200, json.dumps(vt_payload()).encode()

    monkeypatch.setattr(intel_module, "http_json", fake_http)
    first = engine_for(tmp_path, lookup="hash", virustotal_api_key="key")
    assert run(first, medium)

    second = engine_for(tmp_path, lookup="hash", virustotal_api_key="key")
    assert run(second, medium)

    assert len(calls) == 1, calls


def test_an_unknown_file_is_remembered_as_unknown(
    tmp_path: Path, calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "setup.exe").write_bytes(b"MZ fake")

    def fake_http(method: str, url: str, **kwargs: Any) -> tuple[int, bytes]:
        calls.append((method, url))
        return 404, b"{}"

    monkeypatch.setattr(intel_module, "http_json", fake_http)
    engine = engine_for(tmp_path, lookup="hash", virustotal_api_key="key")

    assert run(engine, medium) == []
    cache = AnswerCache(tmp_path / "cache", 168)
    assert cache.get(sha256_file(str(medium / "setup.exe"))) == []


def test_the_lookup_budget_is_respected(
    tmp_path: Path, calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    for index in range(4):
        (medium / f"file-{index}.txt").write_text(f"content {index}")

    def fake_http(method: str, url: str, **kwargs: Any) -> tuple[int, bytes]:
        calls.append((method, url))
        return 404, b"{}"

    monkeypatch.setattr(intel_module, "http_json", fake_http)
    engine = engine_for(tmp_path, lookup="hash", virustotal_api_key="key", max_lookups=2)

    run(engine, medium)

    assert len(calls) == 2


def test_a_meta_defender_answer_is_parsed(
    tmp_path: Path, calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "setup.exe").write_bytes(b"MZ fake")

    def fake_http(method: str, url: str, **kwargs: Any) -> tuple[int, bytes]:
        calls.append((method, url))
        return 200, json.dumps(md_payload(detected=1)).encode()

    monkeypatch.setattr(intel_module, "http_json", fake_http)
    engine = engine_for(tmp_path, lookup="hash", metadefender_api_key="key")

    findings = run(engine, medium)

    assert len(findings) == 1
    assert "metadefender: 1/27" in findings[0].detail
    assert findings[0].severity is Severity.HIGH


def test_an_expired_key_is_reported_and_the_provider_is_dropped(
    tmp_path: Path, calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    for index in range(3):
        (medium / f"file-{index}.txt").write_text(f"content {index}")

    def fake_http(method: str, url: str, **kwargs: Any) -> tuple[int, bytes]:
        calls.append((method, url))
        return 401, b"{}"

    monkeypatch.setattr(intel_module, "http_json", fake_http)
    engine = engine_for(tmp_path, lookup="hash", virustotal_api_key="key")

    assert run(engine, medium) == []
    assert len(calls) == 1, "the provider must not be asked again"
    assert any("not used" in note for note in engine.notes)


# -- uploads ---------------------------------------------------------------
class FakeUploader:
    """The part of the VirusTotal provider that upload mode uses."""

    name = "virustotal"
    label = "VirusTotal"

    def __init__(self, detections: int = 4) -> None:
        self.detections = detections
        self.uploads: list[str] = []

    def lookup(self, digest: str) -> Answer | None:
        return None

    def upload(self, path: str) -> str:
        self.uploads.append(path)
        return "analysis-1"

    def analysis(self, analysis_id: str, *, deadline: float) -> Answer:
        return Answer(
            provider="virustotal",
            found=True,
            detections=self.detections,
            total=70,
            names=["Win32.Trojan.Uploaded"],
            link="https://www.virustotal.com/gui/file/x",
        )


def test_upload_needs_the_second_switch(tmp_path: Path, calls: list) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "setup.exe").write_bytes(b"MZ fake")
    uploader = FakeUploader()
    engine = engine_for(tmp_path, lookup="upload", virustotal_api_key="key")
    engine.providers = [uploader]

    assert run(engine, medium) == []
    assert uploader.uploads == []


def test_upload_mode_reports_what_left_the_machine(tmp_path: Path) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    sample = medium / "setup.exe"
    sample.write_bytes(b"MZ fake")
    uploader = FakeUploader()
    engine = engine_for(
        tmp_path,
        lookup="upload",
        upload_unknowns=True,
        virustotal_api_key="key",
    )
    engine.providers = [uploader]

    findings = run(engine, medium)

    assert uploader.uploads == [str(sample)]
    assert [finding.threat for finding in findings] == ["reputation: Win32.Trojan.Uploaded"]
    assert engine.uploaded_count() == 1
    assert any("uploaded 1 file(s)" in note for note in engine.notes)


def test_an_uploaded_clean_file_produces_no_finding(tmp_path: Path) -> None:
    medium = tmp_path / "usb"
    medium.mkdir()
    (medium / "setup.exe").write_bytes(b"MZ fake")
    engine = engine_for(
        tmp_path, lookup="upload", upload_unknowns=True, virustotal_api_key="key"
    )
    engine.providers = [FakeUploader(detections=0)]

    assert run(engine, medium) == []


# -- providers -------------------------------------------------------------
def test_virustotal_parses_a_detection() -> None:
    provider = VirusTotal("key")
    answer = provider._answer(200, json.dumps(vt_payload(malicious=3, suspicious=1)).encode())

    assert answer.found is True
    assert answer.detections == 4
    assert answer.names[0] == "Win32.Trojan.Fake"


def test_meta_defender_parses_a_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        intel_module,
        "http_json",
        lambda *args, **kwargs: (200, json.dumps(md_payload(detected=2)).encode()),
    )

    answer = MetaDefender("key").lookup("a" * 64)

    assert answer is not None
    assert answer.detections == 2
    assert answer.total == 27
    assert answer.names == ["EICAR-Test-File"]


# -- status ----------------------------------------------------------------
def test_hash_only_mode_says_so(tmp_path: Path) -> None:
    engine = engine_for(tmp_path, lookup="hash", virustotal_api_key="key")

    status = engine.status()

    assert any("hash-only" in warning for warning in status.warnings)
    assert "lookups: hash" in status.detail


def test_upload_mode_warns_about_publishing(tmp_path: Path) -> None:
    engine = engine_for(tmp_path, lookup="upload", upload_unknowns=True, virustotal_api_key="k")

    assert any("upload mode is on" in warning for warning in engine.status().warnings)
