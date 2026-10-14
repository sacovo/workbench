"""The operation registry and what is generated from it: JSON schemas, strict argument binding,
HTTP endpoint descriptors, the CLI's argument handling, and forwarding to a running hub."""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal, Optional

import pytest

from hub_helpers import project  # noqa: F401  (fixture)
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
    assert captured.err.startswith("wb answer: no item R-0007")
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
    assert captured.err.strip() == "wb answer: owner actions come from the workbench page, not the agent's shell"
    # refused before it ran: the item is exactly as `ask` left it
    assert default_registry().get("item").call({"id": "R-0001"}, project=project)["status"] == "waiting_owner"

    with pytest.raises(OwnerOnly, match="workbench page"):
        execute(default_registry().get("comment"), {"title": "x"}, str(project.root))


def test_owner_only_ops_run_via_the_cli_without_claude_code_and_via_the_hub_regardless(project, monkeypatch):
    questions = [{"id": "q1", "text": "Which?", "options": [{"id": "a", "label": "A"}]}]
    default_registry().get("ask").call({"title": "Front limit", "questions": questions}, project=project)

    # Without CLAUDECODE, the CLI path (execute()) still runs an owner_only op normally.
    monkeypatch.delenv("CLAUDECODE", raising=False)
    state = execute(default_registry().get("comment"), {"title": "Via the CLI, no CLAUDECODE"}, str(project.root))
    assert state["opened"]["by"] == {"role": "owner"}

    # The hub's HTTP handler calls Op.call() directly, never execute(): a CLAUDECODE set on the
    # hub's own process (it may itself have been started from inside Claude Code) must not matter.
    monkeypatch.setenv("CLAUDECODE", "1")
    endpoint = next(e for e in default_registry().http_endpoints(project) if e.name == "answer")
    state = endpoint.call({"id": "R-0001", "answers": {"q1": {"choice": "a"}}})
    assert state["answers"]["q1"]["choice"] == "a"


class _FakeHub:
    """A stand-in for `wb serve`: records each request, answers with a canned status and body."""

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


def test_without_a_running_hub_the_cli_says_how_to_start_it(project):
    o = _sample_registry(needs_hub=True).get("sample")
    with pytest.raises(HubNotRunning) as excinfo:  # no hub.json at all
        execute(o, dict(VALID), str(project.root))
    assert str(excinfo.value).startswith("hub not running: start it with `wb serve`")

    child = subprocess.Popen(["true"])
    child.wait()  # reaped, so its pid names no process (a zombie would still answer kill(pid, 0))
    write_hub_info(project, port=1, token="tok", pid=child.pid)
    with pytest.raises(HubNotRunning) as excinfo:  # hub.json left behind by a hub that died
        execute(o, dict(VALID), str(project.root))
    assert str(excinfo.value).startswith("hub not running: start it with `wb serve`")
