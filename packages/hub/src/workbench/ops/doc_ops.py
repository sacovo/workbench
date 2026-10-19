"""Documents and shop carts for the materials list (workbench_kernel.bom): `wbench doc-add`,
`doc-edit`, `doc-remove`, `docs` and `cart`.

Owner's decision R-0260 in my-robot. Documents (datasheets, order confirmations, receipts, manuals)
are copied into the project under `documents/<kind>s/`, tracked with it, and described in
`ledger/documents.jsonl` with the bom items they belong to. The agent reads them like any other
file and may add datasheets, manuals and other documents it fetched; order confirmations and
receipts come only from the owner. A cart mark (`ledger/carts.jsonl`) says the owner put an item
in a shop's cart; it holds until the owner clears it. No order records beyond that.

The page uploads a file through `POST /api/page/document` (workbench.hub.app), which calls
`store_document` here with the bytes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Literal, Optional

from workbench.errors import InvalidInput
from workbench.ops.bom_ops import _web_address
from workbench.ops.ledger_ops import kernel_json
from workbench.ops.registry import op
from workbench.ops.review_ops import Role
from workbench.project import Project
from workbench.review.store import make_by, now

DOCUMENTS_DIR = "documents"  # as workbench_kernel.bom.DOCUMENTS_DIR
DOCUMENTS_FILE = "ledger/documents.jsonl"  # as workbench_kernel.bom.DOCUMENTS_FILE
CARTS_FILE = "ledger/carts.jsonl"  # as workbench_kernel.bom.CARTS_FILE
Kind = Literal["datasheet", "order", "receipt", "manual", "other"]
KINDS = ("datasheet", "order", "receipt", "manual", "other")  # as workbench_kernel.bom.DOCUMENT_KINDS
OWNER_KINDS = ("order", "receipt")  # the owner's own papers: the agent does not file them
FOLDER = {"datasheet": "datasheets", "order": "orders", "receipt": "receipts", "manual": "manuals", "other": "other"}
MAX_DOCUMENT = 64 * 2**20  # bytes
_UNSAFE = re.compile(r"[^\w.()+ -]+")


def _append(project: Project, name: str, record: dict) -> None:
    path = project.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _bom(project: Project) -> dict:
    return kernel_json(project, "bom")


def _check_items(bom: dict, items: list[str]) -> list[str]:
    known = {row["id"] for row in bom["items"]}
    unknown = [i for i in items if i not in known]
    if unknown:
        raise InvalidInput(f"not in the bom: {', '.join(map(repr, unknown))}")
    return list(dict.fromkeys(items))


def _check_role(kind: str, role: str) -> None:
    if kind not in KINDS:
        raise InvalidInput(f"kind is one of {', '.join(KINDS)}, got {kind!r}")
    if kind in OWNER_KINDS and role != "owner":
        raise InvalidInput(f"only the owner files a {kind} (R-0260); the agent adds datasheets, manuals and other documents")


def safe_name(filename: str) -> str:
    """The file's own name, with characters outside letters, digits and `.()+ -_` made `_`."""
    name = _UNSAFE.sub("_", Path(filename.replace("\\", "/")).name).strip(" .")
    if not name:
        raise InvalidInput(f"no usable file name in {filename!r}")
    return name[:120]


def _text(value: str, what: str) -> str:
    value = value.strip()
    if "\n" in value:
        raise InvalidInput(f"the {what} is one line")
    return value


def _current(project: Project) -> dict[str, dict]:
    """The latest record for each document path still listed (unchecked against the bom)."""
    out: dict[str, dict] = {}
    path = project.root / DOCUMENTS_FILE
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and isinstance(record.get("path"), str):
                if record.get("removed"):
                    out.pop(record["path"], None)
                else:
                    out[record["path"]] = record
    return out


def store_document(project: Project, data: bytes, filename: str, kind: str, items: list[str], title: str = "", note: str = "",
                   role: str = "agent", session: Optional[str] = None, name: Optional[str] = None) -> dict:
    """Copies `data` to documents/<kind>s/<name> and records it. The same content under the same
    name is recorded again (new items, title, note); different content under a taken name gets a
    `-2`, `-3`, ... suffix."""
    _check_role(kind, role)
    if not data:
        raise InvalidInput("the file is empty")
    if len(data) > MAX_DOCUMENT:
        raise InvalidInput(f"the file is larger than {MAX_DOCUMENT // 2**20} MB")
    items = _check_items(_bom(project), items)
    title, note = _text(title, "title"), _text(note, "note")
    sha = hashlib.sha256(data).hexdigest()
    stem, suffix = Path(safe_name(filename)).stem, Path(safe_name(filename)).suffix
    folder = project.root / DOCUMENTS_DIR / FOLDER[kind]
    with project.lock():
        for n in range(1, 1000):
            dest = folder / (f"{stem}{suffix}" if n == 1 else f"{stem}-{n}{suffix}")
            if not dest.exists() or hashlib.sha256(dest.read_bytes()).hexdigest() == sha:
                break
        else:
            raise InvalidInput(f"too many files named {stem}{suffix} in {folder.relative_to(project.root)}")
        rel = dest.relative_to(project.root).as_posix()
        if not dest.exists():
            folder.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        record = {"path": rel, "sha256": sha, "size": len(data), "kind": kind, "items": items,
                  **({"title": title} if title else {}), **({"note": note} if note else {}), "by": make_by(role, session, name), "at": now()}
        _append(project, DOCUMENTS_FILE, record)
    return {"record": record}


def _human_doc(r: dict) -> str:
    rec = r["record"]
    if rec.get("removed"):
        return f"removed {rec['path']}"
    items = f" for {', '.join(rec['items'])}" if rec.get("items") else ""
    return f"recorded {rec['kind']} {rec['path']}{items}"


@op(
    "doc-add",
    summary="Copy a document (datasheet, order, receipt, manual) into documents/ and link it to bom items",
    human=_human_doc,
    args={
        "source": "the file to copy (relative to the current directory)", "kind": "datasheet, order, receipt, manual or other (order and receipt: owner only)",
        "items": "JSON list of bom item ids it belongs to", "title": "one line, e.g. the order number", "note": "one line",
        "filename": "name under documents/<kind>s/ (default: the source's name)", "role": "who adds it (default agent)",
        "session": "the agent's session", "name": "a name for the author",
    },
)
def doc_add(project: Project, source: str, kind: Kind, items: list[str] = [], title: str = "", note: str = "", filename: Optional[str] = None,  # noqa: B006 - bind() copies defaults
            role: Role = "agent", session: Optional[str] = None, name: Optional[str] = None) -> dict:
    path = Path(source)
    if not path.is_file():
        raise InvalidInput(f"{source} is not a file")
    return store_document(project, path.read_bytes(), filename or path.name, kind, items, title, note, role, session, name)


@op(
    "doc-edit",
    summary="Change which bom items a document belongs to, its title or note",
    human=_human_doc,
    args={"path": "the document, as listed (documents/...)", "items": "JSON list of bom item ids (replaces the list)", "title": "one line",
          "note": "one line", "role": "who edits it (default agent)", "session": "the agent's session", "name": "a name for the author"},
)
def doc_edit(project: Project, path: str, items: Optional[list[str]] = None, title: Optional[str] = None, note: Optional[str] = None,
             role: Role = "agent", session: Optional[str] = None, name: Optional[str] = None) -> dict:
    with project.lock():
        current = _current(project).get(path)
        if current is None:
            raise InvalidInput(f"no document {path!r} (wbench docs)")
        _check_role(current["kind"], role)
        record = {k: v for k, v in current.items() if k not in ("by", "at")}
        if items is not None:
            record["items"] = _check_items(_bom(project), items)
        for key, value in (("title", title), ("note", note)):
            if value is not None:
                record.pop(key, None)
                if _text(value, key):
                    record[key] = _text(value, key)
        record.update(by=make_by(role, session, name), at=now())
        _append(project, DOCUMENTS_FILE, record)
    return {"record": record}


@op(
    "doc-remove",
    summary="Delete a document's file and take it off the list (owner)",
    owner_only=True,
    human=_human_doc,
    args={"path": "the document, as listed (documents/...)", "name": "a name for the owner"},
)
def doc_remove(project: Project, path: str, name: Optional[str] = None) -> dict:
    """A file already committed stays in git's history; an uncommitted one is gone."""
    with project.lock():
        if path not in _current(project):
            raise InvalidInput(f"no document {path!r} (wbench docs)")
        (project.root / path).unlink(missing_ok=True)
        record = {"path": path, "removed": True, "by": make_by("owner", None, name), "at": now()}
        _append(project, DOCUMENTS_FILE, record)
    return {"record": record}


def _human_docs(r: dict) -> str:
    if not r["documents"]:
        return "no documents"
    lines = []
    for d in r["documents"]:
        extra = "; ".join(filter(None, [d.get("title"), ", ".join(d["items"]), d.get("note"), "file missing" if d.get("missing") else ""]))
        lines.append(f"{d['kind']:<10} {d['path']}" + (f"  ({extra})" if extra else ""))
    return "\n".join(lines)


@op("docs", summary="The project's documents, with the bom items each belongs to", human=_human_docs,
    args={"item": "only the documents of this bom item", "kind": "only this kind"})
def docs(project: Project, item: str = "", kind: str = "") -> dict:
    found = _bom(project).get("documents", [])
    return {"documents": [d for d in found if (not item or item in d["items"]) and (not kind or d["kind"] == kind)]}


@op(
    "cart",
    summary="Mark an item as put in a shop's cart, or clear the mark (owner)",
    owner_only=True,
    human=lambda r: f"{r['record']['id']}: {r['record']['state']} at {r['record']['shop']}",
    args={"id": "the item's id in bom.toml", "shop": "the shop's name", "url": "the cart or product page", "qty": "how many went in",
          "clear": "take the mark off (bought, or taken out of the cart)", "name": "a name for the owner"},
)
def cart(project: Project, id: str, shop: str, url: str = "", qty: Optional[float] = None, clear: bool = False, name: Optional[str] = None) -> dict:
    shop = _text(shop, "shop's name")
    if not shop:
        raise InvalidInput("the shop's name is needed")
    if qty is not None and (isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(qty) or qty <= 0):
        raise InvalidInput(f"qty must be a number above 0, got {qty!r}")
    _check_items(_bom(project), [id])
    record = {"id": id, "shop": shop, "state": "cleared" if clear else "in cart",
              **({"url": _web_address(url)} if url.strip() and not clear else {}), **({"qty": qty} if qty is not None and not clear else {}),
              "by": make_by("owner", None, name), "at": now()}
    with project.lock():
        _append(project, CARTS_FILE, record)
    return {"record": record}
