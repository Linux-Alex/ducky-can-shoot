"""Tests for dcs.core.config: defaults, layering, serialisation and secrets.

The autouse fixture in conftest.py clears every DCS_* variable and points the
XDG directories at a temporary directory, and each load_config call below
passes system_path explicitly, so neither the developer's config nor the real
/etc/dcs/config.toml can influence a result.
"""

from __future__ import annotations

import logging
import shlex
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from dcs.core.config import (
    WATCH_MODES,
    AppConfig,
    default_config_path,
    default_data_dir,
    default_reports_dir,
    default_work_dir,
    dumps_toml,
    load_config,
    option_paths,
    set_option,
)


def python_command(source: str) -> str:
    """Shell command string running *source* with the current interpreter."""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def sane_config() -> AppConfig:
    """An AppConfig that validation_problems() must accept without complaint."""
    config = AppConfig()
    # email is off until a server and a recipient are configured, so a config
    # that *uses* email has to ask for it
    config.email.enabled = True
    config.email.smtp_host = "smtp.example.test"
    config.email.from_addr = "dcs@example.test"
    config.email.to_addrs = ["ops@example.test"]
    return config


# ---------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------


def test_defaults() -> None:
    config = AppConfig()

    assert config.scan.engines == ["clamav", "heuristic"]
    assert config.scan.blocking_severity == "high"
    assert config.scan.max_file_size_mb == 4096
    assert config.scan.exclude_globs == [
        "System Volume Information/*",
        "$RECYCLE.BIN/*",
        "*.tmp",
    ]
    assert config.archive.format == "zip"
    assert config.archive.when == "clean"
    assert config.archive.compression_level == 6
    assert config.upload.provider == "gofile"
    assert config.upload.upload_when == "clean"
    assert config.upload.enabled is True
    assert config.email.security == "starttls"
    assert config.email.smtp_port == 587
    assert config.email.subject_template == "[DCS] {verdict} - {device} ({files} files)"
    # off by default: an installation without an SMTP server and a recipient
    # cannot send anything, and the settings page would open with a warning and
    # a disabled Save button
    assert config.email.enabled is False
    assert config.report.format == "html"
    assert config.report.directory == ""
    assert config.gui.window_width == 1180
    assert config.gui.remember_window_size is True
    assert config.watch.mode == "off"
    assert config.watch.interval_s == 3.0
    assert config.general.output_dir == ""
    assert config.source_path == ""


def test_serialised_section_keys_are_exactly_the_documented_ones() -> None:
    """The config schema does not grow dead or duplicate keys."""
    data = AppConfig().to_dict()

    assert set(data) == {
        "general",
        "scan",
        "archive",
        "upload",
        "email",
        "report",
        "gui",
        "watch",
    }
    assert set(data["general"]) == {
        "confirm_before_upload",
        "keep_archive",
        "output_dir",
        "ui_refresh_ms",
    }
    assert set(data["gui"]) == {
        "auto_select_new_device",
        "remember_window_size",
        "show_log_pane",
        "switch_to_result",
        "window_height",
        "window_width",
    }
    assert set(data["watch"]) == {
        "autostart",
        "ignore_existing",
        "interval_s",
        "mode",
        "notify_timeout_ms",
    }
    # general.language and gui.confirm_upload were removed on purpose
    assert "language" not in data["general"]
    assert "confirm_upload" not in data["gui"]
    assert "theme" not in data["gui"]


def test_upload_sections_match_the_host_table() -> None:
    """Every simple host has its own section; the old zeroxzero key is gone."""
    config = AppConfig()

    for provider_id in ("x0at", "tempsh", "catbox", "tmpfiles", "litterbox"):
        section = getattr(config.upload, provider_id)
        assert section.base_url == ""

    assert not hasattr(config.upload, "zeroxzero")
    # pixeldrain used to default to https://pixeldrain.com/api, which made the
    # override impossible to distinguish from "not configured"
    assert config.upload.pixeldrain.base_url == ""


def test_default_instances_do_not_share_mutable_state() -> None:
    first, second = AppConfig(), AppConfig()

    first.scan.engines.append("extra")
    first.email.to_addrs.append("ops@example.test")

    assert second.scan.engines == ["clamav", "heuristic"]
    assert second.email.to_addrs == []


# ---------------------------------------------------------------------------
# derived paths
# ---------------------------------------------------------------------------


def test_derived_paths_use_configured_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    config = AppConfig()
    config.general.output_dir = str(tmp_path / "out")
    config.report.directory = str(tmp_path / "reports")

    assert config.output_dir == tmp_path / "out"
    assert config.reports_dir == tmp_path / "reports"
    assert config.work_dir == tmp_path / "xdg-data" / "dcs" / "work"


def test_derived_paths_expand_a_leading_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/dcs-xdg-unused")
    config = AppConfig()
    config.general.output_dir = "~/dcs-out"
    config.report.directory = "~/dcs-reports"

    assert config.output_dir == Path.home() / "dcs-out"
    assert config.reports_dir == Path.home() / "dcs-reports"


def test_derived_paths_fall_back_to_xdg_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_home = tmp_path / "xdg-data"
    config_home = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    config = AppConfig()

    assert config.general.output_dir == ""
    assert config.output_dir == data_home / "dcs"
    assert config.reports_dir == data_home / "dcs" / "reports"
    assert config.work_dir == data_home / "dcs" / "work"
    assert config.output_dir == default_data_dir()
    assert config.reports_dir == default_reports_dir()
    assert config.work_dir == default_work_dir()
    assert default_config_path() == config_home / "dcs" / "config.toml"


def test_derived_paths_fall_back_to_home_when_xdg_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    config = AppConfig()

    assert config.output_dir == Path.home() / ".local" / "share" / "dcs"
    assert config.reports_dir == Path.home() / ".local" / "share" / "dcs" / "reports"
    assert config.work_dir == Path.home() / ".local" / "share" / "dcs" / "work"
    assert default_config_path() == Path.home() / ".config" / "dcs" / "config.toml"


# ---------------------------------------------------------------------------
# to_dict / from_dict
# ---------------------------------------------------------------------------


def test_to_dict_from_dict_round_trip() -> None:
    config = AppConfig()
    config.general.output_dir = "/media/usb-out"
    config.general.confirm_before_upload = False
    config.scan.engines = ["heuristic"]
    config.scan.exclude_globs = ["*.bak"]
    config.archive.compression_level = 9
    config.upload.gofile.folder_prefix = "TEST"
    config.email.to_addrs = ["a@example.test", "b@example.test"]
    config.report.max_findings = 42

    data = config.to_dict()
    rebuilt = AppConfig.from_dict(data)

    assert rebuilt == config
    assert isinstance(data, dict)
    assert data["scan"]["engines"] == ["heuristic"]
    assert data["upload"]["gofile"]["folder_prefix"] == "TEST"
    assert data["report"]["max_findings"] == 42


def test_to_dict_removes_source_path() -> None:
    config = AppConfig()
    config.source_path = "/etc/dcs/config.toml"

    data = config.to_dict()

    assert "source_path" not in data
    assert data["email"]["smtp_port"] == 587
    assert config.source_path == "/etc/dcs/config.toml"


def test_from_dict_ignores_unknown_keys_and_accepts_partial_dicts() -> None:
    config = AppConfig.from_dict(
        {"nonsense": 1, "scan": {"bogus": "x", "max_files": 7}, "": "empty key"}
    )

    assert config.scan.max_files == 7
    assert config.scan.engines == ["clamav", "heuristic"]
    assert not hasattr(config.scan, "bogus")

    partial = AppConfig.from_dict({"general": {"output_dir": "/media/pl"}})
    assert partial.general.output_dir == "/media/pl"
    assert partial.general.confirm_before_upload is True
    assert partial.email.smtp_port == 587
    assert partial.source_path == ""

    assert AppConfig.from_dict({}) == AppConfig()


def test_from_dict_builds_nested_dataclasses() -> None:
    config = AppConfig.from_dict(
        {"upload": {"gofile": {"folder_prefix": "NESTED"}, "retries": 9}}
    )

    assert config.upload.gofile.folder_prefix == "NESTED"
    assert config.upload.gofile.token == ""
    assert config.upload.retries == 9
    # every simple host has its own base URL override section
    assert config.upload.pixeldrain.base_url == ""
    assert config.upload.litterbox.base_url == ""
    assert config.upload.x0at.base_url == ""


# ---------------------------------------------------------------------------
# merge / overlay
# ---------------------------------------------------------------------------


def test_merge_only_changes_the_mentioned_keys() -> None:
    config = AppConfig()

    config.merge({"email": {"smtp_host": "smtp.test", "to_addrs": ["ops@test"]}})

    assert config.email.smtp_host == "smtp.test"
    assert config.email.to_addrs == ["ops@test"]
    assert config.email.smtp_port == 587
    assert config.email.security == "starttls"


def test_merge_deeply_merges_nested_sections() -> None:
    config = AppConfig()

    config.merge({"upload": {"gofile": {"folder_prefix": "DEEPMERGE"}, "retries": 4}})

    assert config.upload.gofile.folder_prefix == "DEEPMERGE"
    assert config.upload.gofile.token_command == ""
    assert config.upload.retries == 4
    assert config.upload.provider == "gofile"


def test_merge_ignores_unknown_keys() -> None:
    config = AppConfig()

    config.merge({"nope": {"a": 1}, "email": {"unknown_key": "x"}})

    assert not hasattr(config, "nope")
    assert not hasattr(config.email, "unknown_key")
    assert config.to_dict() == AppConfig().to_dict()


def test_merge_coerces_string_values() -> None:
    config = AppConfig()

    config.merge(
        {
            "email": {"smtp_port": "2525", "insecure_tls": "true", "timeout_s": "30"},
            "general": {"confirm_before_upload": "true", "ui_refresh_ms": "250"},
            "archive": {"min_free_space_ratio": "2.5"},
            "upload": {"enabled": "false"},
        }
    )

    assert config.email.smtp_port == 2525
    assert isinstance(config.email.smtp_port, int)
    assert config.email.insecure_tls is True
    assert config.email.timeout_s == 30
    assert config.general.confirm_before_upload is True
    assert config.general.ui_refresh_ms == 250
    assert config.archive.min_free_space_ratio == 2.5
    assert isinstance(config.archive.min_free_space_ratio, float)
    assert config.upload.enabled is False


def test_merge_coerces_common_boolean_spellings() -> None:
    for raw, expected in (("yes", True), ("1", True), ("on", True), ("TRUE", True), ("no", False), ("off", False), ("0", False)):
        config = AppConfig()
        config.merge({"general": {"confirm_before_upload": raw}})
        assert config.general.confirm_before_upload is expected


# ---------------------------------------------------------------------------
# TOML serialisation
# ---------------------------------------------------------------------------


def test_dumps_toml_is_parseable_and_keeps_types(tmp_path: Path) -> None:
    config = AppConfig()
    config.general.output_dir = "/media/usb-out"
    config.general.confirm_before_upload = False
    config.email.from_name = 'Ducky "Can" Shoot \\ Path'
    config.email.cc_addrs = ["cc1@example.test", "cc2@example.test"]
    config.email.to_addrs = []
    config.scan.exclude_globs = []
    config.upload.retries = 5
    config.archive.min_free_space_ratio = 1.25

    text = dumps_toml(config.to_dict())
    parsed = tomllib.loads(text)

    assert parsed["general"]["output_dir"] == "/media/usb-out"
    assert parsed["general"]["confirm_before_upload"] is False
    assert parsed["general"]["keep_archive"] is True
    assert parsed["email"]["from_name"] == 'Ducky "Can" Shoot \\ Path'
    assert parsed["email"]["cc_addrs"] == ["cc1@example.test", "cc2@example.test"]
    assert parsed["email"]["to_addrs"] == []
    assert parsed["scan"]["exclude_globs"] == []
    assert parsed["upload"]["retries"] == 5
    assert parsed["archive"]["min_free_space_ratio"] == 1.25
    assert parsed["upload"]["gofile"]["folder_prefix"] == "DCS"


def test_dumps_toml_round_trips_through_a_real_file(tmp_path: Path) -> None:
    config = AppConfig()
    config.general.output_dir = "/media/usb-out"
    config.email.from_name = 'Ducky "Can" Shoot \\ Path'
    config.email.cc_addrs = ["cc@example.test"]
    config.email.to_addrs = []
    config.scan.engines = ["heuristic"]
    config.upload.retries = 5
    config.archive.min_free_space_ratio = 1.25
    config.gui.window_width = 1500

    path = tmp_path / "config.toml"
    path.write_text(dumps_toml(config.to_dict()), encoding="utf-8")
    with open(path, "rb") as handle:
        loaded = tomllib.load(handle)

    assert AppConfig.from_dict(loaded) == config


def test_save_writes_the_file_with_private_permissions(tmp_path: Path) -> None:
    config = AppConfig()
    config.general.output_dir = str(tmp_path / "out")
    target = tmp_path / "nested" / "config.toml"

    assert config.save(target) == str(target)

    assert target.is_file()
    assert config.source_path == str(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not (target.parent / "config.toml.tmp").exists()
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Ducky Can Shoot configuration")
    assert tomllib.loads(text)["general"]["output_dir"] == str(tmp_path / "out")


def test_save_and_load_config_round_trip(tmp_path: Path) -> None:
    config = AppConfig()
    config.general.output_dir = str(tmp_path / "out")
    config.report.directory = str(tmp_path / "reports")
    config.email.smtp_host = "smtp.example.test"
    config.email.from_addr = "dcs@example.test"
    config.email.to_addrs = ["ops@example.test"]
    config.scan.engines = ["heuristic"]
    target = tmp_path / "config.toml"

    config.save(target)
    loaded = load_config(target, system_path=None)

    assert loaded == config
    assert loaded.source_path == str(target)
    assert loaded.output_dir == config.output_dir


# ---------------------------------------------------------------------------
# load_config layering
# ---------------------------------------------------------------------------


def test_load_config_applies_the_system_file_over_the_defaults(tmp_path: Path) -> None:
    system = tmp_path / "system.toml"
    system.write_text('[scan]\nmax_files = 11\n[archive]\nwhen = "always"\n', encoding="utf-8")

    config = load_config(system_path=system)

    assert config.scan.max_files == 11
    assert config.archive.when == "always"
    assert config.source_path == str(default_config_path())


def test_load_config_config_file_wins_over_the_system_file_and_the_defaults(tmp_path: Path) -> None:
    system = tmp_path / "system.toml"
    system.write_text(
        '[upload]\nprovider = "pixeldrain"\nretries = 5\n[report]\nformat = "json"\n',
        encoding="utf-8",
    )
    user = tmp_path / "user.toml"
    user.write_text(
        '[upload]\nprovider = "tmpfiles"\n[report]\nformat = "text"\n[gui]\nwindow_width = 1500\n',
        encoding="utf-8",
    )

    config = load_config(user, system_path=system)

    assert config.upload.provider == "tmpfiles"  # --config beats the system file
    assert config.upload.retries == 5  # the system value survives
    assert config.report.format == "text"  # --config beats the system file
    assert config.gui.window_width == 1500  # --config beats the built-in default
    assert config.upload.upload_when == "clean"  # untouched built-in default
    assert config.source_path == str(user)


def test_load_config_missing_config_file_warns_and_keeps_defaults(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing = tmp_path / "absent.toml"

    with caplog.at_level(logging.WARNING, logger="dcs.config"):
        config = load_config(missing, system_path=None)

    assert config.to_dict() == AppConfig().to_dict()
    assert config.source_path == str(missing)
    assert any("does not exist" in record.getMessage() for record in caplog.records)


def test_load_config_ignores_a_broken_config_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text("[scan\nmax_files = ", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="dcs.config"):
        config = load_config(broken, system_path=None)

    assert config.to_dict() == AppConfig().to_dict()
    assert any(record.levelno == logging.ERROR for record in caplog.records)


# ---------------------------------------------------------------------------
# environment overrides
# ---------------------------------------------------------------------------


def test_environment_overrides_win_over_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user = tmp_path / "user.toml"
    user.write_text(
        "\n".join(
            [
                "[upload]",
                'provider = "gofile"',
                'upload_when = "never"',
                "",
                "[email]",
                'smtp_host = "file.example.test"',
                "smtp_port = 25",
                'from_addr = "file@example.test"',
                'to_addrs = ["file@example.test"]',
                "",
                "[scan]",
                'engines = ["heuristic"]',
                "",
                "[general]",
                'output_dir = "/file-output"',
                "",
                "[report]",
                'directory = "/file-reports"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    archive_dir = tmp_path / "env-archive"
    report_dir = tmp_path / "env-reports"
    monkeypatch.setenv("DCS_PROVIDER", "pixeldrain")
    monkeypatch.setenv("DCS_UPLOAD_WHEN", "always")
    monkeypatch.setenv("DCS_SMTP_HOST", "env.example.test")
    monkeypatch.setenv("DCS_SMTP_PORT", "2525")
    monkeypatch.setenv("DCS_SMTP_FROM", "env@example.test")
    monkeypatch.setenv("DCS_SMTP_TO", " a@example.test ,, b@example.test ")
    monkeypatch.setenv("DCS_ENGINES", "clamav, heuristic")
    monkeypatch.setenv("DCS_ARCHIVE_DIR", str(archive_dir))
    monkeypatch.setenv("DCS_REPORT_DIR", str(report_dir))

    config = load_config(user, system_path=None)

    assert config.upload.provider == "pixeldrain"
    assert config.upload.upload_when == "always"
    assert config.email.smtp_host == "env.example.test"
    assert config.email.smtp_port == 2525
    assert isinstance(config.email.smtp_port, int)
    assert config.email.from_addr == "env@example.test"
    assert config.email.to_addrs == ["a@example.test", "b@example.test"]
    assert config.scan.engines == ["clamav", "heuristic"]
    assert config.general.output_dir == str(archive_dir)
    assert config.output_dir == archive_dir
    assert config.report.directory == str(report_dir)
    assert config.reports_dir == report_dir


def test_non_numeric_smtp_port_is_ignored(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DCS_SMTP_PORT", "not-a-port")

    with caplog.at_level(logging.WARNING, logger="dcs.config"):
        config = load_config(None, system_path=None)

    assert config.email.smtp_port == 587
    assert any(
        "ignoring non numeric DCS_SMTP_PORT" in record.getMessage() for record in caplog.records
    )


def test_empty_environment_values_do_not_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DCS_PROVIDER", "")
    monkeypatch.setenv("DCS_ENGINES", "")

    config = load_config(None, system_path=None)

    assert config.upload.provider == "gofile"
    assert config.scan.engines == ["clamav", "heuristic"]


# ---------------------------------------------------------------------------
# secret resolution
# ---------------------------------------------------------------------------


def test_secret_helper_command_wins_over_the_literal_value() -> None:
    config = AppConfig()
    config.email.password = "literal"
    config.email.password_command = python_command("print('from-command')")

    assert config.smtp_password() == "from-command"


def test_secret_environment_variable_wins_over_the_helper_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig()
    config.email.password = "literal"
    config.email.password_command = python_command("print('from-command')")
    monkeypatch.setenv("DCS_SMTP_PASSWORD", "from-env")

    assert config.smtp_password() == "from-env"


def test_secret_falls_back_to_the_literal_without_a_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = AppConfig()
    config.email.password = "literal"
    monkeypatch.delenv("DCS_SMTP_PASSWORD", raising=False)

    assert config.smtp_password() == "literal"


def test_secret_command_with_a_non_zero_exit_falls_back_to_the_literal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = AppConfig()
    config.email.password = "literal"
    config.email.password_command = python_command("import sys; sys.exit(3)")

    with caplog.at_level(logging.WARNING, logger="dcs.config"):
        assert config.smtp_password() == "literal"

    assert any("failed" in record.getMessage() for record in caplog.records)


def test_secret_command_that_cannot_be_started_falls_back_to_the_literal() -> None:
    config = AppConfig()
    config.email.password = "literal"
    config.email.password_command = "/nonexistent/dcs-secret-helper --print"

    assert config.smtp_password() == "literal"


def test_secret_command_output_is_stripped() -> None:
    config = AppConfig()
    config.email.password = "literal"
    config.email.password_command = python_command("print('  spaced-secret  ')")

    assert config.smtp_password() == "spaced-secret"


def test_gofile_token_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AppConfig()
    config.upload.gofile.token = "gofile-literal"
    config.upload.gofile.token_command = python_command("print('gofile-command')")

    assert config.gofile_token() == "gofile-command"
    monkeypatch.setenv("DCS_GOFILE_TOKEN", "gofile-env")
    assert config.gofile_token() == "gofile-env"

    monkeypatch.delenv("DCS_GOFILE_TOKEN")
    config.upload.gofile.token_command = ""
    assert config.gofile_token() == "gofile-literal"


def test_pixeldrain_api_key_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AppConfig()
    config.upload.pixeldrain.api_key = "pixel-literal"
    config.upload.pixeldrain.api_key_command = python_command("print('pixel-command')")

    assert config.pixeldrain_api_key() == "pixel-command"
    monkeypatch.setenv("DCS_PIXELDRAIN_API_KEY", "pixel-env")
    assert config.pixeldrain_api_key() == "pixel-env"

    monkeypatch.delenv("DCS_PIXELDRAIN_API_KEY")
    config.upload.pixeldrain.api_key_command = ""
    assert config.pixeldrain_api_key() == "pixel-literal"


def test_smtp_username_environment_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AppConfig()
    config.email.username = "file-user"

    assert config.smtp_username() == "file-user"
    monkeypatch.setenv("DCS_SMTP_USERNAME", "env-user")
    assert config.smtp_username() == "env-user"
    monkeypatch.setenv("DCS_SMTP_USERNAME", "")
    assert config.smtp_username() == "file-user"


# ---------------------------------------------------------------------------
# masking
# ---------------------------------------------------------------------------


def test_to_dict_masks_non_empty_secrets() -> None:
    config = AppConfig()
    config.email.password = "hunter2"
    config.upload.gofile.token = "gofile-token"
    config.upload.pixeldrain.api_key = "pixel-key"

    masked = config.to_dict(mask_secrets=True)
    plain = config.to_dict()

    assert masked["email"]["password"] == "***"
    assert masked["upload"]["gofile"]["token"] == "***"
    assert masked["upload"]["pixeldrain"]["api_key"] == "***"
    assert plain["email"]["password"] == "hunter2"
    assert plain["upload"]["gofile"]["token"] == "gofile-token"
    assert plain["upload"]["pixeldrain"]["api_key"] == "pixel-key"
    # the object itself keeps the real values
    assert config.email.password == "hunter2"
    assert config.smtp_password() == "hunter2"
    assert config.gofile_token() == "gofile-token"
    assert config.pixeldrain_api_key() == "pixel-key"


def test_to_dict_leaves_empty_secrets_empty() -> None:
    config = AppConfig()

    masked = config.to_dict(mask_secrets=True)

    assert masked["email"]["password"] == ""
    assert masked["upload"]["gofile"]["token"] == ""
    assert masked["upload"]["pixeldrain"]["api_key"] == ""


def test_save_without_secrets_writes_the_masked_values(tmp_path: Path) -> None:
    config = AppConfig()
    config.email.password = "hunter2"
    config.upload.gofile.token = "gofile-token"
    target = tmp_path / "config.toml"

    config.save(target, include_secrets=False)

    text = target.read_text(encoding="utf-8")
    assert "hunter2" not in text
    assert "gofile-token" not in text
    loaded = tomllib.loads(text)
    assert loaded["email"]["password"] == "***"
    assert loaded["upload"]["gofile"]["token"] == "***"


# ---------------------------------------------------------------------------
# validation_problems
# ---------------------------------------------------------------------------


def test_validation_problems_requires_the_email_essentials() -> None:
    config = AppConfig()
    config.email.enabled = True

    assert config.validation_problems() == [
        "email.enabled is true but email.smtp_host is empty",
        "email.enabled is true but email.from_addr is empty",
        "email.enabled is true but no recipient is configured",
    ]


def test_validation_problems_accepts_a_recipient_in_any_field() -> None:
    config = sane_config()
    config.email.to_addrs = []
    config.email.cc_addrs = ["cc@example.test"]

    assert config.validation_problems() == []
    config.email.cc_addrs = []
    config.email.bcc_addrs = ["bcc@example.test"]
    assert config.validation_problems() == []


def test_validation_problems_ignores_the_email_section_when_disabled() -> None:
    config = AppConfig()
    config.email.enabled = False

    assert config.validation_problems() == []


def test_validation_problems_reports_bad_values() -> None:
    config = sane_config()
    config.scan.engines = ["nope", "heuristic", "also-nope"]
    config.archive.format = "rar"
    config.archive.when = "sometimes"
    config.upload.upload_when = "maybe"

    assert config.validation_problems() == [
        "unknown scan engine(s): nope, also-nope",
        "archive.format must be zip or 7z, not 'rar'",
        "archive.when must be clean/always/never, not 'sometimes'",
        "upload.upload_when must be clean/always/never, not 'maybe'",
    ]


def test_validation_problems_requires_at_least_one_engine() -> None:
    config = sane_config()
    config.scan.engines = []

    assert config.validation_problems() == [
        "no scan engine enabled: enable 'clamav' and/or 'heuristic'"
    ]


def test_validation_problems_reports_a_bad_report_format() -> None:
    config = sane_config()
    config.report.format = "pdf"

    assert config.validation_problems() == [
        "report.format must be html, text or both, not 'pdf'"
    ]


def test_validation_problems_reports_a_bad_email_security() -> None:
    config = sane_config()
    config.email.security = "weird"

    assert config.validation_problems() == [
        "email.security must be starttls/ssl/none, not 'weird'"
    ]


def test_validation_problems_is_empty_for_a_sane_config() -> None:
    assert sane_config().validation_problems() == []
    # a fresh installation is valid as it stands: the settings page shows no
    # warning and can save, because email is off until it is configured
    assert load_config(None, system_path=None).validation_problems() == []


# ---------------------------------------------------------------------------
# known bugs
# ---------------------------------------------------------------------------


def test_dumps_toml_escapes_control_characters_in_strings() -> None:
    config = AppConfig()
    config.email.subject_template = "line1\nline2"

    parsed = tomllib.loads(dumps_toml(config.to_dict()))

    assert parsed["email"]["subject_template"] == "line1\nline2"


def test_merge_rejects_a_non_numeric_int_value() -> None:
    config = AppConfig()

    config.merge({"email": {"smtp_port": "2525x"}})

    assert config.email.smtp_port == 587


# ---------------------------------------------------------------------------
# [watch]: what happens when a medium is plugged in
# ---------------------------------------------------------------------------


def test_the_watcher_is_off_until_it_is_asked_for() -> None:
    watch = AppConfig().watch

    assert watch.mode == "off"
    assert watch.interval_s >= 0.5
    assert watch.ignore_existing is True, "a stick from yesterday is not news"
    assert watch.autostart is False
    assert watch.notify_timeout_ms > 0


def test_the_watch_section_survives_a_round_trip() -> None:
    config = AppConfig()
    config.watch.mode = "open_run"
    config.watch.interval_s = 7.5
    config.watch.ignore_existing = False
    config.watch.autostart = True

    parsed = tomllib.loads(dumps_toml(config.to_dict()))
    back = AppConfig.from_dict(parsed)

    assert parsed["watch"]["mode"] == "open_run"
    assert back.watch == config.watch


def test_an_unknown_watch_mode_is_reported() -> None:
    config = AppConfig()
    config.watch.mode = "sometimes"

    problems = config.validation_problems()

    assert any("watch.mode" in problem and "open_run" in problem for problem in problems)


def test_a_watch_interval_that_is_too_small_is_reported() -> None:
    config = AppConfig()
    config.watch.interval_s = 0.1

    assert any("watch.interval_s" in problem for problem in config.validation_problems())


@pytest.mark.parametrize("mode", WATCH_MODES)
def test_every_watch_mode_is_accepted(mode: str) -> None:
    config = AppConfig()
    config.watch.mode = mode

    assert not [p for p in config.validation_problems() if "watch.mode" in p]


def test_the_watch_section_can_be_layered_like_any_other() -> None:
    config = AppConfig()

    config.merge({"watch": {"mode": "notify", "interval_s": "5"}})

    assert config.watch.mode == "notify"
    assert config.watch.interval_s == 5.0


# ---------------------------------------------------------------------------
# dotted option names (dcs config set)
# ---------------------------------------------------------------------------


def test_every_section_is_listed_as_options() -> None:
    names = option_paths()

    assert "watch.mode" in names
    assert "email.smtp_host" in names
    assert "archive.format" in names
    # nested tables are set as a whole section, not as single values
    assert not [name for name in names if name.count(".") != 1]
    assert len(names) == len(set(names))


def test_set_option_coerces_the_value() -> None:
    config = AppConfig()

    assert set_option(config, "watch.mode", "open_run")[0] is True
    assert set_option(config, "watch.interval_s", "9")[0] is True
    assert set_option(config, "watch.ignore_existing", "false")[0] is True
    assert set_option(config, "email.to_addrs", "a@x.test, b@x.test")[0] is True

    assert config.watch.mode == "open_run"
    assert config.watch.interval_s == 9.0
    assert config.watch.ignore_existing is False
    assert config.email.to_addrs == ["a@x.test", "b@x.test"]


def test_set_option_refuses_a_value_the_config_calls_broken() -> None:
    config = AppConfig()

    ok, message = set_option(config, "watch.mode", "maybe")

    assert ok is False
    assert "watch.mode" in message
    assert config.watch.mode == "off", "the old value is kept"


def test_set_option_refuses_an_unknown_option_or_section() -> None:
    config = AppConfig()

    assert set_option(config, "watch", "notify")[0] is False
    assert set_option(config, "nope.mode", "notify")[0] is False
    assert set_option(config, "watch.nope", "1")[0] is False
    assert set_option(config, "scan.max_file_size_mb", "not a number")[0] is False


def test_set_option_keeps_a_nested_table_out_of_reach() -> None:
    config = AppConfig()

    ok, message = set_option(config, "scan.clamav.batch_size", "10")

    assert ok is False
    assert "scan.clamav.batch_size" in message

