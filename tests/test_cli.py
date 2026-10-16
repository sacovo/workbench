"""Tests for the wbench CLI skeleton: version, doctor's shape, and its two warnings.

No app logic exists yet, so these tests are about the CLI contract other code
(and the owner, reading `wbench doctor`) will rely on: the JSON keys stay stable,
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


QUESTIONS = [{"id": "q1", "text": "Which?", "options": [{"id": "a", "label": "A"}]}]


def test_session_defaults_from_wb_session_when_the_flag_is_not_given(project, monkeypatch, capsys):
    # The plugin's SessionStart hook (plugin/hooks/wb_hook.py write_env_file) exports WB_SESSION
    # into the agent's shell; ops that take --session (ask, note, inbox, reply, ...) pick it up
    # here so the agent does not have to name its own session on every call.
    monkeypatch.chdir(project.root)
    monkeypatch.setenv("WB_SESSION", "sess-from-env")

    assert main(["ask", "From the environment", "--questions", json.dumps(QUESTIONS), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["opened"]["by"] == {"role": "agent", "session": "sess-from-env"}

    # An explicit --session on the command line wins over the environment.
    assert main(["ask", "Flag wins", "--questions", json.dumps(QUESTIONS), "--session", "sess-from-flag", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["opened"]["by"] == {"role": "agent", "session": "sess-from-flag"}

    # Neither WB_SESSION nor --session: no session is recorded (unchanged behaviour).
    monkeypatch.delenv("WB_SESSION")
    assert main(["ask", "Neither", "--questions", json.dumps(QUESTIONS), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["opened"]["by"] == {"role": "agent"}

    # An operation without a `session` parameter at all is unaffected by WB_SESSION.
    monkeypatch.setenv("WB_SESSION", "sess-from-env")
    assert main(["status", "--json"]) == 0
    json.loads(capsys.readouterr().out)  # does not raise "unknown argument session"


class _Done:
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_check_prints_the_table_and_exits_by_the_result(project, monkeypatch, capsys):
    # The kernel side (python -m workbench_kernel check) is tested in packages/kernel; here only
    # what the hub does with its answer: the command it runs, the table, and the exit code.
    from workbench.ops import check_ops

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs["env"]))
        ok = "good" in command
        return _Done(0 if ok else 1, "noise\n" + json.dumps({"ok": ok, "text": f"TABLE {ok}", "groups": [], "count": 1, "failed": []}))

    monkeypatch.chdir(project.root)
    monkeypatch.setenv("VIRTUAL_ENV", "/elsewhere")
    monkeypatch.setattr(check_ops.subprocess, "run", fake_run)
    assert main(["check", "--groups", '["good"]']) == 0
    assert capsys.readouterr().out.strip() == "TABLE True"
    assert main(["check"]) == 1
    assert capsys.readouterr().out.strip() == "TABLE False"
    command, env = calls[0]
    assert command[:4] == ["uv", "run", "--project", str(project.root)] and command[-1] == "good"
    assert "VIRTUAL_ENV" not in env

    # A run that cannot report (a wrong group, a broken environment) is an error, not a table.
    monkeypatch.setattr(check_ops.subprocess, "run", lambda command, **kw: _Done(2, json.dumps({"ok": False, "error": "no check group 'x'"})))
    assert main(["check", "--groups", '["x"]']) == 1
    assert "no check group 'x'" in capsys.readouterr().err
