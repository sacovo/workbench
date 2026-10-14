"""Tests for the wb CLI skeleton: version, doctor's shape, and its two warnings.

No app logic exists yet, so these tests are about the CLI contract other code
(and the owner, reading `wb doctor`) will rely on: the JSON keys stay stable,
missing tools are reported rather than raising, and the two environment
warnings fire exactly when their variable is set.
"""

import json
import sys

import pytest

from workbench import __version__
from workbench.cli import STALE_GL_VARS, TOOLS, main


def test_version_prints_the_package_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_doctor_json_has_the_expected_keys(capsys, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    for name in STALE_GL_VARS:
        monkeypatch.delenv(name, raising=False)

    exit_code = main(["doctor", "--json"])

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"tools", "virtual_env", "stale_gl_vars", "warnings"}
    assert set(report["tools"]) == set(TOOLS)
    for info in report["tools"].values():
        assert set(info) == {"found", "path", "version"}
    assert report["virtual_env"] == {"set": False, "value": None, "foreign": False}
    assert report["stale_gl_vars"] == {"checked": list(STALE_GL_VARS), "set": []}
    assert report["warnings"] == []


def test_missing_tool_is_reported_as_missing_not_raised(capsys, monkeypatch, tmp_path):
    # An empty directory as the whole PATH: shutil.which finds none of the six tools.
    monkeypatch.setenv("PATH", str(tmp_path))

    exit_code = main(["doctor", "--json"])

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    for name, info in report["tools"].items():
        assert info == {"found": False, "path": None, "version": None}, name


def test_warnings_appear_when_the_variables_are_set(capsys, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/home/sandro/.venv")
    monkeypatch.setenv("LIBVA_DRIVER_NAME", "nvidia")
    monkeypatch.delenv("__GLX_VENDOR_LIBRARY_NAME", raising=False)
    monkeypatch.delenv("XWAYLAND_NO_GLAMOR", raising=False)

    exit_code = main(["doctor", "--json"])

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["virtual_env"] == {"set": True, "value": "/home/sandro/.venv", "foreign": True}
    assert report["stale_gl_vars"]["set"] == ["LIBVA_DRIVER_NAME"]
    assert len(report["warnings"]) == 2
    assert "VIRTUAL_ENV" in report["warnings"][0]
    assert "LIBVA_DRIVER_NAME" in report["warnings"][1]


def test_no_warnings_when_nothing_is_set(capsys, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    for name in STALE_GL_VARS:
        monkeypatch.delenv(name, raising=False)

    main(["doctor", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert report["warnings"] == []


def test_the_running_environment_itself_is_not_a_warning(capsys, monkeypatch):
    # `uv run` sets VIRTUAL_ENV to the environment it runs in; that one is correct.
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    for name in STALE_GL_VARS:
        monkeypatch.delenv(name, raising=False)

    main(["doctor", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert report["virtual_env"]["foreign"] is False
    assert report["warnings"] == []


def test_human_readable_doctor_runs_without_json(monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert main(["doctor"]) == 0
