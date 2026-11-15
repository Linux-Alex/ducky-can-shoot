#!/usr/bin/env python3
"""The benchmark harness: measure before rewriting anything.

The roadmap refuses to move a hot path out of Python until a measurement says
the path matters.  This script is that measurement:

    python3 scripts/bench.py generate --out /var/tmp/dcs-corpus
    python3 scripts/bench.py enumerate --corpus /var/tmp/dcs-corpus
    python3 scripts/bench.py scan --corpus /var/tmp/dcs-corpus --engines heuristic
    python3 scripts/bench.py scan --corpus /var/tmp/dcs-corpus --engines clamav
    python3 scripts/bench.py job --corpus /var/tmp/dcs-corpus --json
    python3 scripts/bench.py all --corpus /var/tmp/dcs-corpus --json

The corpus is generated from a fixed seed, so two machines measure the same
bytes.  Its composition is described next to the numbers in
docs/benchmarks.md; a number without the machine and the corpus is not a
measurement, it is an anecdote.

Every command prints a table and, with --json, one machine readable object.
Peak memory is the peak RSS of this process (`ru_maxrss`), which is what a
scan of a very large medium has to keep flat.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import random
import resource
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

#: fixed so the corpus is reproducible on every machine
SEED = 20260203

#: sizes are deliberately simple: the point is a shape, not a specific disk
SMALL_FILES = 5000
DEEP_LEVELS = 12
BIG_FILES = (8 * 1024 * 1024, 32 * 1024 * 1024)

TEXT_CHUNK = (
    "The quick brown fox jumps over the lazy dog. "
    "Nothing in this line is malicious; it is filler for a benchmark corpus. "
)


@dataclass
class Result:
    """One measured step."""

    name: str
    seconds: float
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.name:<32} {self.seconds:8.3f} s"


def peak_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS bytes; this project is Linux only, but be kind
    return usage / 1024.0 if sys.platform != "darwin" else usage / (1024.0 * 1024.0)


def machine() -> dict[str, Any]:
    """Everything a reader needs to judge the numbers."""
    info: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpuCount": os.cpu_count(),
    }
    with contextlib.suppress(OSError, StopIteration):
        info["cpuModel"] = next(
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines()
            if line.startswith("model name")
        )
    for tool in ("7z", "zip", "clamscan", "clamdscan", "yara"):
        info[f"tool:{tool}"] = shutil.which(tool) or ""
    try:
        stat = os.statvfs(str(Path.home()))
        info["freeGB"] = round(stat.f_bavail * stat.f_frsize / 1024**3, 1)
    except OSError:
        pass
    return info


# ---------------------------------------------------------------------------
# the corpus
# ---------------------------------------------------------------------------


def generate_corpus(root: Path, *, seed: int = SEED) -> dict[str, Any]:
    """Write a documented, reproducible tree and describe what it contains."""
    if root.exists():
        shutil.rmtree(root)
    # the seed is recorded in the description, not used to randomise content:
    # two machines must write byte-identical trees
    random.Random(seed)
    root.mkdir(parents=True)

    # many small files: worst case for the walker's per-entry overhead
    small = root / "small"
    small.mkdir()
    for index in range(SMALL_FILES):
        bucket = small / f"b{index % 100:03d}"
        bucket.mkdir(exist_ok=True)
        (bucket / f"file-{index:06d}.txt").write_text(
            TEXT_CHUNK * (1 + index % 3), encoding="utf-8"
        )

    # a deep hierarchy: path handling and recursion
    deep = root / "deep"
    cursor = deep
    for level in range(DEEP_LEVELS):
        cursor = cursor / f"level-{level:02d}"
        cursor.mkdir(parents=True)
        (cursor / "note.md").write_text(f"level {level}\n", encoding="utf-8")

    # a few large files: streaming and hashing
    large = root / "large"
    large.mkdir()
    for index, size in enumerate(BIG_FILES):
        with (large / f"large-{index}.bin").open("wb") as handle:
            remaining = size
            block = os.urandom(1024 * 1024)
            while remaining > 0:
                chunk = block[: min(len(block), remaining)]
                handle.write(chunk)
                remaining -= len(chunk)

    # a mixed tree: documents, archives and executables
    mixed = root / "mixed"
    mixed.mkdir()
    (mixed / "report.pdf").write_bytes(
        b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
    )
    (mixed / "notes.txt").write_text(TEXT_CHUNK * 40, encoding="utf-8")
    (mixed / "script.ps1").write_text("Write-Output 'nothing to see here'\n", encoding="utf-8")
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("inner.txt", TEXT_CHUNK * 20)
    (mixed / "bundle.zip").write_bytes(buffer.getvalue())

    return describe_corpus(root)


def describe_corpus(root: Path) -> dict[str, Any]:
    files = 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            files += 1
            with contextlib.suppress(OSError):
                total += path.stat().st_size
    return {"root": str(root), "files": files, "bytes": total, "seed": SEED}


# ---------------------------------------------------------------------------
# the measurements
# ---------------------------------------------------------------------------


def measure_enumerate(root: Path) -> Result:
    from dcs.core.util import walk_files

    started = time.perf_counter()
    files = 0
    total = 0
    for entry in walk_files(str(root), include_hidden=True):
        files += 1
        total += entry.size
    elapsed = time.perf_counter() - started
    return Result("enumerate (walk_files)", elapsed, {"files": files, "bytes": total})


def measure_scan(root: Path, engines: list[str]) -> Result:
    from dcs.core.config import ScanConfig
    from dcs.core.scanner import Scanner

    config = ScanConfig()
    config.engines = engines
    started = time.perf_counter()
    result = Scanner(config).run(str(root))
    elapsed = time.perf_counter() - started
    return Result(
        "scan (" + ", ".join(engines) + ")",
        elapsed,
        {
            "verdict": result.verdict.value,
            "files": result.stats.files_scanned,
            "bytes": result.stats.bytes_scanned,
            "findings": len(result.findings),
        },
    )


def measure_job(root: Path, *, work: Path, json_only: bool = False) -> list[Result]:
    """One whole job with the network stages switched off."""
    from dcs.core.config import AppConfig
    from dcs.core.pipeline import JobRequest, Pipeline

    config = AppConfig()
    config.general.output_dir = str(work / "out")
    config.report.directory = str(work / "reports")
    config.history.database = str(work / "history.db")
    config.upload.enabled = False
    config.email.enabled = False
    config.scan.engines = ["heuristic"]
    config.archive.compression_level = 1

    work.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    result = Pipeline(config).run(
        JobRequest(source_path=str(root), label="bench", do_upload=False, do_email=False)
    )
    elapsed = time.perf_counter() - started
    timings = {
        timing.stage: round(timing.seconds, 3) for timing in result.timings
    }
    return [
        Result(
            "whole job",
            elapsed,
            {
                "verdict": result.verdict.value,
                "files": result.files_scanned,
                "bytes": result.bytes_scanned,
                "stageSeconds": timings,
                "archiveBytes": result.archive_bytes,
            },
        )
    ]


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def emit(results: list[Result], *, as_json: bool) -> None:
    for result in results:
        print(result.text)
        if result.detail:
            print(f"  {json.dumps(result.detail, sort_keys=True)}")
    if as_json:
        print(
            json.dumps(
                {
                    "machine": machine(),
                    "peakRssMb": round(peak_rss_mb(), 1),
                    "results": [asdict(result) for result in results],
                },
                indent=2,
                sort_keys=True,
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["generate", "enumerate", "scan", "job", "all"],
        help="what to measure",
    )
    parser.add_argument("--corpus", default="/var/tmp/dcs-corpus", help="corpus directory")
    parser.add_argument("--out", default="", help="where 'generate' writes (default: --corpus)")
    parser.add_argument("--work", default="", help="scratch directory for 'job' (default: corpus/../dcs-bench-work)")
    parser.add_argument("--engines", default="heuristic", help="comma separated engine ids")
    parser.add_argument("--json", action="store_true", help="also print one JSON object")
    parser.add_argument("--seed", type=int, default=SEED, help="corpus seed")
    args = parser.parse_args(argv)

    corpus = Path(args.corpus).expanduser()
    results: list[Result] = []

    if args.action in ("generate", "all"):
        root = Path(args.out or args.corpus).expanduser()
        print(f"generating the corpus under {root} (seed {args.seed})")
        started = time.perf_counter()
        description = generate_corpus(root, seed=args.seed)
        results.append(
            Result("generate corpus", time.perf_counter() - started, description)
        )

    if args.action in ("enumerate", "all"):
        if not corpus.exists():
            print(f"error: {corpus} does not exist; run 'generate' first", file=sys.stderr)
            return 2
        results.append(measure_enumerate(corpus))

    if args.action in ("scan", "all"):
        if not corpus.exists():
            print(f"error: {corpus} does not exist; run 'generate' first", file=sys.stderr)
            return 2
        engines = [part.strip() for part in args.engines.split(",") if part.strip()]
        results.append(measure_scan(corpus, engines))

    if args.action in ("job", "all"):
        if not corpus.exists():
            print(f"error: {corpus} does not exist; run 'generate' first", file=sys.stderr)
            return 2
        work = Path(args.work).expanduser() if args.work else corpus.parent / "dcs-bench-work"
        results.extend(measure_job(corpus, work=work, json_only=args.json))

    emit(results, as_json=args.json)
    return 0


if __name__ == "__main__":  # pragma: no cover - a manual tool
    raise SystemExit(main())
