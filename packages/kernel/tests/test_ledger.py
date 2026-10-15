"""The ledger: assumed values hold the outputs that need them until a measurement is recorded and
the parameter carries it."""

import json
import sys

import pytest

from workbench_kernel import ledger
from workbench_kernel.api import ManifestError, Project

KEY = "fakeparams:BASE.width"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project whose parameter module `fakeparams` has BASE.width = 36.4, assumed (R-0027), and
    two outputs: one that needs it and one that does not."""
    (tmp_path / "fakeparams.py").write_text("class BASE:\n    width = 36.4\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # a rewrite in the same second must not load a stale .pyc
    sys.modules.pop("fakeparams", None)
    wb = Project("p")
    wb.assume(KEY, item="R-0027", unit="mm", tolerance=0.05)
    wb.output("plate.dxf", command="true", needs=[KEY])
    wb.output("notes.txt", command="true")
    return wb, tmp_path


def record(root, value, unit="mm"):
    path = root / ledger.LEDGER_FILE
    path.parent.mkdir(exist_ok=True)
    entry = {"key": KEY, "value": value, "unit": unit, "source": "calipers", "at": "2026-10-16T09:00:00Z", "by": {"role": "agent"}}
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def set_width(root, value):
    (root / "fakeparams.py").write_text(f"class BASE:\n    width = {value}\n")
    sys.modules.pop("fakeparams", None)


def test_an_unmeasured_assumption_holds_its_output_and_names_the_request(project):
    wb, root = project
    (reason,) = ledger.holds(wb, root, "plate.dxf")
    assert KEY in reason and "R-0027" in reason and "not measured" in reason
    assert ledger.holds(wb, root, "notes.txt") == []


def test_a_measurement_the_parameter_carries_releases_the_hold(project):
    wb, root = project
    record(root, 36.42)  # within the 0.05 tolerance of 36.4
    assert ledger.holds(wb, root, "plate.dxf") == []


def test_a_measurement_the_parameter_does_not_carry_keeps_the_hold_until_it_is_set(project):
    wb, root = project
    record(root, 36.1)
    (reason,) = ledger.holds(wb, root, "plate.dxf")
    assert "36.1" in reason and "still 36.4" in reason
    set_width(root, 36.1)
    assert ledger.holds(wb, root, "plate.dxf") == []


def test_the_latest_record_wins_and_a_wrong_unit_holds(project):
    wb, root = project
    record(root, 36.4)
    record(root, 3.64, unit="cm")
    (reason,) = ledger.holds(wb, root, "plate.dxf")
    assert "cm" in reason


def test_declarations_are_checked():
    wb = Project("p")
    with pytest.raises(ManifestError, match="no assume"):
        wb.output("plate.dxf", command="true", needs=[KEY])
    with pytest.raises(ledger.LedgerError):
        wb.assume("BASE.width", item="R-1", unit="mm")
    wb.assume(KEY, item="R-1", unit="mm")
    with pytest.raises(ManifestError, match="twice"):
        wb.assume(KEY, item="R-1", unit="mm")


def test_require_raises_held_for_a_script(project, monkeypatch):
    wb, root = project
    monkeypatch.setattr(ledger, "_project", lambda _root: wb)
    with pytest.raises(ledger.Held, match="R-0027"):
        ledger.require("plate.dxf", root)
    record(root, 36.4)
    ledger.require("plate.dxf", root)


def test_a_malformed_ledger_line_is_an_error_not_a_silent_skip(project):
    wb, root = project
    (root / "ledger").mkdir()
    (root / ledger.LEDGER_FILE).write_text('{"key": "x"}\n')
    with pytest.raises(ledger.LedgerError, match="needs key, value"):
        ledger.holds(wb, root, "plate.dxf")
