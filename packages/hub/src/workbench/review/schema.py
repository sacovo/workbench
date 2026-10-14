"""The review item event schema, validated strictly.

Every event and saved view is checked field by field before it is written and again when it is
read: unknown fields, missing fields, wrong types, "true" for a boolean, 1 for a boolean, true for
an integer and empty strings where text is required are all errors naming the field. Checks that
need the earlier events of the item (does question q2 exist, is option b one of its options) live
in `fold.py`.

Events (one YAML document each, appended to review/R-0001.yaml):

    open       at, by, build, kind, title, [targets, body]
               decision:    questions (required), [corrections, evidence]
               measurement: quantity, unit (both required)
    reply      at, by, build, body, [status]
    answer     at, by (owner), build, [answers, approves, body]; answers or approves non-empty
    status     at, by, build, to
    delivered  at, [to (agent session id), via (channel)]
    attach     at, by, build, path (review/assets/<item id>/...)
    link       at, by, build, to (another item R-... or a view V-...)

    by        {role: owner | agent | reviewer, [session, name]}
    build     a build id, or null when the project is not in a git repository with a commit
    at        UTC, "2026-10-14T06:37:38Z"
    question  {id, text, options: [{id, label, [effect]}], [recommended, free_text]}
              effect is prose, or a mapping of values that choosing the option approves
    answers   {question id: {[choice], [text]}}; text needs free_text: true on the question
    approves  {key: JSON value}, e.g. {"robot.params:RULES.front_limit": 75}

Targets (item targets, decision evidence, view highlights):

    {kind: part, label, [assembly]}
    {kind: param, key: "robot.params:BASE.wheel_width"}
    {kind: fact, key}
    {kind: file, path, [line]}      path relative to the project root, inside it
    {kind: view, id: "V-0001"}
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import PurePosixPath

from workbench.buildid import BUILD_ID_RE
from workbench.errors import InvalidInput

KINDS = ("comment", "finding", "decision", "measurement", "change")
ROLES = ("owner", "agent", "reviewer")
STATUSES = ("waiting_owner", "waiting_agent", "done", "dropped")
EVENT_TYPES = ("open", "reply", "answer", "status", "delivered", "attach", "link")
TARGET_KINDS = ("part", "param", "fact", "file", "view")

ITEM_ID_RE = re.compile(r"^R-\d{4,}$")
VIEW_ID_RE = re.compile(r"^V-\d{4,}$")
LOCAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")  # question and option ids
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
PARAM_KEY_RE = re.compile(rf"^{_IDENT}(\.{_IDENT})*:{_IDENT}(\.{_IDENT})*$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_EVENT_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # event type: (required, optional)
    "open": (
        ("event", "at", "by", "build", "kind", "title"),
        ("targets", "body", "corrections", "evidence", "questions", "quantity", "unit"),
    ),
    "reply": (("event", "at", "by", "build", "body"), ("status",)),
    "answer": (("event", "at", "by", "build"), ("answers", "approves", "body")),
    "status": (("event", "at", "by", "build", "to"), ()),
    "delivered": (("event", "at"), ("to", "via")),
    "attach": (("event", "at", "by", "build", "path"), ()),
    "link": (("event", "at", "by", "build", "to"), ()),
}
_DECISION_ONLY = ("questions", "corrections", "evidence")
_MEASUREMENT_ONLY = ("quantity", "unit")


# ---------------------------------------------------------------------------------------------
# Primitive checks; each raises InvalidInput naming the field

def fail(where: str, message: str):
    raise InvalidInput(f"{where}: {message}")


def type_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    return {int: "integer", float: "number", str: "string", list: "list", dict: "mapping"}.get(
        type(value), type(value).__name__
    )


def _shown(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def check_fields(obj: object, where: str, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict:
    if not isinstance(obj, dict):
        fail(where, f"expected a mapping, got {type_name(obj)}")
    for key in obj:
        if not isinstance(key, str):
            fail(where, f"field names must be strings, got {type_name(key)} {_shown(key)}")
    allowed = required + optional
    unknown = [k for k in obj if k not in allowed]
    if unknown:
        fail(where, f"unknown field(s) {', '.join(sorted(unknown))}; allowed: {', '.join(allowed)}")
    missing = [k for k in required if k not in obj]
    if missing:
        fail(where, f"missing field(s) {', '.join(missing)}")
    return obj


def check_str(value: object, where: str, *, empty: bool = False, line: bool = False) -> str:
    if not isinstance(value, str):
        fail(where, f"expected a string, got {type_name(value)} {_shown(value)}")
    if not empty and not value.strip():
        fail(where, "must not be empty")
    if line and ("\n" in value or "\r" in value):
        fail(where, "must be a single line")
    if "\0" in value:
        fail(where, "must not contain NUL characters")
    return value


def check_match(value: object, where: str, pattern: re.Pattern, what: str) -> str:
    check_str(value, where, line=True)
    if not pattern.match(value):
        fail(where, f"{_shown(value)} is not {what}")
    return value


def check_time(value: object, where: str) -> str:
    check_match(value, where, TIMESTAMP_RE, 'a UTC time like "2026-10-14T06:37:38Z"')
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        fail(where, f"{_shown(value)} is not a valid date and time")
    return value


def check_one_of(value: object, where: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        fail(where, f"expected one of {', '.join(choices)}, got {type_name(value)} {_shown(value)}")
    return value


def check_bool(value: object, where: str) -> bool:
    if type(value) is not bool:
        fail(where, f"expected true or false, got {type_name(value)} {_shown(value)}")
    return value


def check_int(value: object, where: str, minimum: int | None = None) -> int:
    if type(value) is not int:
        fail(where, f"expected an integer, got {type_name(value)} {_shown(value)}")
    if minimum is not None and value < minimum:
        fail(where, f"must be at least {minimum}, got {value}")
    return value


def check_number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(where, f"expected a number, got {type_name(value)} {_shown(value)}")
    if not math.isfinite(value):
        fail(where, f"must be finite, got {value}")
    return value


def check_list(value: object, where: str, *, nonempty: bool = False) -> list:
    if not isinstance(value, list):
        fail(where, f"expected a list, got {type_name(value)} {_shown(value)}")
    if nonempty and not value:
        fail(where, "must not be empty")
    return value


def check_json(value: object, where: str) -> object:
    """A JSON value: null, boolean, integer, finite number, string, list or string-keyed mapping."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return check_number(value, where)
    if isinstance(value, list):
        for i, item in enumerate(value):
            check_json(item, f"{where}[{i}]")
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                fail(where, f"keys must be strings, got {type_name(key)} {_shown(key)}")
            check_json(item, f"{where}.{key}")
        return value
    fail(where, f"not a JSON value: {type_name(value)}")


def check_vector(value: object, where: str, length: int) -> list:
    check_list(value, where)
    if len(value) != length:
        fail(where, f"expected {length} numbers, got {len(value)}")
    for i, x in enumerate(value):
        check_number(x, f"{where}[{i}]")
    return value


def check_project_path(value: object, where: str) -> str:
    """A relative POSIX path inside the project, already normalised (no '.', '..' or '//')."""
    check_str(value, where, line=True)
    p = PurePosixPath(value)
    if p.is_absolute() or "\\" in value:
        fail(where, f"must be a path relative to the project root, got {_shown(value)}")
    if any(part in ("", ".", "..") for part in value.split("/")):
        fail(where, f"must be a normalised path inside the project, got {_shown(value)}")
    return value


# ---------------------------------------------------------------------------------------------
# Structures

def check_by(value: object, where: str) -> dict:
    check_fields(value, where, ("role",), ("session", "name"))
    check_one_of(value["role"], f"{where}.role", ROLES)
    for key in ("session", "name"):
        if key in value:
            check_str(value[key], f"{where}.{key}", line=True)
    return value


def check_target(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        fail(where, f"expected a target mapping, got {type_name(value)}")
    kind = check_one_of(value.get("kind"), f"{where}.kind", TARGET_KINDS)
    if kind == "part":
        check_fields(value, where, ("kind", "label"), ("assembly",))
        check_str(value["label"], f"{where}.label", line=True)
        if "assembly" in value:
            check_str(value["assembly"], f"{where}.assembly", line=True)
    elif kind == "param":
        check_fields(value, where, ("kind", "key"))
        check_match(value["key"], f"{where}.key", PARAM_KEY_RE, 'a parameter key like "robot.params:BASE.wheel_width"')
    elif kind == "fact":
        check_fields(value, where, ("kind", "key"))
        check_str(value["key"], f"{where}.key", line=True)
    elif kind == "file":
        check_fields(value, where, ("kind", "path"), ("line",))
        check_project_path(value["path"], f"{where}.path")
        if "line" in value:
            check_int(value["line"], f"{where}.line", minimum=1)
    else:  # view
        check_fields(value, where, ("kind", "id"))
        check_match(value["id"], f"{where}.id", VIEW_ID_RE, "a view id like V-0001")
    return value


def check_targets(value: object, where: str) -> list:
    check_list(value, where)
    for i, target in enumerate(value):
        check_target(target, f"{where}[{i}]")
    return value


def check_option(value: object, where: str) -> dict:
    check_fields(value, where, ("id", "label"), ("effect",))
    check_match(value["id"], f"{where}.id", LOCAL_ID_RE, "an option id (letters, digits, _ . -)")
    check_str(value["label"], f"{where}.label", line=True)
    if "effect" in value:
        effect = value["effect"]
        if isinstance(effect, str):
            check_str(effect, f"{where}.effect")
        elif isinstance(effect, dict):
            check_approves(effect, f"{where}.effect")
        else:
            fail(f"{where}.effect", f"expected text or a mapping of approved values, got {type_name(effect)}")
    return value


def check_question(value: object, where: str) -> dict:
    check_fields(value, where, ("id", "text", "options"), ("recommended", "free_text"))
    check_match(value["id"], f"{where}.id", LOCAL_ID_RE, "a question id (letters, digits, _ . -)")
    check_str(value["text"], f"{where}.text")
    options = check_list(value["options"], f"{where}.options")
    ids = []
    for i, option in enumerate(options):
        check_option(option, f"{where}.options[{i}]")
        if option["id"] in ids:
            fail(f"{where}.options[{i}].id", f"duplicate option id {option['id']!r}")
        ids.append(option["id"])
    free_text = check_bool(value["free_text"], f"{where}.free_text") if "free_text" in value else False
    if not options and not free_text:
        fail(where, "needs at least one option or free_text: true")
    if "recommended" in value and value["recommended"] not in ids:
        fail(f"{where}.recommended", f"{_shown(value['recommended'])} is not one of the option ids {ids}")
    return value


def check_approves(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        fail(where, f"expected a mapping of key: value, got {type_name(value)}")
    if not value:
        fail(where, "must not be empty")
    for key, approved in value.items():
        check_str(key, f"{where} key", line=True)
        check_json(approved, f"{where}.{key}")
    return value


def check_answers(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        fail(where, f"expected a mapping of question id: answer, got {type_name(value)}")
    for qid, answer in value.items():
        check_match(qid, f"{where} key", LOCAL_ID_RE, "a question id")
        check_fields(answer, f"{where}.{qid}", (), ("choice", "text"))
        if not answer:
            fail(f"{where}.{qid}", "needs a choice or a text")
        if "choice" in answer:
            check_match(answer["choice"], f"{where}.{qid}.choice", LOCAL_ID_RE, "an option id")
        if "text" in answer:
            check_str(answer["text"], f"{where}.{qid}.text")
    return value


def check_asset_path(value: object, where: str, item_id: str | None) -> str:
    check_project_path(value, where)
    parts = value.split("/")
    if len(parts) < 4 or parts[0] != "review" or parts[1] != "assets" or not ITEM_ID_RE.match(parts[2]):
        fail(where, f"must be a file under review/assets/<item id>/, got {_shown(value)}")
    if item_id is not None and parts[2] != item_id:
        fail(where, f"must be under review/assets/{item_id}/, got {_shown(value)}")
    return value


def check_event(event: object, where: str, item_id: str | None = None) -> dict:
    """Validate one event's own fields (not its consistency with the rest of the item)."""
    if not isinstance(event, dict):
        fail(where, f"expected an event mapping, got {type_name(event)}")
    etype = check_one_of(event.get("event"), f"{where}.event", EVENT_TYPES)
    required, optional = _EVENT_FIELDS[etype]
    check_fields(event, where, required, optional)
    check_time(event["at"], f"{where}.at")
    if "by" in event:
        check_by(event["by"], f"{where}.by")
    if "build" in event and event["build"] is not None:
        check_match(event["build"], f"{where}.build", BUILD_ID_RE, "a build id like 5367af0 or 5367af0+1c2d3e4f")

    if etype == "open":
        kind = check_one_of(event["kind"], f"{where}.kind", KINDS)
        check_str(event["title"], f"{where}.title", line=True)
        if "targets" in event:
            check_targets(event["targets"], f"{where}.targets")
        if "body" in event:
            check_str(event["body"], f"{where}.body", empty=True)
        if kind != "decision":
            for key in _DECISION_ONLY:
                if key in event:
                    fail(f"{where}.{key}", f"only a decision has {key}, this is a {kind}")
        if kind != "measurement":
            for key in _MEASUREMENT_ONLY:
                if key in event:
                    fail(f"{where}.{key}", f"only a measurement has {key}, this is a {kind}")
        if kind == "decision":
            if "questions" not in event:
                fail(where, "a decision needs questions")
            questions = check_list(event["questions"], f"{where}.questions", nonempty=True)
            seen = []
            for i, question in enumerate(questions):
                check_question(question, f"{where}.questions[{i}]")
                if question["id"] in seen:
                    fail(f"{where}.questions[{i}].id", f"duplicate question id {question['id']!r}")
                seen.append(question["id"])
            for i, text in enumerate(check_list(event.get("corrections", []), f"{where}.corrections")):
                check_str(text, f"{where}.corrections[{i}]")
            check_targets(event.get("evidence", []), f"{where}.evidence")
        if kind == "measurement":
            for key in _MEASUREMENT_ONLY:
                if key not in event:
                    fail(where, f"a measurement needs {key}")
            check_str(event["quantity"], f"{where}.quantity", line=True)
            check_str(event["unit"], f"{where}.unit", line=True)
    elif etype == "reply":
        check_str(event["body"], f"{where}.body")
        if "status" in event:
            check_one_of(event["status"], f"{where}.status", STATUSES)
    elif etype == "answer":
        if event["by"]["role"] != "owner":
            fail(f"{where}.by.role", f"only the owner answers, got {event['by']['role']!r}")
        if "answers" in event:
            check_answers(event["answers"], f"{where}.answers")
        if "approves" in event:
            check_approves(event["approves"], f"{where}.approves")
        if not event.get("answers") and not event.get("approves"):
            fail(where, "an answer needs answers or approves (use a reply for text only)")
        if "body" in event:
            check_str(event["body"], f"{where}.body", empty=True)
    elif etype == "status":
        check_one_of(event["to"], f"{where}.to", STATUSES)
    elif etype == "delivered":
        if "to" in event:
            check_str(event["to"], f"{where}.to", line=True)
        if "via" in event:
            check_str(event["via"], f"{where}.via", line=True)
    elif etype == "attach":
        check_asset_path(event["path"], f"{where}.path", item_id)
    else:  # link
        to = check_str(event["to"], f"{where}.to", line=True)
        if not (ITEM_ID_RE.match(to) or VIEW_ID_RE.match(to)):
            fail(f"{where}.to", f"expected an item id R-... or a view id V-..., got {_shown(to)}")
        if item_id is not None and to == item_id:
            fail(f"{where}.to", "an item cannot link to itself")
    return event


# ---------------------------------------------------------------------------------------------
# Saved views (review/views/V-0001.yaml, one document)

VIEW_REQUIRED = ("at", "by", "build", "assembly", "camera")
VIEW_OPTIONAL = ("hidden", "section", "highlight", "note")


def check_camera(value: object, where: str) -> dict:
    # The stock viewer's camera state (spike S1): position, target, quaternion, zoom.
    check_fields(value, where, ("position", "target"), ("quaternion", "zoom"))
    check_vector(value["position"], f"{where}.position", 3)
    check_vector(value["target"], f"{where}.target", 3)
    if "quaternion" in value:
        check_vector(value["quaternion"], f"{where}.quaternion", 4)
    if "zoom" in value and check_number(value["zoom"], f"{where}.zoom") <= 0:
        fail(f"{where}.zoom", "must be positive")
    return value


def check_section(value: object, where: str) -> dict:
    # Up to three clipping planes, as the stock viewer has; intersection as its clip option.
    check_fields(value, where, ("planes",), ("intersection",))
    planes = check_list(value["planes"], f"{where}.planes", nonempty=True)
    if len(planes) > 3:
        fail(f"{where}.planes", f"at most 3 planes, got {len(planes)}")
    for i, plane in enumerate(planes):
        check_fields(plane, f"{where}.planes[{i}]", ("normal", "offset"))
        normal = check_vector(plane["normal"], f"{where}.planes[{i}].normal", 3)
        if not any(normal):
            fail(f"{where}.planes[{i}].normal", "must not be zero")
        check_number(plane["offset"], f"{where}.planes[{i}].offset")
    if "intersection" in value:
        check_bool(value["intersection"], f"{where}.intersection")
    return value


def check_view(doc: object, where: str) -> dict:
    check_fields(doc, where, VIEW_REQUIRED, VIEW_OPTIONAL)
    check_time(doc["at"], f"{where}.at")
    check_by(doc["by"], f"{where}.by")
    if doc["build"] is not None:
        check_match(doc["build"], f"{where}.build", BUILD_ID_RE, "a build id")
    check_str(doc["assembly"], f"{where}.assembly", line=True)
    check_camera(doc["camera"], f"{where}.camera")
    for i, label in enumerate(check_list(doc.get("hidden", []), f"{where}.hidden")):
        check_str(label, f"{where}.hidden[{i}]", line=True)
    if "section" in doc:
        check_section(doc["section"], f"{where}.section")
    check_targets(doc.get("highlight", []), f"{where}.highlight")
    if "note" in doc:
        check_str(doc["note"], f"{where}.note", empty=True)
    return doc
