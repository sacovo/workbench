"""Check objects, check groups and `python -m workbench_kernel check`."""

import json
import math
import os
import subprocess
import sys

import pytest

from workbench_kernel.api import ManifestError, Project
from workbench_kernel.checks import Check, CheckError, assert_checks, run_checks, run_group


def make(value=1.0, limit=2.0, sense="max", **overrides):
    fields = dict(name="c", value=value, limit=limit, unit="N", source="RULES.x", credibility="calculated", method="m", sense=sense)
    return Check(**{**fields, **overrides})


def test_utilisation_for_upper_and_lower_limits():
    assert make(1.0, 2.0).utilisation == 0.5 and make(1.0, 2.0).passed
    assert make(3.0, 2.0).utilisation == 1.5 and not make(3.0, 2.0).passed
    assert make(4.0, 2.0, "min").utilisation == 0.5 and make(4.0, 2.0, "min").passed
    assert make(1.0, 2.0, "min").utilisation == 2.0 and not make(1.0, 2.0, "min").passed
    assert make(0.0, 2.0, "min").utilisation == math.inf and not make(0.0, 2.0, "min").passed


@pytest.mark.parametrize(
    "overrides",
    [{"value": math.nan}, {"limit": math.inf}, {"limit": 0.0}, {"value": True}, {"credibility": "guessed"}, {"sense": "eq"}, {"source": ""}],
)
def test_malformed_checks_are_refused(overrides):
    with pytest.raises(CheckError):
        make(**overrides)


def test_a_group_that_returns_nothing_fails():
    result = run_group("empty", lambda: [])
    assert not result["ok"] and "asserts nothing" in result["error"]
    with pytest.raises(AssertionError, match="asserts nothing"):
        assert_checks([])


def test_a_group_that_raises_or_returns_junk_fails_without_raising():
    def raises():
        raise RuntimeError("boom")

    assert "boom" in run_group("raises", raises)["error"]
    assert not run_group("junk", lambda: [1.0])["ok"]
    assert "duplicate" in run_group("twice", lambda: [make(), make()])["error"]


def test_run_checks_reports_every_failure():
    wb = Project("p")
    wb.checks("good")(lambda: [make(1.0, 2.0)])
    wb.checks("bad")(lambda: [make(3.0, 2.0, name="over"), make(1.0, 2.0, name="under")])
    result = run_checks(wb)
    assert not result["ok"] and result["count"] == 3 and result["failed"] == ["bad: over"]
    assert run_checks(wb, ["good"])["ok"]
    with pytest.raises(CheckError):
        run_checks(wb, ["missing"])
    with pytest.raises(CheckError, match="nothing is asserted"):
        run_checks(Project("empty"))


def test_check_groups_take_no_arguments_and_register_once():
    wb = Project("p")
    wb.checks("a")(lambda: [])
    with pytest.raises(ManifestError):
        wb.checks("a")(lambda: [])
    with pytest.raises(ManifestError):
        wb.checks("b")(lambda x: [])
    assert wb.describe()["check_groups"] == ["a"]


MANIFEST = '''
from workbench_kernel.api import Project
from workbench_kernel.checks import Check

wb = Project("checked")

@wb.checks("pass")
def passing():
    print("chatter that must not reach stdout")
    return [Check("load", 1.0, 2.0, "N", "datasheet", "calculated", "by hand")]

@wb.checks("fail")
def failing():
    return [Check("gap", 1.0, 2.0, "mm", "RULES.gap", "measured", "calipers", sense="min")]
'''


@pytest.fixture
def checked_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "checked"\nversion = "0"\n[tool.workbench]\nmanifest = "checked_manifest"\n')
    (tmp_path / "checked_manifest.py").write_text(MANIFEST)
    return tmp_path


def run_cli(project, *args):
    env = {**os.environ, "PYTHONPATH": str(project)}
    return subprocess.run([sys.executable, "-m", "workbench_kernel", "check", "--project", str(project), *args], capture_output=True, text=True, env=env)


def test_cli_exit_codes_and_json(checked_project):
    passing = run_cli(checked_project, "pass", "--json")
    assert passing.returncode == 0
    result = json.loads(passing.stdout)  # one JSON object, nothing else on stdout
    assert result["ok"] and result["count"] == 1 and "load" in result["text"]
    assert "chatter" in passing.stderr
    failing = run_cli(checked_project)
    assert failing.returncode == 1 and "gap" in failing.stdout and "1 failing" in failing.stdout
    assert run_cli(checked_project, "nope").returncode == 2


def test_numpy_values_describe_as_plain_json():
    np = pytest.importorskip("numpy")
    check = make(np.float64(1.0), np.float64(2.0))
    assert type(check.value) is float and type(check.limit) is float
    described = check.describe()
    assert type(described["passed"]) is bool
    assert json.loads(json.dumps(described, allow_nan=False))["passed"] is True
