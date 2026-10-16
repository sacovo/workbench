"""Materials and tools: what a project needs, what the owner has, what is still to order or make.

A project lists its parts and tools in `bom.toml` at its root (or the file named by
`[tool.workbench] bom` in pyproject.toml):

    title = "Parts list"
    intro = "markdown shown at the top"

    [[hold]]
    what = "Lower plate and upper deck"
    wait_for = "The wheel's hub ring width (R-0027)."
    items = ["lower-plate", "upper-deck"]   # optional: the item ids it holds

    [[group]]
    name = "Arms and servos"
    kind = "bought"            # bought, or made (sheet, printed, turned), or tools
    note = "optional markdown under the heading"

    [[group.item]]
    id = "sts3215"             # unique; the owner's inventory refers to it
    name = "Feetech STS3215, 12 V, 30 kg*cm"
    qty = 9                    # a number; 0 with qty_text "as needed" counts nothing
    unit = "m"                 # optional: a measured amount (metres of wire); without one the
                               # item is counted, and what is had must be a whole number
    qty_text = "8 + 1"         # optional, how the quantity reads
    status = "decided"         # owned, decided, assumed, designed, open, optional
    notes = "..."
    # made parts may give `material`, `size` and `file`, and `outputs`: the names of the files the
    # project declares (`wb.output`) that make it, checked against the manifest; tools give `for`
    # and `service` (who can do the job instead of the tool)

What the owner has is recorded in `ledger/inventory.jsonl`, one JSON object per line, appended and
never rewritten (`wbench have`): {"id": ..., "have": 3, "by": {...}, "at": ..., "note": ...}. The
latest record for an id counts; an item with status "owned" counts as had in full until a record
says otherwise. Each item's shortfall is its quantity less what is had: to order (bought), to make
(made), or missing (tools). An item that is not ready to order says so instead: "on hold" (a
`[[hold]]` names it), "decide first" (status assumed or open: the model is not chosen) or
"optional". Those are listed but not counted as short, and a shopping list leaves them out.

Inventory records whose id is not (or no longer) in the bom, or whose `have` is not a number of at
least 0, are skipped and reported in `problems`: the ledger is append-only, so one stale record
must not stop the whole list.

Product links the owner asks the agent to check are in `ledger/links.jsonl`, appended the same way
(`wbench check-link` by the owner, `wbench link-verdict` by the agent): {"id": ..., "url": ...,
"state": "requested" | "ok" | "check" | "wrong", "summary": ..., "price": ..., "currency": ...,
"shop": ..., "by": {...}, "at": ...}. Each item gets its latest record as `link` and its latest
"ok" one as `source`: where to buy it, for the shopping list.
"""

from __future__ import annotations

import json
import math
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

INVENTORY_FILE = Path("ledger") / "inventory.jsonl"
LINKS_FILE = Path("ledger") / "links.jsonl"
LINK_STATES = ("requested", "ok", "check", "wrong")
KINDS = ("bought", "sheet", "printed", "turned", "tools")
MADE = ("sheet", "printed", "turned")
STATUSES = ("owned", "decided", "assumed", "designed", "open", "optional", "from SO-ARM100")
UNDECIDED = ("assumed", "open")  # the model or design is not chosen: decide before ordering
ACTIONS = ("to order", "to make", "missing", "on hold", "decide first", "optional")
TEXT_FIELDS = ("name", "qty_text", "status", "notes", "material", "size", "file", "for", "service", "unit")


class BomError(Exception):
    """A bom.toml or inventory record the workbench cannot use."""


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    qty: float
    group: str
    kind: str
    status: str = ""
    qty_text: str = ""
    notes: str = ""
    material: str = ""
    size: str = ""
    file: str = ""
    used_for: str = ""
    service: str = ""
    unit: str = ""
    outputs: tuple[str, ...] = ()  # declared output files that make it
    hold: str = ""  # what it waits for, from a [[hold]] that names it

    @property
    def shown_qty(self) -> str:
        return self.qty_text or (f"{self.qty:.6g}" if self.qty < 1e6 else f"{self.qty:.0f}") + (f" {self.unit}" if self.unit else "")

    def describe(self) -> dict:
        return {k: list(v) if isinstance(v, tuple) else v for k, v in self.__dict__.items() if v not in ("", None, ())}


@dataclass(frozen=True)
class Group:
    name: str
    kind: str
    note: str
    items: tuple[Item, ...]


@dataclass(frozen=True)
class Bom:
    title: str
    intro: str
    holds: tuple[tuple[str, str, tuple[str, ...]], ...]  # what, wait for, item ids
    groups: tuple[Group, ...]
    outro: str = ""
    items: dict = field(default_factory=dict)  # id -> Item


def bom_path(root: Path) -> Path:
    pyproject = Path(root) / "pyproject.toml"
    name = "bom.toml"
    if pyproject.is_file():
        with pyproject.open("rb") as f:
            name = tomllib.load(f).get("tool", {}).get("workbench", {}).get("bom", name)
    return Path(root) / name


def load(root: Path) -> Bom:
    path = bom_path(root)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise BomError(f"no {path.name} in {root}") from None
    except tomllib.TOMLDecodeError as exc:
        raise BomError(f"{path.name}: {exc}") from None
    groups, items = [], {}
    for g, group in enumerate(data.get("group", [])):
        where = f"{path.name} group {g + 1}"
        name, kind = group.get("name"), group.get("kind")
        if not name or kind not in KINDS:
            raise BomError(f"{where}: needs a name and a kind out of {KINDS}")
        members = []
        for i, raw in enumerate(group.get("item", [])):
            here = f"{where} ({name}) item {i + 1}"
            unknown = set(raw) - {"id", *TEXT_FIELDS, "qty", "outputs"}
            if unknown:
                raise BomError(f"{here}: unknown fields {sorted(unknown)}")
            if not isinstance(raw.get("id"), str) or not raw["id"] or not raw.get("name"):
                raise BomError(f"{here}: needs an id (text) and a name")
            wrong = [key for key in TEXT_FIELDS if key in raw and not isinstance(raw[key], str)]
            if wrong:
                raise BomError(f"{here} ({raw['id']}): {', '.join(wrong)} must be text")
            outputs = raw.get("outputs", [])
            if not isinstance(outputs, list) or not all(isinstance(o, str) and o for o in outputs):
                raise BomError(f"{here} ({raw['id']}): outputs must be a list of output names")
            if outputs and kind not in MADE:
                raise BomError(f"{here} ({raw['id']}): only a made part (sheet, printed, turned) has outputs")
            qty = raw.get("qty")
            if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(qty) or qty < 0:
                raise BomError(f"{here} ({raw['id']}): qty must be a finite number of at least 0")
            if raw.get("status", "") not in ("", *STATUSES):
                raise BomError(f"{here} ({raw['id']}): status must be one of {STATUSES}")
            if raw["id"] in items:
                raise BomError(f"{here}: id {raw['id']!r} is used twice")
            item = Item(
                id=raw["id"], name=raw["name"], qty=float(qty), group=name, kind=kind,
                status=raw.get("status", ""), qty_text=raw.get("qty_text", ""), notes=raw.get("notes", ""),
                material=raw.get("material", ""), size=raw.get("size", ""), file=raw.get("file", ""),
                used_for=raw.get("for", ""), service=raw.get("service", ""), unit=raw.get("unit", ""),
                outputs=tuple(outputs),
            )
            items[item.id] = item
            members.append(item)
        groups.append(Group(name, kind, group.get("note", ""), tuple(members)))
    holds = []
    for h, hold in enumerate(data.get("hold", [])):
        where = f"{path.name} hold {h + 1}"
        what, wait_for, held = hold.get("what"), hold.get("wait_for"), hold.get("items", [])
        if not isinstance(what, str) or not what or not isinstance(wait_for, str) or not wait_for:
            raise BomError(f"{where}: needs what and wait_for, both text")
        if set(hold) - {"what", "wait_for", "items"}:
            raise BomError(f"{where}: unknown fields {sorted(set(hold) - {'what', 'wait_for', 'items'})}")
        if not isinstance(held, list) or not all(isinstance(i, str) for i in held):
            raise BomError(f"{where}: items must be a list of item ids")
        missing = [i for i in held if i not in items]
        if missing:
            raise BomError(f"{where}: items {missing} are not in the bom")
        for i in held:
            items[i] = replace(items[i], hold=wait_for)
        holds.append((what, wait_for, tuple(held)))
    groups = [replace(g, items=tuple(items[i.id] for i in g.items)) for g in groups]
    return Bom(data.get("title", "Parts list"), data.get("intro", ""), tuple(holds), tuple(groups), data.get("outro", ""), items)


def _amount(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def read_inventory(root: Path, bom: Bom, problems: list[str] | None = None) -> dict[str, dict]:
    """The latest usable inventory record for each id. A line that is not JSON, or lacks id or
    have, is an error; a record for an id not in the bom, or with a `have` that is not a number of
    at least 0, is skipped and described in `problems` (when given)."""
    path = Path(root) / INVENTORY_FILE
    problems = [] if problems is None else problems
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    out = {}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise BomError(f"{INVENTORY_FILE}:{number}: not valid JSON: {exc}") from None
        if not isinstance(record, dict) or "id" not in record or "have" not in record:
            raise BomError(f"{INVENTORY_FILE}:{number}: a record needs id and have")
        if not isinstance(record["id"], str) or record["id"] not in bom.items:
            problems.append(f"{INVENTORY_FILE}:{number}: {record['id']!r} is not in the bom (renamed or removed?); skipped")
            continue
        if _amount(record["have"]) is None:
            problems.append(f"{INVENTORY_FILE}:{number}: have {record['have']!r} for {record['id']!r} is not a number of at least 0; skipped")
            continue
        out[record["id"]] = record
    return out


def read_links(root: Path, bom: Bom, problems: list[str] | None = None) -> dict[str, dict]:
    """{id: {"link": latest record, "source": latest "ok" record or None}}. A line that is not JSON,
    or lacks id, url or state, is an error; a record for an id not in the bom, or with an unknown
    state, is skipped and described in `problems`."""
    path = Path(root) / LINKS_FILE
    problems = [] if problems is None else problems
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    out: dict[str, dict] = {}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise BomError(f"{LINKS_FILE}:{number}: not valid JSON: {exc}") from None
        if not isinstance(record, dict) or not all(k in record for k in ("id", "url", "state")):
            raise BomError(f"{LINKS_FILE}:{number}: a record needs id, url and state")
        if not isinstance(record["id"], str) or record["id"] not in bom.items:
            problems.append(f"{LINKS_FILE}:{number}: {record['id']!r} is not in the bom (renamed or removed?); skipped")
            continue
        if record["state"] not in LINK_STATES:
            problems.append(f"{LINKS_FILE}:{number}: state {record['state']!r} is not one of {', '.join(LINK_STATES)}; skipped")
            continue
        entry = out.setdefault(record["id"], {"link": None, "source": None})
        entry["link"] = record
        if record["state"] == "ok":
            entry["source"] = record
    return out


def action_of(item: Item) -> str:
    if item.hold:
        return "on hold"
    if item.status in UNDECIDED:
        return "decide first"
    if item.status == "optional":
        return "optional"
    return "missing" if item.kind == "tools" else "to make" if item.kind in MADE else "to order"


def declared_outputs(root: Path) -> set[str] | None:
    """The output names the project's manifest declares, or None when it has no manifest."""
    try:
        from workbench_kernel.ledger import _project

        return set(_project(Path(root)).outputs)
    except Exception:  # noqa: BLE001 - no manifest, or one that does not import: nothing to check against
        return None


def check_outputs(bom: Bom, declared: set[str]) -> list[str]:
    """Items naming outputs the manifest does not declare."""
    return [f"{item.id}: output {name!r} is not declared on the manifest" for item in bom.items.values() for name in item.outputs if name not in declared]


def status(root: Path) -> dict:
    """Every item with what is needed, had and short, and the totals by kind."""
    bom = load(root)
    problems: list[str] = []
    declared = declared_outputs(root)
    if declared is not None:
        problems += check_outputs(bom, declared)
    inventory = read_inventory(root, bom, problems)
    links = read_links(root, bom, problems)
    rows = []
    for item in bom.items.values():
        record = inventory.get(item.id)
        if record is not None:
            have = float(record["have"])
        else:
            have = item.qty if item.status == "owned" else 0.0
        short = round(max(0.0, item.qty - have), 6)  # no 0.30000000000000004
        rows.append({**item.describe(), "have": have, "short": short, "action": action_of(item), "recorded": record is not None,
                     **{k: v for k, v in links.get(item.id, {}).items() if v is not None}})
    return {
        "title": bom.title,
        "items": rows,
        "short": {action: sum(1 for r in rows if r["short"] > 0 and r["action"] == action) for action in ACTIONS},
        "problems": problems,
    }


def _files(item: Item) -> str:
    return ", ".join([*item.outputs, *([item.file] if item.file else [])])


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def markdown(bom: Bom) -> str:
    """The parts list as markdown (no inventory: that is the owner's own state)."""
    out = [f"# {bom.title}", "", "<!-- Generated from bom.toml by `wbench bom --markdown`; edit bom.toml, not this file. -->", ""]
    if bom.intro:
        out += [bom.intro.strip(), ""]
    if bom.holds:
        out += ["## Hold: do not order or cut before measuring", "", "| What | Wait for |", "|---|---|"]
        out += [f"| {_cell(what)} | {_cell(wait)} |" for what, wait, _ in bom.holds]
        out.append("")
    for group in bom.groups:
        out += [f"## {group.name}", ""]
        if group.note:
            out += [group.note.strip(), ""]
        if group.kind == "tools":
            out += ["| Tool | Qty | For | Instead | Notes |", "|---|---|---|---|---|"]
            out += [f"| {_cell(i.name)} | {i.shown_qty} | {_cell(i.used_for)} | {_cell(i.service)} | {_cell(i.notes)} |" for i in group.items]
        elif group.kind == "sheet":
            out += ["| Part | Qty | Material | Size, mm | File |", "|---|---|---|---|---|"]
            out += [f"| {_cell(i.name)} | {i.shown_qty} | {_cell(i.material)} | {_cell(i.size)} | {_cell(_files(i))} |" for i in group.items]
        elif group.kind in MADE:
            out += ["| Part | Qty | Status | Notes | Files |", "|---|---|---|---|---|"]
            out += [f"| {_cell(i.name)} | {i.shown_qty} | {i.status}{', held' if i.hold else ''} | {_cell(i.notes)} | {_cell(_files(i))} |" for i in group.items]
        else:
            out += ["| Part | Qty | Status | Notes |", "|---|---|---|---|"]
            out += [f"| {_cell(i.name)} | {i.shown_qty} | {i.status}{', held' if i.hold else ''} | {_cell(i.notes)} |" for i in group.items]
        out.append("")
    if bom.outro:
        out += [bom.outro.strip(), ""]
    return "\n".join(out)
