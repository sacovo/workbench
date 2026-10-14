"""Review operations: items (ask, note, comment, reply, answer, set-status, attach, link, inbox,
item, index) and saved views (view-create, view-list, view-get).

They run without a hub: each writes the files under the project lock. The hub will call the same
functions. Roles are declared by the caller, not authenticated: `answer` and `comment` always
write as the owner, `ask` and `note` as the agent or a reviewer, and `reply`, `set-status`,
`attach`, `link` and `view-create` take `--role` (default agent, the CLI's usual caller; the UI
passes owner).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

from workbench.ops.registry import op
from workbench.project import Project
from workbench.review import ReviewStore, compact, make_by

Role = Literal["owner", "agent", "reviewer"]
Author = Literal["agent", "reviewer"]
Status = Literal["waiting_owner", "waiting_agent", "done", "dropped"]
NoteKind = Literal["comment", "finding", "measurement", "change"]

WHOSE = {"waiting_owner": "owner's move", "waiting_agent": "agent's move", "done": "done", "dropped": "dropped"}

_TARGETS_HELP = (
    "JSON list of targets: {kind: part, label, assembly?} | {kind: param, key: \"robot.params:BASE.wheel_width\"} | "
    "{kind: fact, key} | {kind: file, path, line?} | {kind: view, id}"
)
_COMMON = {
    "id": "item id, e.g. R-0001",
    "title": "one line, shown in the inbox and in review/INDEX.md",
    "body": "markdown",
    "targets": _TARGETS_HELP,
    "session": "the agent session id, recorded with the event",
    "name": "a name for the author, e.g. reviewer-2",
}


def _help(*common: str, **extra: str) -> dict[str, str]:
    """Help texts for one operation: the shared ones it names, plus its own."""
    return {**{k: _COMMON[k] for k in common}, **extra}


# -- human-readable output ---------------------------------------------------------------------

def _who(by: dict) -> str:
    return by["role"] + (f" ({by['name']})" if "name" in by else "")


def _target(t: dict) -> str:
    if t["kind"] == "part":
        return f"part {t['label']}" + (f" in {t['assembly']}" if "assembly" in t else "")
    if t["kind"] == "file":
        return f"file {t['path']}" + (f":{t['line']}" if "line" in t else "")
    if t["kind"] == "view":
        return f"view {t['id']}"
    return f"{t['kind']} {t['key']}"


def _indent(text: str, prefix: str = "    ") -> list[str]:
    return [prefix + line if line else "" for line in text.rstrip("\n").splitlines()]


def _event_lines(ev: dict, questions: list[dict]) -> list[str]:
    who = _who(ev["by"]) if "by" in ev else "wb"
    etype = ev["event"]
    lines: list[str] = []
    if etype == "open":
        lines.append(f"  {who} opened it ({ev['kind']}, {ev['at']}):")
        for t in ev.get("targets", []):
            lines.append(f"    target: {_target(t)}")
    elif etype == "reply":
        lines.append(f"  {who} replied ({ev['at']})" + (f", status {ev['status']}" if "status" in ev else "") + ":")
    elif etype == "answer":
        lines.append(f"  {who} answered ({ev['at']}):")
        by_id = {q["id"]: q for q in questions}
        for qid, answer in ev.get("answers", {}).items():
            question = by_id.get(qid, {"text": "?", "options": []})
            labels = {o["id"]: o["label"] for o in question["options"]}
            parts = []
            if "choice" in answer:
                parts.append(f"{answer['choice']}: {labels.get(answer['choice'], '?')}")
            if "text" in answer:
                parts.append(f"\"{answer['text']}\"")
            lines.append(f"    {qid} ({question['text']}) -> {'; '.join(parts)}")
        for key, value in ev.get("approves", {}).items():
            lines.append(f"    approves {key} = {json.dumps(value, ensure_ascii=False)}")
    elif etype == "status":
        lines.append(f"  {who} set status {ev['to']} ({ev['at']})")
    elif etype == "attach":
        lines.append(f"  {who} attached {ev['path']} ({ev['at']})")
    elif etype == "link":
        lines.append(f"  {who} linked {ev['to']} ({ev['at']})")
    elif etype == "delivered":
        lines.append(f"  delivered to the agent ({ev['at']}" + (f", session {ev['to']}" if "to" in ev else "") + ")")
    if ev.get("body"):
        lines += _indent(ev["body"])
    return lines


def _header(s: dict) -> str:
    extra = []
    if s.get("questions"):
        extra.append(f"{len(s['open_questions'])} of {len(s['questions'])} questions open")
    extra.append(WHOSE[s["status"]])
    return f"{s['id']} {s['kind']} \"{s['title']}\" ({'; '.join(extra)})"


def _human_opened(s: dict) -> str:
    return f"{s['id']} opened: {s['kind']} \"{s['title']}\" ({WHOSE[s['status']]})"


def _human_recorded(s: dict) -> str:
    last = s["last_activity"]
    text = f"{s['id']}: {last['event']} recorded ({WHOSE[s['status']]})"
    if s.get("questions"):
        text += f"; {len(s['open_questions'])} of {len(s['questions'])} questions open"
    if s["approved"]:
        text += "; approved " + ", ".join(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in s["approved"].items())
    return text


def _human_inbox(r: dict) -> str:
    lines: list[str] = []
    for entry in r["items"]:
        lines.append(_header(entry))
        for ev in entry["events"]:
            lines += _event_lines(ev, entry.get("questions", []))
        if entry.get("open_questions"):
            by_id = {q["id"]: q for q in entry["questions"]}
            lines.append("  open questions: " + "; ".join(f"{q} ({by_id[q]['text']})" for q in entry["open_questions"]))
    for bad in r["unreadable"]:
        lines.append(f"unreadable: {bad['file']}: {bad['error']}")
    n = len(r["items"])
    if not n:
        lines.append("inbox empty")
    else:
        lines.append(f"{n} item{'s' if n != 1 else ''}" + (", marked delivered" if r["acknowledged"] else ", not acknowledged"))
    return "\n".join(lines)


def _human_item(s: dict) -> str:
    lines = [_header(s), f"  opened by {_who(s['opened']['by'])} at {s['opened']['at']}, build {s['opened']['build']}"]
    for c in s.get("corrections", []):
        lines.append(f"  correction: {c}")
    if s["body"] and "events" not in s:
        lines += _indent(s["body"])
    for t in s["targets"]:
        lines.append(f"  target: {_target(t)}")
    for t in s.get("evidence", []):
        lines.append(f"  evidence: {_target(t)}")
    if s["kind"] == "measurement":
        lines.append(f"  quantity: {s['quantity']} in {s['unit']}")
    for q in s.get("questions", []):
        state = "answered" if q["id"] in s["answers"] else "open"
        choices = [f"{o['id']}: {o['label']}" for o in q["options"]]
        if q.get("free_text"):
            choices.append("free text")
        rec = f", recommended {q['recommended']}" if "recommended" in q else ""
        lines.append(f"  {q['id']} [{state}] {q['text']} ({', '.join(choices)}{rec})")
    for key, value in s["approved"].items():
        lines.append(f"  approved: {key} = {json.dumps(value, ensure_ascii=False)}")
    for path in s["attachments"]:
        lines.append(f"  attachment: {path}")
    for to in s["links"]:
        lines.append(f"  link: {to}")
    for ev in s.get("events", []):
        lines += _event_lines(ev, s.get("questions", []))
    return "\n".join(lines)


def _human_index(r: dict) -> str:
    text = f"{r['path']}: {r['items']} items" + ("" if r["changed"] else " (unchanged)")
    if r["unreadable"]:
        text += f"; unreadable: {', '.join(r['unreadable'])}"
    return text


def _human_view(v: dict) -> str:
    return f"{v['id']} {v['assembly']}" + (f": {v['note'].splitlines()[0]}" if v.get("note") else "") + f" ({_who(v['by'])}, {v['at']})"


def _human_view_list(r: dict) -> str:
    lines = [_human_view(v) for v in r["views"]] or ["no saved views"]
    lines += [f"unreadable: {name}" for name in r["unreadable"]]
    return "\n".join(lines)


def _human_view_get(v: dict) -> str:
    return json.dumps(v, indent=2, ensure_ascii=False)


# -- items -------------------------------------------------------------------------------------

@op(
    "ask",
    summary="Open a decision request for the owner (agent)",
    human=_human_opened,
    args=_help(
        "title",
        "body",
        "targets",
        "session",
        "name",
        questions=(
            'JSON list: [{"id": "q1", "text": "...", "options": [{"id": "a", "label": "...", "effect": '
            '"prose" or {"robot.params:RULES.front_limit": 75}}], "recommended": "a", "free_text": false}]'
        ),
        corrections="JSON list of strings, shown before everything else",
        evidence="JSON list of targets the decision rests on (same forms as targets)",
        attach="JSON list of local files to freeze as evidence (copied to review/assets/<id>/)",
        role="who asks: agent (default) or reviewer",
    ),
)
def ask(
    project: Project,
    title: str,
    questions: list[dict],
    body: str = "",
    corrections: Optional[list[str]] = None,
    evidence: Optional[list[dict]] = None,
    targets: Optional[list[dict]] = None,
    attach: Optional[list[str]] = None,
    role: Author = "agent",
    session: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    """Open a decision: corrections first, the evidence, then questions with options the owner can
    answer one at a time. An option's effect may be a mapping of values; choosing it approves them."""
    fields = {
        "by": make_by(role, session, name),
        "kind": "decision",
        "title": title,
        "questions": questions,
        **compact({"targets": targets, "body": body, "corrections": corrections, "evidence": evidence}),
    }
    return ReviewStore(project).open_item(fields, attach=attach or [])


@op(
    "note",
    summary="Open a comment, finding, measurement request or change note (agent or reviewer)",
    human=_human_opened,
    args=_help(
        "title",
        "body",
        "targets",
        "session",
        "name",
        kind="comment, finding, measurement (a request to the owner to measure something) or change",
        quantity="measurement only: the key of the quantity, e.g. robot.params:BASE.wheel_width",
        unit="measurement only: the unit, e.g. mm",
        role="who writes: agent (default) or reviewer",
    ),
)
def note(
    project: Project,
    kind: NoteKind,
    title: str,
    body: str = "",
    targets: Optional[list[dict]] = None,
    quantity: Optional[str] = None,
    unit: Optional[str] = None,
    role: Author = "agent",
    session: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    """Open an item that is not a decision."""
    fields = {"by": make_by(role, session, name), "kind": kind, "title": title, **compact({"targets": targets, "body": body})}
    if quantity is not None:
        fields["quantity"] = quantity
    if unit is not None:
        fields["unit"] = unit
    return ReviewStore(project).open_item(fields)


@op(
    "comment",
    summary="Open a comment as the owner (used by the UI)",
    human=_human_opened,
    args=_help("title", "body", "targets", "name"),
)
def comment(
    project: Project,
    title: str,
    body: str = "",
    targets: Optional[list[dict]] = None,
    name: Optional[str] = None,
) -> dict:
    """The owner's own comment on a part, parameter, fact, file or view; it is the agent's move."""
    fields = {"by": make_by("owner", None, name), "kind": "comment", "title": title, **compact({"targets": targets, "body": body})}
    return ReviewStore(project).open_item(fields)


@op(
    "inbox",
    summary="Owner events the agent has not had yet, with their authors; marks them delivered",
    human=_human_inbox,
    args=_help("session", ack="append a delivered event to each item returned (--no-ack: just look)"),
)
def inbox(project: Project, ack: bool = True, session: Optional[str] = None) -> dict:
    """Every item with owner events after its last delivery: title, kind, whose move, open
    questions, approved values and the new events with their authors. Unless --no-ack, each
    returned item gets a delivered event, so the next inbox does not return the same events."""
    result = ReviewStore(project).inbox(ack=ack, session=session, via="inbox")
    result["acknowledged"] = ack and bool(result["items"])
    return result


@op(
    "item",
    summary="State of one item, optionally with all its events",
    human=_human_item,
    args=_help("id", events="include every event"),
)
def item(project: Project, id: str, events: bool = False) -> dict:
    """The folded state: whose move, open questions, answers, approved values, unread counts."""
    evs, state = ReviewStore(project).item(id)
    if events:
        state["events"] = [{"index": i, **ev} for i, ev in enumerate(evs)]
    return state


@op(
    "reply",
    summary="Reply to an item; the move goes to the other side unless --status says otherwise",
    human=_human_recorded,
    args=_help("id", "body", "session", "name", status="set whose move it is, or done / dropped", role="who replies (default agent)"),
)
def reply(
    project: Project,
    id: str,
    body: str,
    status: Optional[Status] = None,
    role: Role = "agent",
    session: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    fields = {"by": make_by(role, session, name), "body": body}
    if status is not None:
        fields["status"] = status
    return ReviewStore(project).append(id, "reply", fields)


@op(
    "answer",
    summary="Answer a decision's questions, all or some, or record approved values (owner)",
    human=_human_recorded,
    args=_help(
        "id",
        "name",
        answers='JSON object: {"q1": {"choice": "b"}, "q2": {"text": "..."}}; unanswered questions stay open',
        approves='JSON object of approved values, e.g. {"robot.params:BASE.wheel_width": 62}',
        body="remarks (markdown)",
    ),
)
def answer(
    project: Project,
    id: str,
    answers: Optional[dict] = None,
    approves: Optional[dict] = None,
    body: str = "",
    name: Optional[str] = None,
) -> dict:
    """The owner's answer. Choosing an option whose effect is a mapping approves those values;
    values given in approves are added and win over an option's."""
    store = ReviewStore(project)
    with project.lock():
        _, state = store.item(id)
        merged: dict = {}
        questions = {q["id"]: q for q in state.get("questions", [])}
        for qid, chosen in (answers or {}).items():
            choice = chosen.get("choice") if isinstance(chosen, dict) else None
            for option in questions.get(qid, {}).get("options", []):
                if option["id"] == choice and isinstance(option.get("effect"), dict):
                    merged.update(option["effect"])
        merged.update(approves or {})
        fields = {"by": make_by("owner", None, name), **compact({"answers": answers, "approves": merged, "body": body})}
        return store.append(id, "answer", fields)


@op(
    "set-status",
    summary="Set whose move it is, or close an item as done or dropped",
    human=_human_recorded,
    args=_help("id", "session", "name", to="waiting_owner, waiting_agent, done or dropped", role="who sets it (default agent)"),
)
def set_status(
    project: Project,
    id: str,
    to: Status,
    role: Role = "agent",
    session: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    return ReviewStore(project).append(id, "status", {"by": make_by(role, session, name), "to": to})


@op(
    "attach",
    summary="Copy a file to review/assets/<item>/ and attach it to the item",
    human=_human_recorded,
    args=_help(
        "id",
        "session",
        "name",
        source="the file to copy (relative to the current directory)",
        filename="name under review/assets/<item>/ (default: the source's name)",
        role="who attaches (default agent)",
    ),
)
def attach(
    project: Project,
    id: str,
    source: str,
    filename: Optional[str] = None,
    role: Role = "agent",
    session: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    return ReviewStore(project).attach(id, Path(source), make_by(role, session, name), filename)


@op(
    "link",
    summary="Link an item to another item or a saved view",
    human=_human_recorded,
    args=_help("id", "session", "name", to="R-... or V-...", role="who links (default agent)"),
)
def link(
    project: Project,
    id: str,
    to: str,
    role: Role = "agent",
    session: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    return ReviewStore(project).append(id, "link", {"by": make_by(role, session, name), "to": to})


@op("index", summary="Regenerate review/INDEX.md from the item files", human=_human_index)
def index(project: Project) -> dict:
    """Every write regenerates the index already; this is for after a merge or a hand edit."""
    return ReviewStore(project).write_index()


# -- views -------------------------------------------------------------------------------------

@op(
    "view-create",
    summary="Save a view (assembly, camera, hidden parts, section, highlights, note) either side can open",
    human=_human_view,
    args=_help(
        "session",
        "name",
        assembly="the assembly shown",
        camera='JSON object: {"position": [x, y, z], "target": [x, y, z], "quaternion": [x, y, z, w], "zoom": 1.0}',
        hidden="JSON list of part labels hidden in the view",
        section='JSON object: {"planes": [{"normal": [0, 0, 1], "offset": 120.0}], "intersection": false}',
        highlight="JSON list of targets to highlight (same forms as targets)",
        note="markdown",
        role="who saves it (default agent)",
    ),
)
def view_create(
    project: Project,
    assembly: str,
    camera: dict,
    hidden: Optional[list[str]] = None,
    section: Optional[dict] = None,
    highlight: Optional[list[dict]] = None,
    note: str = "",
    role: Role = "agent",
    session: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    fields = {
        "by": make_by(role, session, name),
        "assembly": assembly,
        "camera": camera,
        **compact({"hidden": hidden, "section": section, "highlight": highlight, "note": note}),
    }
    return ReviewStore(project).create_view(fields)


@op("view-list", summary="List saved views", human=_human_view_list)
def view_list(project: Project) -> dict:
    views, unreadable = ReviewStore(project).views()
    return {
        "views": [
            {k: v[k] for k in ("id", "assembly", "note", "by", "at", "build") if k in v} for v in views
        ],
        "unreadable": [name for name, _ in unreadable],
    }


@op("view-get", summary="One saved view", human=_human_view_get, args={"id": "view id, e.g. V-0001"})
def view_get(project: Project, id: str) -> dict:
    return ReviewStore(project).view(id)
