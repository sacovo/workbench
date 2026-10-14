"""The review store: append-only item files, saved views and the generated index.

Layout under the project root:

    review/R-0001.yaml          one item: a YAML stream, one event per document, append only
    review/assets/R-0001/...    files attached to an item (frozen evidence, photos)
    review/views/V-0001.yaml    one saved view, one document, never changed
    review/INDEX.md             generated after every write

Item and view files are created with O_EXCL and item files are only ever opened for appending, so
bytes once written are never rewritten. Writers hold the project lock exclusively
(`.workbench/lock`), readers shared, so a reader never sees half an event. Ids are the highest
existing number plus one, allocated under the lock, so two processes never get the same id. The
CLI calls these functions directly; the hub will call the same ones.

Every event and view is validated (schema.py) and the item re-folded (fold.py) before anything is
written, so an invalid event never reaches a file. Files are validated again when read; an item
that fails is reported as unreadable, never skipped silently.
"""

from __future__ import annotations

import os
import re
import shutil
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from workbench.buildid import build_id
from workbench.errors import Conflict, GitError, InvalidInput, NotFound
from workbench.project import Project, atomic_write
from workbench.review import schema
from workbench.review.fold import fold
from workbench.review.index import render_index
from workbench.review.yamlio import dump_document, load_document, load_documents

REVIEW_DIR = "review"
INDEX_FILE = "INDEX.md"
ITEM_FILE_RE = re.compile(r"^(R-(\d{4,}))\.yaml$")
VIEW_FILE_RE = re.compile(r"^(V-(\d{4,}))\.yaml$")
ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
CREATE_ATTEMPTS = 5

# Field order in written events and views, for readable diffs.
_ORDER = (
    "event", "at", "by", "build", "kind", "title", "to", "via", "status", "path", "assembly",
    "camera", "hidden", "section", "highlight", "note", "targets", "quantity", "unit",
    "corrections", "answers", "approves", "body", "evidence", "questions",
)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_by(role: str, session: str | None = None, name: str | None = None) -> dict:
    by = {"role": role}
    if session is not None:
        by["session"] = session
    if name is not None:
        by["name"] = name
    return by


def compact(fields: dict) -> dict:
    """Drop optional fields left at their empty default (None, "", [], {})."""
    return {k: v for k, v in fields.items() if v is not None and v != "" and v != [] and v != {}}


def _ordered(obj: dict) -> dict:
    rank = {k: i for i, k in enumerate(_ORDER)}
    return dict(sorted(obj.items(), key=lambda kv: rank.get(kv[0], len(rank))))


def _create_exclusive(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


def _append_bytes(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ReviewStore:
    def __init__(self, project: Project):
        self.project = project
        self.dir = project.root / REVIEW_DIR
        self.views_dir = self.dir / "views"
        self.assets_root = self.dir / "assets"
        self.index_path = self.dir / INDEX_FILE

    # -- paths and stamps ----------------------------------------------------------------------

    def item_path(self, item_id: str) -> Path:
        return self.dir / f"{item_id}.yaml"

    def view_path(self, view_id: str) -> Path:
        return self.views_dir / f"{view_id}.yaml"

    def asset_dir(self, item_id: str) -> Path:
        return self.assets_root / item_id

    def rel(self, path: Path) -> str:
        return path.relative_to(self.project.root).as_posix()

    def stamp(self) -> str | None:
        """The current build id, or None outside a git repository or before the first commit."""
        try:
            return build_id(self.project.root)
        except GitError:
            return None

    def _read_lock(self):
        # Nothing to read before the first write; do not create .workbench/ just to look.
        return self.project.lock(exclusive=False) if self.dir.is_dir() else nullcontext()

    def _new_event(self, etype: str, fields: dict, build: str | None) -> dict:
        event = {"event": etype, "at": now()}
        if etype != "delivered":
            event["build"] = build
        event.update(fields)
        return _ordered(event)

    # -- reading -------------------------------------------------------------------------------

    def item_ids(self) -> list[str]:
        return self._ids(self.dir, ITEM_FILE_RE)

    def view_ids(self) -> list[str]:
        return self._ids(self.views_dir, VIEW_FILE_RE)

    @staticmethod
    def _ids(directory: Path, pattern: re.Pattern) -> list[str]:
        if not directory.is_dir():
            return []
        found = []
        for entry in os.scandir(directory):
            m = pattern.match(entry.name)
            if m and entry.is_file():
                found.append((int(m.group(2)), m.group(1)))
        return [name for _, name in sorted(found)]

    def _read_events(self, item_id: str) -> tuple[list[dict], bytes]:
        schema.check_match(item_id, "item id", schema.ITEM_ID_RE, "an item id like R-0001")
        path = self.item_path(item_id)
        where = self.rel(path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise NotFound(f"no item {item_id} ({where} does not exist)") from None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidInput(f"{where}: not UTF-8: {exc}") from None
        docs = load_documents(text, where)
        events = [schema.check_event(doc, f"{where} event {i}", item_id) for i, doc in enumerate(docs)]
        return events, raw

    def events(self, item_id: str) -> list[dict]:
        with self._read_lock():
            return self._read_events(item_id)[0]

    def item(self, item_id: str) -> tuple[list[dict], dict]:
        """(events, state) of one item, read once."""
        with self._read_lock():
            events = self._read_events(item_id)[0]
        return events, fold(events, item_id)

    def state(self, item_id: str) -> dict:
        with self._read_lock():
            return fold(self._read_events(item_id)[0], item_id)

    def states(self) -> tuple[list[dict], list[tuple[str, str]]]:
        """(states of all readable items, [(file name, error)] for the others), in id order."""
        with self._read_lock():
            return self._states()

    def _states(self) -> tuple[list[dict], list[tuple[str, str]]]:
        states, unreadable = [], []
        for item_id in self.item_ids():
            try:
                states.append(fold(self._read_events(item_id)[0], item_id))
            except InvalidInput as exc:
                unreadable.append((f"{item_id}.yaml", str(exc)))
        return states, unreadable

    # -- writing -------------------------------------------------------------------------------

    def open_item(self, fields: dict, attach: list[str | os.PathLike] = ()) -> dict:
        """Create the next item from an open event's `fields` (by, kind, title, ...).

        Files in `attach` are copied to review/assets/<id>/ and added to the evidence as file
        targets, so the evidence a decision rests on is frozen with it (decisions only).
        """
        sources = [Path(p) for p in attach]
        names = [self._check_source(p) for p in sources]
        if sources and fields.get("kind") != "decision":
            raise InvalidInput("attach at open is for a decision's evidence; use `wb attach` for other items")
        if len(set(names)) != len(names):
            raise InvalidInput(f"attach: two files share a name: {names}")
        build = self.stamp()
        # Check everything before taking an id; the asset refs added under the lock are checked
        # there (their files are copied after the item file exists).
        probe = self._new_event("open", fields, build)
        schema.check_event(probe, "open")
        fold([probe], "new item")
        self._check_references(probe)
        with self.project.lock():
            self.dir.mkdir(exist_ok=True)
            for _ in range(CREATE_ATTEMPTS):
                item_id = self._next_id("R", self.dir, ITEM_FILE_RE)
                event = self._new_event("open", fields, build)
                if sources:
                    event["evidence"] = list(event.get("evidence", [])) + [
                        {"kind": "file", "path": self.rel(self.asset_dir(item_id) / n)} for n in names
                    ]
                    event = _ordered(event)
                schema.check_event(event, f"{item_id} open", item_id)
                try:
                    _create_exclusive(self.item_path(item_id), dump_document(event).encode("utf-8"))
                except FileExistsError:
                    continue  # written by something that ignores the lock; take the next number
                break
            else:
                raise Conflict(f"could not allocate an item id in {CREATE_ATTEMPTS} attempts")
            if sources:
                self.asset_dir(item_id).mkdir(parents=True, exist_ok=True)
                for source, name in zip(sources, names, strict=True):
                    shutil.copyfile(source, self.asset_dir(item_id) / name)
            self._write_index()
            return fold([event], item_id)

    def append(self, item_id: str, etype: str, fields: dict) -> dict:
        """Append one event of type `etype` (not open, not attach) and return the new state."""
        if etype in ("open", "attach"):
            raise ValueError(f"use open_item() or attach() for {etype}")
        event = self._new_event(etype, fields, self.stamp())
        schema.check_event(event, f"{item_id} {etype}", item_id)
        self._check_references(event)
        with self.project.lock():
            events, raw = self._read_events(item_id)
            state = fold(events + [event], item_id)
            self._append(item_id, event, raw)
            if etype != "delivered":
                self._write_index()
            return state

    def attach(self, item_id: str, source: str | os.PathLike, by: dict, filename: str | None = None) -> dict:
        """Copy `source` to review/assets/<item>/<filename> and record an attach event."""
        source = Path(source)
        if not source.is_file():
            raise InvalidInput(f"attach: {source} is not a file")
        name = self._check_name(source.name if filename is None else filename)
        build = self.stamp()
        with self.project.lock():
            events, raw = self._read_events(item_id)
            dest = self.asset_dir(item_id) / name
            event = self._new_event("attach", {"by": by, "path": self.rel(dest)}, build)
            schema.check_event(event, f"{item_id} attach", item_id)
            state = fold(events + [event], item_id)
            if dest.exists():
                raise Conflict(f"{self.rel(dest)} exists already; attach under another name")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            self._append(item_id, event, raw)
            self._write_index()
            return state

    def inbox(self, ack: bool = True, session: str | None = None, via: str = "inbox") -> dict:
        """Items with owner events the agent has not had yet, and those events with their authors.

        With `ack`, a `delivered` event is appended to each returned item in the same locked step,
        so an owner event written meanwhile is either returned now or stays unread.
        """
        if not self.dir.is_dir():
            return {"items": [], "unreadable": []}
        items, unreadable = [], []
        with self.project.lock(exclusive=ack):
            for item_id in self.item_ids():
                try:
                    events, raw = self._read_events(item_id)
                    state = fold(events, item_id)
                except InvalidInput as exc:
                    unreadable.append({"file": f"{item_id}.yaml", "error": str(exc)})
                    continue
                if not state["unread_agent"]:
                    continue
                entry = {
                    "id": item_id,
                    "kind": state["kind"],
                    "title": state["title"],
                    "status": state["status"],
                }
                if state["kind"] == "decision":
                    entry["questions"] = state["questions"]
                    entry["open_questions"] = state["open_questions"]
                if state["kind"] == "measurement":
                    entry["quantity"] = state["quantity"]
                    entry["unit"] = state["unit"]
                entry["approved"] = state["approved"]
                entry["events"] = [{"index": i, **events[i]} for i in state["unread_agent"]]
                if ack:
                    fields = compact({"to": session, "via": via})
                    delivered = self._new_event("delivered", fields, None)
                    schema.check_event(delivered, f"{item_id} delivered", item_id)
                    fold(events + [delivered], item_id)
                    self._append(item_id, delivered, raw)
                items.append(entry)
        return {"items": items, "unreadable": unreadable}

    def _append(self, item_id: str, event: dict, raw: bytes) -> None:
        path = self.item_path(item_id)
        if not raw.endswith(b"\n"):
            raise Conflict(f"{self.rel(path)} does not end with a newline: an earlier write was cut short")
        _append_bytes(path, dump_document(event).encode("utf-8"))

    def write_index(self) -> dict:
        with self.project.lock():
            return self._write_index()

    def _write_index(self) -> dict:
        states, unreadable = self._states()
        text = render_index(states, unreadable)
        self.dir.mkdir(exist_ok=True)
        try:
            current = self.index_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = None
        if current != text:
            atomic_write(self.index_path, text)
        return {
            "path": self.rel(self.index_path),
            "items": len(states),
            "unreadable": [name for name, _ in unreadable],
            "changed": current != text,
        }

    # -- views ---------------------------------------------------------------------------------

    def create_view(self, fields: dict) -> dict:
        doc = _ordered({"at": now(), "build": self.stamp(), **fields})
        schema.check_view(doc, "view")
        self._check_references(doc)
        with self.project.lock():
            self.views_dir.mkdir(parents=True, exist_ok=True)
            for _ in range(CREATE_ATTEMPTS):
                view_id = self._next_id("V", self.views_dir, VIEW_FILE_RE)
                try:
                    _create_exclusive(self.view_path(view_id), dump_document(doc).encode("utf-8"))
                except FileExistsError:
                    continue
                return {"id": view_id, **doc}
            raise Conflict(f"could not allocate a view id in {CREATE_ATTEMPTS} attempts")

    def view(self, view_id: str) -> dict:
        schema.check_match(view_id, "view id", schema.VIEW_ID_RE, "a view id like V-0001")
        path = self.view_path(view_id)
        with self._read_lock():
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise NotFound(f"no view {view_id} ({self.rel(path)} does not exist)") from None
        doc = load_document(text, self.rel(path))
        return {"id": view_id, **schema.check_view(doc, self.rel(path))}

    def views(self) -> tuple[list[dict], list[tuple[str, str]]]:
        views, unreadable = [], []
        for view_id in self.view_ids():
            try:
                views.append(self.view(view_id))
            except InvalidInput as exc:
                unreadable.append((f"{view_id}.yaml", str(exc)))
        return views, unreadable

    # -- helpers -------------------------------------------------------------------------------

    @staticmethod
    def _next_id(prefix: str, directory: Path, pattern: re.Pattern) -> str:
        numbers = [int(m.group(2)) for e in os.scandir(directory) if (m := pattern.match(e.name))]
        return f"{prefix}-{max(numbers, default=0) + 1:04d}"

    def _check_name(self, name: str) -> str:
        if not isinstance(name, str) or not ASSET_NAME_RE.match(name) or name.endswith((".", ".lock")):
            raise InvalidInput(f"attachment name {name!r}: use letters, digits and _ . + - (no paths)")
        return name

    def _check_source(self, source: Path) -> str:
        if not source.is_file():
            raise InvalidInput(f"attach: {source} is not a file")
        return self._check_name(source.name)

    def _check_references(self, obj: dict) -> None:
        """View targets, file targets and links must point at something that exists now."""
        targets = [*obj.get("targets", []), *obj.get("evidence", []), *obj.get("highlight", [])]
        for t in targets:
            if t["kind"] == "view" and not self.view_path(t["id"]).is_file():
                raise NotFound(f"target view {t['id']} does not exist")
            if t["kind"] == "file" and not (self.project.root / t["path"]).exists():
                raise NotFound(f"target file {t['path']} does not exist in the project")
        if obj.get("event") == "link":
            to = obj["to"]
            path = self.item_path(to) if to.startswith("R-") else self.view_path(to)
            if not path.is_file():
                raise NotFound(f"cannot link to {to}: it does not exist")
