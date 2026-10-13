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
`src/dcs/core/scanner/__init__.py` and to the engine ids the configuration
validation accepts. An engine yields `Finding` objects and reports progress
through `ctx.tick_file(); it must never raise out of `scan_batch`.

**Adding a detection rule.** The built-in engine is data driven: extension sets,
indicator tables and regexes live in `src/dcs/core/scanner/rules.py`, and the logic
that consumes them is in `src/dcs/core/scanner/heuristic.py`. A new rule is a new
table entry plus a test that reproduces the evidence it relies on.

**Adding a stage.** See [docs/ARCHITECTURE.md](ARCHITECTURE.md) for the stage
contract, the event sink and the exact skip rules.

---

