"""Materials and tools: what a project needs, what the owner has, what is still to order or make.

A project lists its parts and tools in `bom.toml` at its root (or the file named by
`[tool.workbench] bom` in pyproject.toml):

    title = "Parts list"
    intro = "markdown shown at the top"

    [[hold]]
    what = "Lower plate and upper deck"
    wait_for = "The wheel's hub ring width (R-0027)."

    [[group]]
    name = "Arms and servos"
    kind = "bought"            # bought, or made (sheet, printed, turned), or tools
    note = "optional markdown under the heading"

    [[group.item]]
    id = "sts3215"             # unique; the owner's inventory refers to it
    name = "Feetech STS3215, 12 V, 30 kg*cm"
    qty = 9                    # a number; 0 with qty_text "as needed" counts nothing
    qty_text = "8 + 1"         # optional, how the quantity reads
    status = "decided"         # owned, decided, assumed, designed, open, optional
    notes = "..."
    # made parts may give `material`, `size` and `file`; tools give `for` and `service` (who can do
    # the job instead of the tool)

What the owner has is recorded in `ledger/inventory.jsonl`, one JSON object per line, appended and
never rewritten (`wbench have`): {"id": ..., "have": 3, "by": {...}, "at": ..., "note": ...}. The
latest record for an id counts; an item with status "owned" counts as had in full until a record
says otherwise. Each item's shortfall is its quantity less what is had: to order (bought), to make
(made), or missing (tools).
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

INVENTORY_FILE = Path("ledger") / "inventory.jsonl"
KINDS = ("bought", "sheet", "printed", "turned", "tools")
MADE = ("sheet", "printed", "turned")
STATUSES = ("owned", "decided", "assumed", "designed", "open", "optional", "from SO-ARM100")


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

    @property
    def shown_qty(self) -> str:
        return self.qty_text or f"{self.qty:g}"

    def describe(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in ("", None)}


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
    holds: tuple[tuple[str, str], ...]
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
            unknown = set(raw) - {"id", "name", "qty", "qty_text", "status", "notes", "material", "size", "file", "for", "service"}
            if unknown:
                raise BomError(f"{here}: unknown fields {sorted(unknown)}")
            if not raw.get("id") or not raw.get("name"):
                raise BomError(f"{here}: needs an id and a name")
            qty = raw.get("qty")
            if isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty < 0:
                raise BomError(f"{here} ({raw['id']}): qty must be a number of at least 0")
            if raw.get("status", "") not in ("", *STATUSES):
                raise BomError(f"{here} ({raw['id']}): status must be one of {STATUSES}")
            if raw["id"] in items:
                raise BomError(f"{here}: id {raw['id']!r} is used twice")
            item = Item(
                id=raw["id"], name=raw["name"], qty=float(qty), group=name, kind=kind,
                status=raw.get("status", ""), qty_text=raw.get("qty_text", ""), notes=raw.get("notes", ""),
                material=raw.get("material", ""), size=raw.get("size", ""), file=raw.get("file", ""),
                used_for=raw.get("for", ""), service=raw.get("service", ""),
            )
            items[item.id] = item
            members.append(item)
        groups.append(Group(name, kind, group.get("note", ""), tuple(members)))
    holds = tuple((h["what"], h["wait_for"]) for h in data.get("hold", []))
    return Bom(data.get("title", "Parts list"), data.get("intro", ""), holds, tuple(groups), data.get("outro", ""), items)


def read_inventory(root: Path, bom: Bom) -> dict[str, dict]:
    """The latest inventory record for each id."""
    path = Path(root) / INVENTORY_FILE
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
        if record["id"] not in bom.items:
            raise BomError(f"{INVENTORY_FILE}:{number}: {record['id']!r} is not in the bom")
        out[record["id"]] = record
    return out


def status(root: Path) -> dict:
    """Every item with what is needed, had and short, and the totals by kind."""
    bom = load(root)
    inventory = read_inventory(root, bom)
    rows = []
    for item in bom.items.values():
        record = inventory.get(item.id)
        if record is not None:
            have = float(record["have"])
        else:
            have = item.qty if item.status == "owned" else 0.0
        short = max(0.0, item.qty - have)
        action = "missing" if item.kind == "tools" else "to make" if item.kind in MADE else "to order"
        rows.append({**item.describe(), "have": have, "short": short, "action": action, "recorded": record is not None})
    return {
        "title": bom.title,
        "items": rows,
        "short": {action: sum(1 for r in rows if r["short"] > 0 and r["action"] == action) for action in ("to order", "to make", "missing")},
    }


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def markdown(bom: Bom) -> str:
    """The parts list as markdown (no inventory: that is the owner's own state)."""
    out = [f"# {bom.title}", "", "<!-- Generated from bom.toml by `wbench bom --markdown`; edit bom.toml, not this file. -->", ""]
    if bom.intro:
        out += [bom.intro.strip(), ""]
    if bom.holds:
        out += ["## Hold: do not order or cut before measuring", "", "| What | Wait for |", "|---|---|"]
        out += [f"| {_cell(what)} | {_cell(wait)} |" for what, wait in bom.holds]
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
            out += [f"| {_cell(i.name)} | {i.shown_qty} | {_cell(i.material)} | {_cell(i.size)} | {_cell(i.file)} |" for i in group.items]
        else:
            out += ["| Part | Qty | Status | Notes |", "|---|---|---|---|"]
            out += [f"| {_cell(i.name)} | {i.shown_qty} | {i.status} | {_cell(i.notes)} |" for i in group.items]
        out.append("")
    if bom.outro:
        out += [bom.outro.strip(), ""]
    return "\n".join(out)
