"""The ledger (workbench_kernel.ledger): `wbench ledger`, `wbench measure` and `wbench make`.

The declarations live on the project's manifest and the parameter values in its code, so the
holds are worked out by `python -m workbench_kernel ledger` in the project's own environment, as
the checks are. This process only appends measurements to `ledger/measurements.jsonl` and starts
an output's command as a job (workbench.jobs) once nothing holds it.
"""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Optional

from workbench import jobs, outputs
from workbench.errors import InvalidInput, WorkbenchError
from workbench.hub.kernel import kernel_env
from workbench.ops.registry import op
from workbench.project import Project
from workbench.review.store import ReviewStore, make_by, now

LEDGER_FILE = Path("ledger") / "measurements.jsonl"  # as workbench_kernel.ledger.LEDGER_FILE
TIMEOUT = 600.0


class LedgerRunError(WorkbenchError):
    """The kernel could not report the ledger: the environment or the manifest failed."""

    http_status = 500


class OutputHeld(WorkbenchError):
    """An output asked for while a value it needs is assumed or out of date."""

    http_status = 409


def kernel_json(project: Project, *arguments: str) -> dict:
    """One JSON object from `python -m workbench_kernel <arguments> --project ROOT` in the project's environment."""
    root = str(project.root)
    command = ["uv", "run", "--project", root, "python", "-m", "workbench_kernel", *arguments, "--project", root]
    try:
        done = subprocess.run(command, cwd=project.root, env=kernel_env(), capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LedgerRunError(f"could not run the kernel: {exc}") from None
    lines = done.stdout.strip().splitlines()
    try:
        result = json.loads(lines[-1]) if lines else None
    except ValueError:
        result = None
    if not isinstance(result, dict) or done.returncode != 0 or "error" in result:
        reason = result.get("error") if isinstance(result, dict) else None
        raise LedgerRunError(f"kernel {arguments[0]} failed (exit {done.returncode}): {reason or done.stderr.strip()[-2000:] or 'no output'}")
    return result


def kernel_ledger(project: Project, output: Optional[str] = None) -> dict:
    return kernel_json(project, "ledger", *(["--output", output] if output is not None else []))


def _human_ledger(r: dict) -> str:
    lines = []
    for a in r["assumptions"]:
        record = a["record"]
        seen = f"; measured {record['value']} {record['unit']} by {record['by']['role']} at {record['at']}" if record else ""
        lines.append(f"{a['state']:>8}  {a['key']} = {a['value']} {a['unit']} ({a['item']}){seen}")
    for o in r["outputs"]:
        lines.append(f"{'HELD' if o['held'] else 'ok':>8}  {o['name']}: {o['command']}")
        lines += [f"          {reason}" for reason in o["held"]]
    return "\n".join(lines) or "the project declares no assumptions or outputs"


@op(
    "ledger",
    summary="List assumed and measured values and the outputs they hold",
    human=_human_ledger,
)
def ledger(project: Project) -> dict:
    """Each assumption: its current value, latest measurement and state (assumed, measured, stale:
    measured but the parameter still differs, unit: measured in another unit). Each output: the
    reasons it is held, if any."""
    return kernel_ledger(project)


def _human_measure(r: dict) -> str:
    text = f"recorded {r['record']['key']} = {r['record']['value']} {r['record']['unit']} in {LEDGER_FILE}"
    if r["holds"]:
        text += f"\nstill {r['state']}: " + "; ".join(r["holds"])
    return text


@op(
    "measure",
    summary="Record a measured value in the ledger (from the command line or the owner's answer)",
    human=_human_measure,
    args={
        "key": "the assumed parameter, module:OBJECT.field, as declared with assume()",
        "value": "the measured value; omit it to take the owner's answer on the item",
        "unit": "its unit; must be the declared one (default: the declared one)",
        "source": "how it was measured: instrument, readings, who (required unless from the owner's answer)",
        "item": "the measurement request (default: the declared item); a reply is posted on it",
        "role": "who records: agent (default) or reviewer",
        "session": "the recording agent's session",
    },
)
def measure(
    project: Project,
    key: str,
    value: Optional[float] = None,
    unit: Optional[str] = None,
    source: str = "",
    item: Optional[str] = None,
    role: str = "agent",
    session: Optional[str] = None,
) -> dict:
    """Appends one record; never rewrites the file. The parameter itself is not changed: until it
    carries the measured value, the outputs that need it stay held (state `stale`)."""
    if role not in ("agent", "reviewer"):
        raise InvalidInput("role must be agent or reviewer")
    declared = {a["key"]: a for a in kernel_ledger(project)["assumptions"]}
    if key not in declared:
        raise InvalidInput(f"{key} is not an assumption the manifest declares; it declares {sorted(declared)}")
    assumption = declared[key]
    unit = unit or assumption["unit"]
    if unit != assumption["unit"]:
        raise InvalidInput(f"{key} is declared in {assumption['unit']}, not {unit}")
    item = item or assumption["item"]
    store = ReviewStore(project)
    _, state = store.item(item)
    if value is None:
        if state["kind"] != "measurement" or key not in state["approved"]:
            raise InvalidInput(f"no value given and {item} carries no owner's answer for {key}")
        value = state["approved"][key]
        source = source or f"the owner's answer on {item}"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidInput(f"value must be a finite number, got {value!r}")
    if not source.strip():
        raise InvalidInput("say how it was measured (source)")
    record = {"key": key, "value": value, "unit": unit, "source": source, "by": make_by(role, session), "at": now(), "item": item, "build": store.stamp()}
    with project.lock():
        path = project.root / LEDGER_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    store.append(item, "reply", {"by": make_by(role, session), "body": f"Recorded in {LEDGER_FILE}: {key} = {value} {unit} ({source})."})
    after = {a["key"]: a for a in kernel_ledger(project)["assumptions"]}[key]
    holds = [] if after["state"] == "measured" else [f"the parameter is {after['value']} {unit}; set it to {value} and rebuild"]
    return {"record": record, "state": after["state"], "holds": holds}


def _human_make(r: dict) -> str:
    return f"{r['id']} running: {r['title']}\nlog {r['log']}; its end is reported on {r['id']}"


@op(
    "make",
    summary="Make a declared output as a job, unless an assumed value holds it",
    human=_human_make,
    args={"output": "the output's name, as declared with output()", "session": "the starting agent's session"},
)
def make(project: Project, output: str, session: Optional[str] = None) -> dict:
    """Refuses, naming each value and its measurement request, while the output is held;
    otherwise starts its command as a `wbench run` job."""
    result = kernel_ledger(project, output)
    if result["holds"]:
        raise OutputHeld(f"{output} is held: " + "; ".join(result["holds"]))
    declared = {o["name"]: o for o in result["outputs"]}[output]
    title = f"Make {output}"
    body = f"`{declared['command']}`\n\nNeeds: " + (", ".join(declared["needs"]) or "no assumed values")
    names = outputs.siblings(result, output)
    values = {a["key"]: {k: a[k] for k in ("value", "unit", "state", "item")} | {"record": a["record"]} for a in result["assumptions"] if a["key"] in declared["needs"]}
    make_spec = {"outputs": names, "before": outputs.before(project, names), "build": outputs.current_build(project), "values": values}
    state = ReviewStore(project).open_item({"by": make_by("agent", session), "kind": "job", "title": title, "body": body})
    started = jobs.start(project, state["id"], declared["command"], make=make_spec)
    return {"id": state["id"], "title": title, "stamps": names, **started}


_STATE_MARK = {"missing": "missing", "unstamped": "unstamped", "modified": "MODIFIED", "stale": "stale", "current": "current"}


def _human_outputs(r: dict) -> str:
    lines = [f"build {r['build']}"]
    for o in r["outputs"]:
        made = o["stamp"]
        when = f"; made {made['at']} at {str(made.get('build'))[:12]} by {made.get('job')}" if made else ""
        lines.append(f"{_STATE_MARK[o['state']]:>9}  {o['path']}{when}")
        lines += [f"           held: {reason}" for reason in o["held"]]
    return "\n".join(lines)


@op("outputs", summary="Declared outputs: whether each file is missing, unstamped, modified, stale or current, and held", human=_human_outputs)
def outputs_(project: Project) -> dict:
    """Each declared output's file against its as-made stamp (ledger/made.jsonl) and the current
    build id, with the ledger's holds (workbench.outputs)."""
    return outputs.status(project, kernel_ledger(project))
