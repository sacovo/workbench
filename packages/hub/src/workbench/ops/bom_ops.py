"""Materials and tools (workbench_kernel.bom): `wbench bom`, `wbench have`, and `wbench todo`, the
owner's list of items waiting on them.

The bom lives in the project (bom.toml) and is read by `python -m workbench_kernel bom` in the
project's environment; this process only appends the owner's inventory records to
`ledger/inventory.jsonl`.
"""

from __future__ import annotations

import json
import math
from typing import Optional

from workbench.errors import InvalidInput
from workbench.ops.ledger_ops import kernel_json
from workbench.outputs import status as output_status
from workbench.ops.registry import op
from workbench.project import Project
from workbench.review.store import ReviewStore, make_by, now

INVENTORY_FILE = "ledger/inventory.jsonl"  # as workbench_kernel.bom.INVENTORY_FILE


def _qty(value: float) -> str:
    return f"{value:g}"


def _human_bom(r: dict) -> str:
    if r.get("markdown") is not None:
        return r["markdown"].removesuffix("\n")  # print() puts back the newline the kernel ended it with
    lines, group = [], None
    for row in r["items"]:
        if row["group"] != group:
            group = row["group"]
            lines.append(f"\n{group}")
        mark = f"{row['action']} {_qty(row['short'])}" if row["short"] > 0 else "ok"
        shown = row.get("qty_text") or _qty(row["qty"])
        lines.append(f"  {mark:>14}  {row['id']:<24} need {shown:<8} have {_qty(row['have']):<5} {row['name']}")
    counts = ", ".join(f"{n} {action}" for action, n in r["short"].items() if n)
    lines.append(f"\n{counts or 'nothing short'}")
    lines += [f"warning: {p}" for p in r.get("problems", [])]
    return "\n".join(lines).lstrip("\n")


@op(
    "bom",
    summary="Materials and tools: what is needed, had, and still to order or make",
    human=_human_bom,
    args={"short": "only items still short", "tools": "only the tools", "markdown": "the parts list as markdown (no inventory)"},
)
def bom(project: Project, short: bool = False, tools: bool = False, markdown: bool = False) -> dict:
    """From the project's bom.toml and the owner's inventory records. Each item: quantity needed,
    had (an owned item counts in full until recorded otherwise) and short, with the action:
    to order (bought), to make (made), missing (tools). A made item that names its output files
    gets each one's state (workbench.outputs) and holds."""
    if markdown:
        return {"items": [], "short": {}, "markdown": _markdown(project)}
    result = kernel_json(project, "bom")
    rows = result["items"]
    if any(row.get("outputs") for row in rows):
        files = {o["name"]: o for o in output_status(project, kernel_json(project, "ledger"))["outputs"]}
        for row in rows:
            if row.get("outputs"):
                row["files"] = [
                    {"name": name, "state": files[name]["state"], "held": files[name]["held"]} if name in files else {"name": name, "state": "undeclared", "held": []}
                    for name in row["outputs"]
                ]
    if tools:
        rows = [r for r in rows if r["kind"] == "tools"]
    if short:
        rows = [r for r in rows if r["short"] > 0]
    return {**result, "items": rows}


def _markdown(project: Project) -> str:
    import subprocess

    from workbench.hub.kernel import kernel_env

    root = str(project.root)
    done = subprocess.run(
        ["uv", "run", "--project", root, "python", "-m", "workbench_kernel", "bom", "--markdown", "--project", root],
        cwd=project.root, env=kernel_env(), capture_output=True, text=True, timeout=600,
    )
    if done.returncode:
        raise InvalidInput(f"bom --markdown failed: {done.stdout.strip() or done.stderr.strip()[-2000:]}")
    return done.stdout


@op(
    "have",
    summary="Record how many of a part or tool the owner has (owner)",
    owner_only=True,
    human=lambda r: f"recorded: have {_qty(r['record']['have'])} of {r['record']['id']} ({r['item']['name']}); short {_qty(r['item']['short'])}",
    args={"id": "the item's id in bom.toml", "qty": "how many are on hand (0 for none)", "note": "e.g. where it is, or ordered on ...", "name": "a name for the owner"},
)
def have(project: Project, id: str, qty: float, note: str = "", name: Optional[str] = None) -> dict:
    """Appends one record to ledger/inventory.jsonl; the latest record for an item counts."""
    if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(qty) or qty < 0:
        raise InvalidInput(f"qty must be a number of at least 0, got {qty!r}")
    items = {row["id"]: row for row in kernel_json(project, "bom")["items"]}
    if id not in items:
        raise InvalidInput(f"no item {id!r} in the bom")
    if not items[id].get("unit") and qty != int(qty):
        raise InvalidInput(f"{id} is counted: a whole number, got {qty!r}")
    record = {"id": id, "have": qty, "by": make_by("owner", None, name), "at": now(), **({"note": note} if note else {})}
    with project.lock():
        path = project.root / INVENTORY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    item = {row["id"]: row for row in kernel_json(project, "bom")["items"]}[id]
    return {"record": record, "item": item}


def owner_items(project: Project) -> list[dict]:
    """The items whose move is the owner's, oldest first, with their questions."""
    states, _ = ReviewStore(project).states()
    return [s for s in states if s["status"] == "waiting_owner"]


def _human_todo(r: dict) -> str:
    if not r["items"]:
        return "nothing waits on you"
    lines = []
    for s in r["items"]:
        lines.append(f"{s['id']} {s['kind']}: {s['title']}")
        if s["kind"] == "measurement":
            lines.append(f"    measure {s['quantity']} in {s['unit']}")
        for q in s.get("questions", []):
            if q["id"] not in s.get("open_questions", []):
                continue
            lines.append(f"    {q['id']}: {q['text']}")
            for o in q["options"]:
                rec = "  (recommended)" if q.get("recommended") == o["id"] else ""
                lines.append(f"       {o['id']}) {o['label']}{rec}")
            if q.get("free_text"):
                lines.append("       or your own text")
    lines.append("\nanswer with `wbench owner` (interactive) or in the page")
    return "\n".join(lines)


@op("todo", summary="What waits on the owner: decisions, measurements, comments", human=_human_todo)
def todo(project: Project) -> dict:
    """Every item whose move is the owner's, with its open questions and options."""
    return {"items": owner_items(project)}
