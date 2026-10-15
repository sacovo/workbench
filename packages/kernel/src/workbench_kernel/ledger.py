"""The ledger: values the design assumes until someone measures them, and the outputs they hold.

A project declares on its manifest which parameters are assumed and which outputs rest on them:

    wb.assume("robot.params:BASE.wheel_body_width", item="R-0027", unit="mm", tolerance=0.05)
    wb.output("lower_plate.dxf", command="uv run scripts/export_base.py",
              needs=["robot.params:BASE.wheel_body_width"])

A key is `module:OBJECT.field`, the same key a measurement request (review item of kind
`measurement`) carries as its quantity. Measurements are recorded in `ledger/measurements.jsonl`
in the project root, one JSON object per line, appended and never rewritten (`wbench measure`
writes them):

    {"key": "...", "value": 36.1, "unit": "mm", "source": "calipers over the hub ring, 3 readings",
     "by": {"role": "agent", ...}, "at": "2026-10-16T09:00:00Z", "item": "R-0027"}

The latest record for a key is its measured value. An output is held while any value it needs is
- not measured: the design still runs on the assumption;
- measured but not in the parameters: the record differs from the parameter's current value by
  more than the assumption's tolerance, so the output would be made from the old number;
- measured in another unit than the one declared.
A held output must not be made: `wbench make` refuses to start its command, and a script that
writes it calls `require(name)` (or `held(name)`) first, so running the script directly fails too.
"""

from __future__ import annotations

import importlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

LEDGER_FILE = Path("ledger") / "measurements.jsonl"


class LedgerError(Exception):
    """A declaration or record the ledger cannot use."""


class Held(Exception):
    """An output asked for while a value it needs is still assumed or out of date."""

    def __init__(self, output: str, reasons: list[str]):
        self.output, self.reasons = output, reasons
        super().__init__(f"{output} is held: " + "; ".join(reasons))


@dataclass(frozen=True)
class Assumption:
    key: str
    item: str  # the measurement request that asks for it
    unit: str
    tolerance: float  # a measured value this close to the parameter counts as applied
    note: str

    def describe(self) -> dict:
        return {"key": self.key, "item": self.item, "unit": self.unit, "tolerance": self.tolerance, "note": self.note}


@dataclass(frozen=True)
class Output:
    name: str  # a file the project makes, as named in its build directory
    command: str  # the shell command that makes it, run in the project root
    needs: tuple[str, ...]  # keys of assumptions it rests on
    doc: str

    def describe(self) -> dict:
        return {"name": self.name, "command": self.command, "needs": list(self.needs), "doc": self.doc}


def split_key(key: str) -> tuple[str, list[str]]:
    module, sep, path = key.partition(":")
    if not sep or not module or not path or not all(path.split(".")):
        raise LedgerError(f"key {key!r} must be module:OBJECT.field, e.g. robot.params:BASE.wheel_width")
    return module, path.split(".")


def current_value(key: str):
    """The parameter's value as the code has it now."""
    module, path = split_key(key)
    value = importlib.import_module(module)
    for name in path:
        try:
            value = getattr(value, name)
        except AttributeError:
            raise LedgerError(f"{key}: {value!r} has no attribute {name!r}") from None
    return value


def read_records(root: Path) -> list[dict]:
    path = Path(root) / LEDGER_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise LedgerError(f"{LEDGER_FILE}:{number}: not valid JSON: {exc}") from None
        if not isinstance(record, dict) or not {"key", "value", "unit", "source", "at"} <= set(record):
            raise LedgerError(f"{LEDGER_FILE}:{number}: a record needs key, value, unit, source and at")
        records.append(record)
    return records


def latest(records: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for record in records:  # appended in time order: the last one wins
        out[record["key"]] = record
    return out


def status(project, root: Path) -> list[dict]:
    """Each assumption with its current value, its latest measurement and its state: assumed,
    measured (and applied), stale (measured, parameter differs) or unit (measured in another unit)."""
    measured = latest(read_records(root))
    out = []
    for assumption in project.assumptions.values():
        value = current_value(assumption.key)
        record = measured.get(assumption.key)
        if record is None:
            state = "assumed"
        elif record["unit"] != assumption.unit:
            state = "unit"
        elif not _close(value, record["value"], assumption.tolerance):
            state = "stale"
        else:
            state = "measured"
        out.append({**assumption.describe(), "value": value, "record": record, "state": state})
    return out


def _close(value, measured, tolerance: float) -> bool:
    if isinstance(value, (int, float)) and isinstance(measured, (int, float)) and not isinstance(value, bool):
        return math.isclose(value, measured, abs_tol=tolerance, rel_tol=0.0)
    return value == measured


def reasons(entry: dict) -> str:
    key, unit, record = entry["key"], entry["unit"], entry["record"]
    if entry["state"] == "assumed":
        return f"{key} is assumed ({entry['value']} {unit}) and not measured yet: {entry['item']}"
    if entry["state"] == "unit":
        return f"{key} was measured in {record['unit']}, the parameter is in {unit} ({entry['item']})"
    return (
        f"{key} was measured as {record['value']} {unit} ({record['at']}) but the parameter is still "
        f"{entry['value']} {unit}: set it and rebuild ({entry['item']})"
    )


def holds(project, root: Path, output: str) -> list[str]:
    """Why `output` is held; empty when it may be made."""
    if output not in project.outputs:
        raise LedgerError(f"no output {output!r}; the project declares {sorted(project.outputs)}")
    entries = {entry["key"]: entry for entry in status(project, root)}
    return [reasons(entries[key]) for key in project.outputs[output].needs if entries[key]["state"] != "measured"]


def summary(project, root: Path) -> dict:
    entries = status(project, root)
    by_key = {entry["key"]: entry for entry in entries}
    outputs = []
    for out in project.outputs.values():
        held_by = [reasons(by_key[key]) for key in out.needs if by_key[key]["state"] != "measured"]
        outputs.append({**out.describe(), "held": held_by})
    return {"assumptions": entries, "outputs": outputs}


def _project(root: Path):
    from workbench_kernel.api import Project
    from workbench_kernel.paths import manifest_module

    module = importlib.import_module(manifest_module(Path(root).resolve()))
    projects = [v for v in vars(module).values() if isinstance(v, Project)]
    if len(projects) != 1:
        raise LedgerError(f"manifest {module.__name__} must create exactly one Project, has {len(projects)}")
    return projects[0]


def _root(root) -> Path:
    """`root`, or the nearest directory up from the current one whose pyproject.toml names a manifest."""
    if root is not None:
        return Path(root)
    from workbench_kernel.paths import manifest_module

    for directory in (Path.cwd(), *Path.cwd().parents):
        if (directory / "pyproject.toml").is_file():
            manifest_module(directory)  # raises if it names no manifest
            return directory
    raise LedgerError(f"no pyproject.toml with [tool.workbench] above {Path.cwd()}")


def held(output: str, root=None) -> list[str]:
    """For a project's own script: why `output` is held (empty when it may be written)."""
    root = _root(root)
    return holds(_project(root), root, output)


def require(output: str, root=None) -> None:
    """For a project's own script: raise Held unless `output` may be written."""
    found = held(output, root)
    if found:
        raise Held(output, found)
