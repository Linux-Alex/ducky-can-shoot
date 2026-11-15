# Benchmarks

The roadmap refuses to move a hot path out of Python before a measurement says
that the path matters. This file is where those measurements live: the corpus
that makes them reproducible, the commands that produce them, and a baseline
table that is filled in by whoever runs them - not by guesswork.

**No number belongs in this file without the machine and the corpus it was
measured on.** A benchmark is a claim about one machine running one tree, and a
reader has to be able to decide whether that machine is like theirs.

## The corpus

`scripts/bench.py generate` writes a tree from a fixed seed, so every machine
measures the same bytes. The composition is deliberate:

| Part | What it stresses |
| --- | --- |
| 5 000 small text files in 100 buckets (`small/`) | The per-entry cost of the walker and the per-file overhead of the engines: the case where Python matters most. |
| A 12-level deep hierarchy (`deep/`) | Path construction, relative path handling and directory recursion. |
| Two incompressible files, 8 MiB and 32 MiB (`large/`) | Streaming, hashing and the archive stage; memory must not grow with the file. |
| A mixed tree (`mixed/`) | A PDF, a text document, a PowerShell script, an OLE-free zip: one file of each kind the heuristic engine treats differently. |

The generator reports exactly what it wrote, and the command must be run with
the same `--seed` for the numbers to be comparable:

```bash
python3 scripts/bench.py generate --out /var/tmp/dcs-corpus
```

## The machine

Record these next to every result. `scripts/bench.py --json` prints them:

- CPU model and core count, RAM,
- the filesystem and the disk (an SSD and a spinning disk are different tools),
- Python version, ClamAV version and signature age, `7z`/`zip` versions,
- the kernel, and whether the tree was in the page cache (a second run of
  `enumerate` is much faster than the first, which is why each measurement is
  quoted with its run number).

## The measurements

Each command prints a table and, with `--json`, one object with the machine and
the peak RSS of the process.

```bash
# 1. enumeration: the walker alone
python3 scripts/bench.py enumerate --corpus /var/tmp/dcs-corpus

# 2. the heuristic engine alone (no ClamAV, no network)
python3 scripts/bench.py scan --corpus /var/tmp/dcs-corpus --engines heuristic

# 3. the signature engine alone
python3 scripts/bench.py scan --corpus /var/tmp/dcs-corpus --engines clamav

# 4. both engines, which is what most runs do
python3 scripts/bench.py scan --corpus /var/tmp/dcs-corpus --engines clamav,heuristic

# 5. a whole job: scan, archive, report, history; no upload, no email
python3 scripts/bench.py job --corpus /var/tmp/dcs-corpus --json
```

Run each at least three times, quote the spread, and run the first two on a
fully cached tree and on a freshly dropped cache
(`sync; echo 3 | sudo tee /proc/sys/vm/drop_caches`) - the difference is the
share of the work that is disk rather than Python.

### Interface responsiveness

Not scriptable, so it is recorded by hand:

1. Build a large corpus (raise `SMALL_FILES` in `scripts/bench.py`, or copy the
   `small/` tree until it holds several hundred thousand files).
2. Start `dcs gui`, select the corpus as a folder and start the job.
3. While the scan runs, scroll the live view, switch tabs and open the settings
   page. Note whether the progress bar and the counters move smoothly.
4. Note the frame time `general.ui_refresh_ms` was set to, and whether lowering
   or raising it changed anything.

The question this answers is exactly the roadmap's gate B7: is the interface the
bottleneck at the configured refresh rate? If it is not, the C++ front end is
not justified by the interface.

## The gate

The migration only starts if a measurement says it should. These thresholds are
agreed before measuring, not after:

| Benchmark | Question | Gate for continuing |
| --- | --- | --- |
| B1 enumerate | How much of a scan is enumeration plus stat? | Port the walker only if it is a meaningful share of total wall time on the small-file corpus; a few percent is not. |
| B2 hash | Is hashing disk bound or Python bound? | If a native extension moves less than a tenth of the gap between the measured rate and the raw disk rate, keep Python. |
| B3 heuristic matching | How much of the heuristic engine is regex work? | Port the matchers only for the file kinds that dominate the time; measure per kind. |
| B4 end to end | What does a full scan gain? | If the end-to-end gain is small, stop: the complexity is not paid for. |
| B5 memory | Does peak RSS stay flat on a huge tree? | Any regression blocks the migration; streaming is a feature. |
| B6 upload | Is there Python overhead left in the upload path? | Compare against a raw upload of the same file; within noise means leave the adapters in Python. |
| B7 interface | Is the interface the bottleneck at `general.ui_refresh_ms`? | Only a real problem justifies a C++ front end. |

## Baseline

Fill this in on the reference machine and commit it with the date. Until a row
is measured it stays empty: an invented number is worse than no number.

| Measurement | Run 1 | Run 2 | Run 3 | Notes (cached / cold) |
| --- | --- | --- | --- | --- |
| enumerate | | | | |
| scan, heuristic | | | | |
| scan, ClamAV | | | | |
| scan, both | | | | |
| whole job | | | | |
| peak RSS (whole job) | | | | |
| interface during the scan | | | | |

**Reference machine (fill in):**

| | |
| --- | --- |
| CPU | |
| RAM | |
| Disk / filesystem | |
| Python | |
| ClamAV (version, signature age) | |
| Date | |
