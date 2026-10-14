"""Engineering checks: a value against a limit, with where the limit comes from and how far the
value can be trusted.

A project registers check groups on its manifest's `Project`:

    @wb.checks("wheel_servo")
    def wheel_servo():
        return [Check("shaft moment, braking", 0.61, 0.40, "N*m", source="RULES.wheel_shaft_moment",
                      credibility="calculated", method="static load times braking factor, lever 22 mm")]

Importing the manifest builds nothing; a group runs when `python -m workbench_kernel check` (or
`wbench check`, or the project's pytest) asks for it. A group must return at least one check: a
group that returns nothing asserts nothing and fails like a check over its limit.

Utilisation is value / limit for an upper limit (sense "max") and limit / value for a lower one
(sense "min", such as a clearance or a safety factor), so a check passes when it is at most 1.
"""

from __future__ import annotations

import math
import traceback
from dataclasses import asdict, dataclass
from typing import Literal

Credibility = Literal["calculated", "simulated", "measured"]
CREDIBILITIES = ("calculated", "simulated", "measured")


class CheckError(Exception):
    """A check that cannot be evaluated: malformed, not finite, or a group that asserts nothing."""


@dataclass(frozen=True)
class Check:
    name: str
    value: float
    limit: float
    unit: str
    source: str  # the limit's origin: a parameter field ("RULES.front_limit") or a datasheet citation
    credibility: Credibility  # how the value was obtained
    method: str  # one line: model, load case, assumptions
    sense: Literal["max", "min"] = "max"  # the value must stay at most (max) or at least (min) the limit

    def __post_init__(self):
        where = f"check {self.name!r}"
        if not self.name or not self.unit or not self.source or not self.method:
            raise CheckError(f"{where}: name, unit, source and method must all be given")
        if self.credibility not in CREDIBILITIES:
            raise CheckError(f"{where}: credibility {self.credibility!r} is not one of {CREDIBILITIES}")
        if self.sense not in ("max", "min"):
            raise CheckError(f"{where}: sense must be 'max' or 'min', not {self.sense!r}")
        for field in ("value", "limit"):
            number = getattr(self, field)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise CheckError(f"{where}: {field} must be a finite number, got {number!r}")
        if self.limit <= 0:
            raise CheckError(f"{where}: limit must be positive, got {self.limit}")

    @property
    def utilisation(self) -> float:
        if self.sense == "max":
            return self.value / self.limit
        return math.inf if self.value <= 0 else self.limit / self.value

    @property
    def passed(self) -> bool:
        return self.utilisation <= 1.0

    def describe(self) -> dict:
        utilisation = self.utilisation
        return {**asdict(self), "utilisation": utilisation if math.isfinite(utilisation) else None, "passed": self.passed}


def run_group(name: str, fn) -> dict:
    """Run one check group; never raises. The result carries the checks or the error."""
    try:
        checks = fn()
        if not isinstance(checks, (list, tuple)) or not all(isinstance(c, Check) for c in checks):
            raise CheckError(f"group {name!r} must return a list of Check, got {type(checks).__name__}")
        if not checks:
            raise CheckError(f"group {name!r} returned no checks: it asserts nothing")
        names = [c.name for c in checks]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise CheckError(f"group {name!r} has duplicate check names: {duplicates}")
        described = [c.describe() for c in checks]
        return {"group": name, "ok": all(c["passed"] for c in described), "checks": described, "error": None}
    except Exception as exc:  # noqa: BLE001 - reported as a failed group
        return {"group": name, "ok": False, "checks": [], "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}


def run_checks(project, groups: list[str] | None = None) -> dict:
    """Run the project's check groups (all, or those named) and summarise them."""
    known = list(project.check_groups)
    unknown = sorted(set(groups or []) - set(known))
    if unknown:
        raise CheckError(f"no check group {', '.join(map(repr, unknown))}; the project has {known}")
    if not known:
        raise CheckError(f"project {project.name!r} registers no check groups: nothing is asserted")
    results = [run_group(name, project.check_groups[name]) for name in (groups or known)]
    return {
        "project": project.name,
        "ok": all(r["ok"] for r in results),
        "groups": results,
        "count": sum(len(r["checks"]) for r in results),
        "failed": [f"{r['group']}: {c['name']}" for r in results for c in r["checks"] if not c["passed"]]
        + [f"{r['group']}: {r['error']}" for r in results if r["error"]],
    }


def assert_checks(checks) -> None:
    """For a project's pytest: fail on an empty list, a malformed check or any check over its limit,
    naming every failing check with its value, limit and utilisation."""
    checks = list(checks)
    assert checks, "no checks: the group asserts nothing"
    failing = [c for c in checks if not c.passed]
    assert not failing, "; ".join(
        f"{c.name}: {c.value:.4g} {c.unit} against {c.limit:.4g} ({c.sense}, {c.source}), utilisation {c.utilisation:.2f}"
        for c in failing
    )


def table(result: dict) -> str:
    """The `wbench check` printout of a run_checks result."""
    lines = []
    for group in result["groups"]:
        lines.append(f"{group['group']}  {'ok' if group['ok'] else 'FAILED'}")
        if group["error"]:
            lines.append(f"  error: {group['error']}")
        for c in group["checks"]:
            util = "inf" if c["utilisation"] is None else f"{c['utilisation']:.2f}"
            relation = "<=" if c["sense"] == "max" else ">="
            mark = "  " if c["passed"] else "!!"
            lines.append(
                f"{mark}{util:>6}  {c['name']}: {c['value']:.4g} {relation} {c['limit']:.4g} {c['unit']}"
                f"  [{c['credibility']}; limit: {c['source']}]"
            )
            lines.append(f"          {c['method']}")
    verdict = "all pass" if result["ok"] else f"{len(result['failed'])} failing"
    lines.append(f"{result['count']} checks in {len(result['groups'])} groups: {verdict}")
    return "\n".join(lines)
