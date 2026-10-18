# Extending DCS

How to add a detection rule, a scan engine, an upload host, a report format or a pipeline
stage, and what the tests expect from each of them.

[Back to the README](../README.md)

**Adding an upload provider.** Implement the `UploadProvider` protocol from
`src/dcs/core/upload/base.py` (`provider_id`, `label`, `requires_key`,
`max_bytes`, `availability()` and `upload()`), subclass `BaseProvider` for the
retry, timing and result plumbing, then register it in
`src/dcs/core/upload/registry.py`. If it is an ordinary "one POST, one URL" host,
all you need is a `SimpleHostSpec` entry in `SIMPLE_HOSTS` in
`src/dcs/core/upload/simple.py`. Record in the spec how the endpoint was verified
and when.

**Adding a scan engine.** Implement the `ScanEngine` protocol from
`src/dcs/core/scanner/base.py` (`engine_id`, `label`, `status()` and
`scan_batch()`), then add the engine to `Scanner.build_engines()` in
`src/dcs/core/scanner/__init__.py` and to `ENGINE_IDS` in
`src/dcs/core/config.py`. An engine yields `Finding` objects and reports
progress through `ctx.tick_file()`; it must never raise out of `scan_batch`.

There is a second shape for engines that want the whole root at once, or that
talk to a network service per run: the `TreeScanEngine` protocol with
`scan_tree(root, ctx)`. YARA (which compiles its rules once) and the hash
intelligence engine (one HTTP client and one answer cache per run) use it, and
the scanner runs them in their own thread beside the batch engines, collecting
findings through a queue. Three rules come with that shape: do not call
`ctx.tick_file()` (the batch engines own the progress counters), stop as soon
as `ctx.cancelled` is set, and put anything the report must say loudly - a rule
set that did not compile, an API key that was refused - into `self.notes`,
which the scanner turns into result warnings. An engine that could not do its
job must never leave the scan looking clean: `available = False` in
`status()` and a warning is the contract.

**Sending data off the machine.** A new engine that leaves the host needs the
same two-switch consent the reputation lookups use: one switch for the mode
(off by default) and a second, explicitly worded one for sending file
*contents*, with every sent path named in the result. The tests in
`tests/test_scanner_intel.py` show the pattern.

**Adding a detection rule.** The built-in engine is data driven: extension sets,
indicator tables and regexes live in `src/dcs/core/scanner/rules.py`, and the logic
that consumes them is in `src/dcs/core/scanner/heuristic.py`. A new rule is a new
table entry plus a test that reproduces the evidence it relies on.

**Adding a stage.** See [docs/ARCHITECTURE.md](ARCHITECTURE.md) for the stage
contract, the event sink and the exact skip rules.

---

