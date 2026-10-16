"""The operation registry and what is generated from it: JSON schemas, strict argument binding,
HTTP endpoint descriptors, the CLI's argument handling, and forwarding to a running hub."""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal, Optional

import pytest

from workbench.cli import OwnerOnly, execute, main
from workbench.errors import HubNotRunning, InvalidInput, NotFound
from workbench.ops import Registry, default_registry
from workbench.project import Project, write_hub_info


def _sample_registry(needs_hub: bool = False) -> Registry:
    registry = Registry()

    @registry.op("sample", summary="Every supported parameter type", needs_hub=needs_hub, args={"text": "some text"})
    def sample(
        project: Project,
        text: str,
        count: int,
        ratio: float,
        flag: bool,
        tags: list[str],
        rows: list[dict],
        data: dict,
        mode: Literal["a", "b"] = "a",
        note: Optional[str] = None,
        verbose: bool = False,
    ) -> dict:
        return {"text": text, "count": count, "ratio": ratio, "flag": flag, "tags": tags, "rows": rows,
                "data": data, "mode": mode, "note": note, "verbose": verbose, "root": str(project.root)}

    return registry


VALID = {"text": "t", "count": 3, "ratio": 2, "flag": False, "tags": ["x"], "rows": [{"a": 1}], "data": {"k": "v"}}


def test_schema_of_every_supported_type():
    # The HTTP bodies and MCP tool inputs of the next work packages are validated against this.
    schema = _sample_registry().get("sample").schema()
    assert schema == {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "some text"},
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "flag": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "rows": {"type": "array", "items": {"type": "object"}},
            "data": {"type": "object"},
            "mode": {"type": "string", "enum": ["a", "b"], "default": "a"},
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "verbose": {"type": "boolean", "default": False},
        },
        "required": ["text", "count", "ratio", "flag", "tags", "rows", "data"],
        "additionalProperties": False,
    }


def test_binding_fills_defaults_injects_the_project_and_widens_int_to_float(project):
    result = _sample_registry().get("sample").call(dict(VALID), project=project)
    assert result["ratio"] == 2.0 and isinstance(result["ratio"], float)
    assert (result["mode"], result["note"], result["verbose"]) == ("a", None, False)
    assert result["root"] == str(project.root)


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"flag": "true"}, "argument flag: expected true or false, got string"),
        ({"flag": 1}, "argument flag: expected true or false, got integer"),
        ({"count": True}, "argument count: expected an integer, got boolean"),
        ({"count": "3"}, "argument count: expected an integer, got string"),
        ({"ratio": "1.5"}, "argument ratio: expected a number, got string"),
        ({"tags": ["a", 1]}, "argument tags: expected a list of strings"),
        ({"rows": [1]}, "argument rows: expected a list of JSON objects"),
        ({"mode": "c"}, "argument mode: expected one of a, b"),
        ({"text": None}, "argument text: must not be null"),
        ({"extra": 1}, "unknown argument(s) extra"),
        ({"text": ...}, "missing argument(s) text"),
    ],
)
def test_binding_is_strict(project, change, needle):
    arguments = {**VALID, **change}
    arguments = {k: v for k, v in arguments.items() if v is not ...}
    with pytest.raises(InvalidInput) as excinfo:
        _sample_registry().get("sample").call(arguments, project=project)
    assert needle in str(excinfo.value)


@pytest.mark.parametrize(
    ("signature", "needle"),
    [
        ("def f(x: set) -> dict: ...", "unsupported type"),
        ("def f(x: 'str | int') -> dict: ...", "only Optional"),
        ("def f(json: str) -> dict: ...", "reserved"),
        ("def f(x) -> dict: ...", "needs a type annotation"),
    ],
)
def test_registration_rejects_what_the_cli_and_schema_cannot_express(signature, needle):
    namespace: dict = {}
    exec(signature, namespace)
    with pytest.raises(TypeError, match=needle):
        Registry().op("f", summary="s")(namespace["f"])


def test_http_endpoints_call_the_operations_with_the_project(project):
    endpoints = {e.name: e for e in default_registry().http_endpoints(project)}
    assert len(endpoints) >= 15
    for name, e in endpoints.items():
        assert (e.method, e.path) == ("POST", f"/api/op/{name}")
        assert e.schema["additionalProperties"] is False
        assert "project" not in e.schema["properties"]

    state = endpoints["comment"].call({"title": "Edge too close", "targets": [{"kind": "part", "label": "plate"}]})
    assert (state["id"], state["opened"]["by"], state["status"]) == ("R-0001", {"role": "owner"}, "waiting_agent")
    inbox = endpoints["inbox"].call({"ack": False})
    assert [item["id"] for item in inbox["items"]] == ["R-0001"]

    with pytest.raises(InvalidInput) as excinfo:
        endpoints["inbox"].call({"ack": "false"})  # a string boolean from a sloppy client
    assert excinfo.value.http_status == 400
    with pytest.raises(NotFound) as excinfo:
        endpoints["item"].call({"id": "R-0042"})
    assert excinfo.value.http_status == 404


def test_cli_merges_a_json_arguments_file_under_the_command_line(project, monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(project.root)
    args_file = tmp_path / "ask.json"
    args_file.write_text(json.dumps({
        "title": "title from the file",
        "body": "body from the file",
        "questions": [{"id": "q1", "text": "Which?", "options": [{"id": "a", "label": "A"}]}],
    }))
    assert main(["ask", "title from the command line", "--file", str(args_file), "--json"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert (state["title"], state["body"]) == ("title from the command line", "body from the file")
    assert state["open_questions"] == ["q1"]


def test_cli_errors_go_to_stderr_with_a_nonzero_exit(project, monkeypatch, capsys):
    monkeypatch.chdir(project.root)
    monkeypatch.delenv("CLAUDECODE", raising=False)  # this is about NotFound/usage errors, not owner_only
    assert main(["answer", "R-0007", "--answers", '{"q1": {"choice": "a"}}']) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("wbench answer: no item R-0007")
    with pytest.raises(SystemExit) as excinfo:  # malformed JSON is a usage error
        main(["answer", "R-0007", "--answers", "{q1: a}"])
    assert excinfo.value.code == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_cli_refuses_owner_only_ops_under_claude_code(project, monkeypatch, capsys):
    # answer and comment have their author role fixed to the owner (review_ops.py); Claude Code
    # sets CLAUDECODE for the commands its agent runs, so the CLI must refuse them under it.
    monkeypatch.chdir(project.root)
    questions = [{"id": "q1", "text": "Which?", "options": [{"id": "a", "label": "A"}]}]
    main(["ask", "Front limit", "--questions", json.dumps(questions)])  # ask is not owner_only
    capsys.readouterr()

    monkeypatch.setenv("CLAUDECODE", "1")
    assert main(["answer", "R-0001", "--answers", '{"q1": {"choice": "a"}}']) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "wbench answer: owner actions come from the workbench page, not the agent's shell"
    # refused before it ran: the item is exactly as `ask` left it
    assert default_registry().get("item").call({"id": "R-0001"}, project=project)["status"] == "waiting_owner"

    with pytest.raises(OwnerOnly, match="workbench page"):
        execute(default_registry().get("comment"), {"title": "x"}, str(project.root))


def test_writes_as_owner_is_the_one_rule_behind_the_role_owner_gate():
    # Op.writes_as_owner is what cli.execute, the MCP shim and the hub's HTTP routes all call
    # (docs/plan.md design principle 2 extended to authority): a single rule, not one check per op.
    registry = default_registry()
    for name in ("answer", "comment"):  # owner_only: always, whatever the arguments
        o = registry.get(name)
        assert o.writes_as_owner({}) is True
        assert o.writes_as_owner({"role": "agent"}) is True  # owner_only ignores a role argument
        assert o.writes_as_owner("not a dict") is True  # never crashes on malformed input either

    for name in ("reply", "set-status", "attach", "link", "view-create"):  # take --role, default agent
        o = registry.get(name)
        assert o.writes_as_owner({"role": "owner"}) is True
        assert o.writes_as_owner({"role": "agent"}) is False
        assert o.writes_as_owner({"role": "reviewer"}) is False
        assert o.writes_as_owner({}) is False  # omitted: the op's own default (agent) applies later
        assert o.writes_as_owner("not a dict") is False

    # ask and note take a role too, but "owner" is not even one of its choices (schema.py refuses
    # it on bind()); writes_as_owner only looks at the string, so it is unaffected either way.
    assert registry.get("ask").writes_as_owner({"role": "reviewer"}) is False
    assert registry.get("status").writes_as_owner({}) is False  # a role-less op: never an owner write


ROLE_TAKING_OPS = ("reply", "set-status", "attach", "link", "view-create")


def _minimal_role_op_args(name: str, item_id: str, other_id: str, tmp_path) -> dict:
    if name == "reply":
        return {"id": item_id, "body": "hello"}
    if name == "set-status":
        return {"id": item_id, "to": "waiting_agent"}
    if name == "attach":
        source = tmp_path / "note.txt"
        source.write_text("x")
        return {"id": item_id, "source": str(source)}
    if name == "link":
        return {"id": item_id, "to": other_id}
    if name == "view-create":
        return {"assembly": "demo", "camera": {"position": [0.0, 0.0, 0.0], "target": [0.0, 0.0, 0.0]}}
    raise AssertionError(name)


@pytest.mark.parametrize("name", ROLE_TAKING_OPS)
def test_cli_refuses_role_owner_for_every_role_taking_op_under_claude_code(project, monkeypatch, tmp_path, name):
    # docs/plan.md's own examples of the gap: `wbench reply R-0001 "..." --role owner` and
    # `wbench set-status R-0001 done --role owner` succeeded in the agent's shell (test_cli.py-
    # style main() coverage of exactly those two is below). attach, link and view-create take the
    # same --role and must be refused the same way, under the one rule, with no op-by-op special
    # case: this test parametrizes over all five rather than hand-copying the check per op.
    monkeypatch.chdir(project.root)
    questions = [{"id": "q1", "text": "Which?", "options": [{"id": "a", "label": "A"}]}]
    item_id = default_registry().get("ask").call({"title": "t1", "questions": questions}, project=project)["id"]
    other_id = default_registry().get("ask").call({"title": "t2", "questions": questions}, project=project)["id"]
    args = _minimal_role_op_args(name, item_id, other_id, tmp_path)

    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(OwnerOnly, match="workbench page"):
        execute(default_registry().get(name), {**args, "role": "owner"}, str(project.root))

    # the same operation, same CLAUDECODE, still runs with role agent (the CLI's usual caller)
    result = execute(default_registry().get(name), {**args, "role": "agent"}, str(project.root))
    assert isinstance(result, dict) and result


def test_cli_refuses_reply_and_set_status_with_role_owner_under_claude_code(project, monkeypatch, capsys):
    monkeypatch.chdir(project.root)
    questions = [{"id": "q1", "text": "Which?", "options": [{"id": "a", "label": "A"}]}]
    main(["ask", "Front limit", "--questions", json.dumps(questions)])
    capsys.readouterr()

    monkeypatch.setenv("CLAUDECODE", "1")
    assert main(["reply", "R-0001", "Applied 75 mm.", "--role", "owner"]) == 1
    assert "owner actions come from the workbench page" in capsys.readouterr().err
    assert main(["set-status", "R-0001", "done", "--role", "owner"]) == 1
    assert "owner actions come from the workbench page" in capsys.readouterr().err
    # refused before either ran: the item is exactly as `ask` left it
    assert default_registry().get("item").call({"id": "R-0001"}, project=project)["status"] == "waiting_owner"

    # the same commands, same CLAUDECODE, succeed with role agent (the CLI's own usual caller)
    assert main(["reply", "R-0001", "Applied 75 mm.", "--role", "agent"]) == 0
    capsys.readouterr()
    assert main(["set-status", "R-0001", "done", "--role", "agent"]) == 0


def test_owner_only_ops_run_via_the_cli_without_claude_code_and_via_the_hub_regardless(project, monkeypatch):
    questions = [{"id": "q1", "text": "Which?", "options": [{"id": "a", "label": "A"}]}]
    default_registry().get("ask").call({"title": "Front limit", "questions": questions}, project=project)

    # Without CLAUDECODE, the CLI path (execute()) still runs an owner_only op normally.
    monkeypatch.delenv("CLAUDECODE", raising=False)
    state = execute(default_registry().get("comment"), {"title": "Via the CLI, no CLAUDECODE"}, str(project.root))
    assert state["opened"]["by"] == {"role": "owner"}

    # Not only owner_only ops: a plain role="owner" argument is let through the same way, since a
    # terminal with CLAUDECODE unset is the owner's own, not an agent's (docs/plan.md "today").
    state = execute(default_registry().get("reply"), {"id": "R-0001", "body": "fine", "role": "owner"}, str(project.root))
    assert state["last_activity"]["role"] == "owner"

    # The hub's HTTP handler calls Op.call() directly, never execute(): a CLAUDECODE set on the
    # hub's own process (it may itself have been started from inside Claude Code) must not matter.
    monkeypatch.setenv("CLAUDECODE", "1")
    endpoint = next(e for e in default_registry().http_endpoints(project) if e.name == "answer")
    state = endpoint.call({"id": "R-0001", "answers": {"q1": {"choice": "a"}}})
    assert state["answers"]["q1"]["choice"] == "a"


class _FakeHub:
    """A stand-in for `wbench serve`: records each request, answers with a canned status and body."""

    def __init__(self, status: int = 200, body: dict | None = None):
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                fake.requests.append({
                    "path": self.path,
                    "token": self.headers.get("X-Workbench-Token"),
                    "body": json.loads(self.rfile.read(length)),
                })
                payload = json.dumps(body if body is not None else {"echo": fake.requests[-1]["body"]}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_needs_hub_operations_are_forwarded_with_the_token(project):
    o = _sample_registry(needs_hub=True).get("sample")
    hub = _FakeHub()
    try:
        write_hub_info(project, port=hub.port, token="tok-123")  # this process stands in for the hub's pid
        result = execute(o, dict(VALID), str(project.root))
        assert hub.requests == [{"path": "/api/op/sample", "token": "tok-123", "body": VALID}]
        assert result == {"echo": VALID}

        with pytest.raises(InvalidInput):  # bad arguments never reach the hub
            execute(o, {**VALID, "count": "3"}, str(project.root))
        assert len(hub.requests) == 1
    finally:
        hub.close()

    hub = _FakeHub(status=404, body={"error": "no such scene"})
    try:
        write_hub_info(project, port=hub.port, token="tok-123")
        with pytest.raises(NotFound, match="no such scene"):
            execute(o, dict(VALID), str(project.root))
    finally:
        hub.close()


def test_session_defaults_from_wb_session_for_a_forwarded_operation_too(project, monkeypatch):
    # cli.execute() fills in the WB_SESSION default before deciding local vs. forwarded, so a
    # needs_hub operation that takes `session` gets it exactly like a local one (test_cli.py
    # covers the local, end-to-end case with the real `ask` operation).
    registry = Registry()

    @registry.op("needs-session", summary="s", needs_hub=True)
    def needs_session(project: Project, session: Optional[str] = None) -> dict:
        return {"session": session}

    o = registry.get("needs-session")
    hub = _FakeHub()
    try:
        write_hub_info(project, port=hub.port, token="tok-123")
        monkeypatch.setenv("WB_SESSION", "sess-env")
        execute(o, {}, str(project.root))
        assert hub.requests[-1]["body"] == {"session": "sess-env"}

        execute(o, {"session": "explicit"}, str(project.root))
        assert hub.requests[-1]["body"] == {"session": "explicit"}

        monkeypatch.delenv("WB_SESSION")
        execute(o, {}, str(project.root))
        assert hub.requests[-1]["body"] == {}
    finally:
        hub.close()


def test_without_a_running_hub_the_cli_says_how_to_start_it(project):
    o = _sample_registry(needs_hub=True).get("sample")
    with pytest.raises(HubNotRunning) as excinfo:  # no hub.json at all
        execute(o, dict(VALID), str(project.root))
    assert str(excinfo.value).startswith("hub not running: start it with `wbench serve`")

    child = subprocess.Popen(["true"])
    child.wait()  # reaped, so its pid names no process (a zombie would still answer kill(pid, 0))
    write_hub_info(project, port=1, token="tok", pid=child.pid)
    with pytest.raises(HubNotRunning) as excinfo:  # hub.json left behind by a hub that died
        execute(o, dict(VALID), str(project.root))
    assert str(excinfo.value).startswith("hub not running: start it with `wbench serve`")
