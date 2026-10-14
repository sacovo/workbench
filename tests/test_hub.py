"""The hub over its real sockets, with the fake kernel (fake_kernel.py): the guard, the operation
endpoints, blob serving, scene replay, kernel generations (crash, rebuild swap, failed rebuild),
review pushes, picks, comments with frozen evidence, and the app window's launch."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import struct
import subprocess
import sys
import time
import zlib

import pytest
from websockets.exceptions import InvalidStatus

from hub_helpers import WB, project  # noqa: F401  (project: fixture)
from hub_running import RunningHub, fake_control, kernel_processes, wait_until
from workbench.errors import HubNotRunning
from workbench.hub.launch import open_window
from workbench.ops import default_registry
from workbench.ops.doctor import STALE_GL_VARS
from workbench.project import HubInfo, pid_alive, write_hub_info

QUESTIONS = [
    {"id": "q1", "text": "Which front limit?", "options": [{"id": "a", "label": "35 mm"}, {"id": "b", "label": "75 mm"}], "recommended": "a"},
]
CAMERA = {"position": [100.0, -100.0, 80.0], "target": [0.0, 0.0, 0.0], "quaternion": [0.3, 0.1, 0.2, 0.9], "zoom": 1.5}


def tiny_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")


@pytest.fixture
def running(project, tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>page</title>")
    (static / "assets" / "app.js").write_text("console.log('page')")
    hub = RunningHub(project, static_dir=static)
    yield hub
    hub.stop()
    assert kernel_processes(project.root) == [], "kernels left running after the hub stopped"


def pick(page, scene: str, status: dict, **extra) -> None:
    page.send({"type": "status", "scene": scene, "status": status, "pick": {"camera": CAMERA, **extra}})


# -- the guard ---------------------------------------------------------------------------------

def test_the_api_needs_the_token_and_the_hubs_own_origin_and_host(running):
    own = running.url

    def status(**kw) -> int:
        return running.request("GET", "/api/state", **kw)[0]

    assert status() == 200
    assert status(headers={"Origin": own}) == 200
    assert status(token=None) == 401
    assert status(token="not-the-token") == 401
    assert status(headers={"Origin": "http://evil.example"}) == 403  # the right token, from another page
    assert status(headers={"Origin": "null"}) == 403
    assert status(headers={"Host": "evil.example"}) == 403  # DNS rebinding
    code, body = running.request("POST", "/api/op/status", {}, headers={"Origin": "http://localhost:8000"})
    assert code == 403 and "refused" in body["error"]

    # The page and its assets carry no secret: no token needed, the guard's Origin rule still holds.
    assert running.request("GET", "/", token=None) == (200, b"<!doctype html><title>page</title>")
    assert running.request("GET", "/assets/app.js", token=None)[0] == 200
    assert running.request("GET", "/", token=None, headers={"Origin": "http://evil.example"})[0] == 403

    # The page's WebSocket: token in the query, Origin checked like everything else.
    page = running.page(origin=own)
    assert page.wait("wb_hello")["settings"]["control"] == "trackball"
    page.close()
    for kwargs in ({"token": "not-the-token"}, {"origin": "http://evil.example"}):
        with pytest.raises(InvalidStatus) as refused:
            running.page(**kwargs)
        assert refused.value.response.status_code == 403


# -- the page-credential gate -------------------------------------------------------------------
# answer and comment are owner_only; reply and set-status are not, but role="owner" makes them
# write as the owner too (Op.writes_as_owner) - the exact gap this gate closes (docs/plan.md,
# the authority-confusion risk): only the page `wbench open`/`serve --open` opened holds the
# credential, so the hub.json token alone (what an agent's shell or an HTTP client reading that
# file can present) is refused even though the Guard accepts it for everything else.

OWNER_WRITE_CASES = [
    ("answer", {"id": "R-0001", "answers": {"q1": {"choice": "a"}}}),
    ("comment", {"title": "Edge too close"}),
    ("reply", {"id": "R-0001", "body": "noted", "role": "owner"}),
    ("set-status", {"id": "R-0001", "to": "done", "role": "owner"}),
]


@pytest.mark.parametrize(("name", "arguments"), OWNER_WRITE_CASES)
def test_owner_writes_over_http_need_the_page_credential_even_with_the_right_token(running, name, arguments):
    running.op("ask", title="Front limit", questions=QUESTIONS)  # R-0001, for reply/set-status/answer

    for bad in (None, "wrong"):
        status, body = running.request("POST", f"/api/op/{name}", arguments, page_token=bad)
        assert status == 403 and "page credential" in body["error"], (bad, status, body)

    status, body = running.request("POST", f"/api/op/{name}", arguments)  # DEFAULT: running.page_credential
    assert status == 200, body


def test_agent_writes_over_http_need_no_page_credential(running):
    running.op("ask", title="Front limit", questions=QUESTIONS)  # R-0001
    cases = [
        ("ask", {"title": "Another one", "questions": QUESTIONS}),
        ("note", {"kind": "finding", "title": "Edge too thin"}),
        ("reply", {"id": "R-0001", "body": "still working on it"}),  # role defaults to agent
        ("set-status", {"id": "R-0001", "to": "waiting_owner"}),
    ]
    for name, arguments in cases:
        status, body = running.request("POST", f"/api/op/{name}", arguments, page_token=None)
        assert status == 200, (name, body)


def test_page_comment_needs_the_page_credential(running):
    shown = running.op("show", assembly="demo")
    page = running.page()
    page.wait("wb_scene")
    page.show_scene(shown["scene"])
    pick(page, shown["scene"], {"lastPick": {"path": "/demo_assembly", "name": "carriage"}}, point=[0.5, 0.5, 1.0], normal=[0, 0, 1])
    page.wait("wb_pick")
    selection = running.op("selection")
    comment = {"seq": selection["seq"], "title": "Too close to the wheel", "body": "", "camera": CAMERA}

    for bad in (None, "wrong"):
        status, body = running.request("POST", "/api/page/comment", comment, page_token=bad)
        assert status == 403 and "page credential" in body["error"], (bad, status, body)

    status, body = running.request("POST", "/api/page/comment", comment)  # DEFAULT: running.page_credential
    assert status == 200, body


# -- operations and blobs ----------------------------------------------------------------------

def test_operations_answer_with_their_results_and_errors(running):
    asked = running.op("ask", title="Front limit", questions=QUESTIONS)
    assert asked["id"] == "R-0001" and (running.project.root / "review" / "R-0001.yaml").is_file()

    cases = [
        ("answer", {"id": "R-0001", "answers": {"q1": {"choice": "zz"}}}, 400, "'zz' is not an option of q1"),
        ("item", {"id": "R-0099"}, 404, "no item R-0099"),
        ("item", {"id": "R-0001", "bogus": 1}, 400, "unknown argument(s) bogus"),
        ("show", {"assembly": "nope"}, 400, "no assembly 'nope'"),
        ("serve", {}, 404, "no route"),  # starting a hub is not something a hub does; open is (below)
    ]
    for name, arguments, code, needle in cases:
        status, body = running.request("POST", f"/api/op/{name}", arguments)
        assert (status, needle in body["error"]) == (code, True), (name, status, body)
    status, body = running.request("POST", "/api/op/item", b"{not json")
    assert status == 400 and "not valid JSON" in body["error"]


def test_a_scene_shown_before_any_page_is_replayed_with_its_updates(running):
    first = running.op("show", assembly="demo")
    moved = running.op("show", assembly="demo", args={"height": 250.0})
    assert (first["incremental"], first["pages"], first["new_kernel"]) == (False, 0, False)
    assert (moved["incremental"], moved["moved"], moved["changed"], moved["blob"]) == (True, ["carriage"], [], None)

    page = running.page()
    scene, update = page.wait("wb_scene"), page.wait("wb_update")
    types = page.types()
    assert types.index("wb_scene") < types.index("wb_update")
    assert (scene["scene"], scene["blob"]["sha"]) == (first["scene"], first["blob"]["sha"])
    assert (update["previous"], update["scene"]) == (first["scene"], moved["scene"])
    assert update["moved"] == [{"path": "/demo_assembly/carriage", "loc": [[0.0, 0.0, 250.0], [0.0, 0.0, 0.0, 1.0]]}]

    # The blob the scene names is served byte for byte; other names are refused.
    status, payload = running.request("GET", f"/api/blob/{scene['blob']['sha']}")
    raw = (running.project.blobs_dir / f"{scene['blob']['sha']}.json").read_bytes()
    assert status == 200 and payload == json.loads(raw) and hashlib.sha256(raw).hexdigest() == scene["blob"]["sha"]
    assert running.request("GET", "/api/blob/" + "g" * 64)[0] == 400
    assert running.request("GET", "/api/blob/" + "0" * 64)[0] == 404

    # A connected page gets the next update live, a changed part with its blob.
    mark = page.mark()
    wider = running.op("show", assembly="demo", args={"height": 250.0, "width": 20.0})
    live = page.wait("wb_update", after=mark)
    assert (live["scene"], live["changed"], live["blob"]["kind"]) == (wider["scene"], [{"path": "/demo_assembly/column", "label": "column"}], "parts")
    assert wider["pages"] == 1


# -- kernel generations ------------------------------------------------------------------------

def test_a_kernel_crash_keeps_the_last_scene_and_the_next_show_starts_a_new_kernel(running):
    shown = running.op("show", assembly="demo")
    os.kill(running.state()["kernel"]["pid"], signal.SIGKILL)
    state = running.wait_state(lambda s: s["kernel"]["state"] == "crashed", 10, "the crash to be seen")
    assert "killed by signal 9" in state["kernel"]["error"]["message"]
    assert state["scene"]["scene"] == shown["scene"]

    page = running.page()
    assert page.wait("wb_hello")["kernel"]["state"] == "crashed"
    assert page.wait("wb_scene")["scene"] == shown["scene"]
    assert running.request("GET", f"/api/blob/{shown['blob']['sha']}")[0] == 200
    pick(page, shown["scene"], {"lastPick": {"path": "/demo_assembly", "name": "base"}})
    assert "not running" in page.wait("wb_notice")["message"]
    assert running.request("POST", "/api/op/selection", {})[0] == 404

    again = running.op("show", assembly="demo")
    assert (again["new_kernel"], again["generation"]) == (True, 2)
    state = running.state()["kernel"]
    assert (state["state"], state["error"], state["generation"]) == ("ready", None, 2)


def test_a_rebuild_builds_in_a_new_kernel_while_the_old_one_answers(running):
    first = running.op("show", assembly="demo")
    page = running.page()
    page.wait("wb_scene")
    page.show_scene(first["scene"])
    old_pid = running.state()["kernel"]["pid"]
    fake_control(running.project, build_delay=2.0)
    mark = page.mark()
    page.send({"type": "rebuild"})
    running.wait_state(lambda s: s["kernel"]["pending"] is not None, 5, "the new kernel")

    t0 = time.monotonic()
    pick(page, first["scene"], {"activeTool": "PropertiesMeasurement", "selectedShapeIDs": ["/demo_assembly/column/faces/faces_3", False]})
    backend = page.wait("wb_backend", after=mark, timeout=5)
    selection = page.wait("wb_pick", after=mark, timeout=5)["selection"]
    answered_in = time.monotonic() - t0
    assert answered_in < 1.5 and running.state()["kernel"]["state"] == "building"
    assert backend["reply"]["result"]["kernel_pid"] == old_pid
    assert (selection["label"], selection["kind"], selection["scene"]) == ("column", "face", first["scene"])

    rebuilt = page.wait("wb_scene", lambda m: m["scene"] != first["scene"], after=mark, timeout=10)
    state = running.wait_state(lambda s: s["kernel"]["state"] == "ready")["kernel"]
    assert (state["generation"], state["error"]) == (2, None) and state["pid"] != old_pid
    assert pid_alive(old_pid)  # the page still shows the old scene, and picks on it go to the old kernel
    mark = page.mark()
    pick(page, first["scene"], {"selectedShapeIDs": ["/demo_assembly/base/faces/faces_1", False]})
    assert page.wait("wb_backend", after=mark)["reply"]["result"]["kernel_pid"] == old_pid
    page.show_scene(rebuilt["scene"])
    wait_until(lambda: not pid_alive(old_pid), 10, "the old kernel to stop once the page shows the new scene")


def test_a_code_change_builds_in_a_new_kernel_and_marks_the_scene_stale_meanwhile(running):
    running.op("show", assembly="demo")
    same = running.op("show", assembly="demo", args={"height": 120.0})
    assert (same["new_kernel"], same["incremental"]) == (False, True)

    params = running.project.root / "src" / "demo" / "params.py"
    params.write_text(params.read_text() + "LIFT = {'stroke': 400}\n")
    assert running.wait_state(lambda s: s["stale"], 5, "the scene to be marked stale")["scene"]["stale"] is True
    changed = running.op("show", assembly="demo", args={"height": 120.0})
    assert (changed["new_kernel"], changed["incremental"], changed["generation"]) == (True, False, 2)
    assert running.state()["stale"] is False


@pytest.mark.parametrize(("behaviour", "kind"), [({"crash_on": "build"}, "crash"), ({"fail_on": "build"}, "build")])
def test_a_failed_rebuild_keeps_the_last_good_scene_and_the_old_kernel(running, behaviour, kind):
    first = running.op("show", assembly="demo")
    old_pid = running.state()["kernel"]["pid"]
    fake_control(running.project, **behaviour)
    status, body = running.request("POST", "/api/op/show", {"assembly": "demo", "rebuild": True})
    assert status in (500, 503) and body["error"].startswith("show demo {}: ")

    state = running.state()
    assert (state["kernel"]["state"], state["kernel"]["pid"], state["scene"]["scene"]) == ("ready", old_pid, first["scene"])
    assert (state["kernel"]["error"]["kind"], state["kernel"]["error"]["generation"]) == (kind, 2)
    page = running.page()
    page.wait("wb_scene")
    pick(page, first["scene"], {"lastPick": {"path": "/demo_assembly", "name": "base"}})
    assert page.wait("wb_pick")["selection"]["label"] == "base"  # still answered by the old kernel


# -- review, picks, comments -------------------------------------------------------------------

def test_review_files_written_by_the_cli_and_by_the_hub_reach_the_page(running):
    page = running.page()
    assert page.wait("wb_review")["items"] == []
    mark = page.mark()
    subprocess.run([str(WB), "ask", "Front limit", "--questions", json.dumps(QUESTIONS)], cwd=running.project.root,
                   check=True, capture_output=True, timeout=60)
    pushed = page.wait("wb_review", lambda m: [i["id"] for i in m["items"]] == ["R-0001"], after=mark, timeout=5)
    assert pushed["items"][0]["status"] == "waiting_owner"

    mark = page.mark()
    running.op("answer", id="R-0001", answers={"q1": {"choice": "b"}})
    pushed = page.wait("wb_review", lambda m: m["items"][0]["status"] == "waiting_agent", after=mark, timeout=5)
    assert pushed["items"][0]["answers"]["q1"]["choice"] == "b"
    assert running.state()["items"]["waiting_agent"] == 1


def test_a_pick_is_resolved_by_the_kernel_and_a_comment_freezes_it(running):
    shown = running.op("show", assembly="demo")
    status, body = running.request("POST", "/api/op/selection", {})
    assert status == 404 and "nothing is picked" in body["error"]
    page = running.page()
    page.wait("wb_scene")
    page.show_scene(shown["scene"])

    pick(page, shown["scene"], {"lastPick": {"path": "/demo_assembly", "name": "carriage"}}, point=[0.5, 0.5, 1.0], normal=[0, 0, 1])
    page.wait("wb_pick")
    selection = running.op("selection")
    assert (selection["label"], selection["kind"], selection["via"], selection["assembly"], selection["current"]) == (
        "carriage", "solid", "double-click", "demo", True)
    assert (selection["point"], selection["camera"], selection["build"]) == ([0.5, 0.5, 1.0], CAMERA, shown["build"])

    # A vertex: its exact point from the kernel, not the page's estimate. No viewer index is kept.
    mark = page.mark()
    pick(page, shown["scene"], {"activeTool": "PropertiesMeasurement", "selectedShapeIDs": ["/demo_assembly/base/vertices/vertices_2", False]}, point=[9.0, 9.0, 9.0])
    vertex = page.wait("wb_pick", after=mark)["selection"]
    assert (vertex["label"], vertex["kind"], vertex["point"], vertex["via"]) == ("base", "vertex", [1.0, 2.0, 3.0], "measure tool")
    assert "vertices_2" not in json.dumps(running.op("selection"))

    # The code moves on before the comment is sent: the evidence keeps the build the scene was made from.
    params = running.project.root / "src" / "demo" / "params.py"
    params.write_text(params.read_text() + "LIFT = {'stroke': 400}\n")
    assert running.wait_state(lambda s: s["build"] != shown["build"], 5, "a new build id")["build"] != shown["build"]

    png = tiny_png()
    comment = {"title": "Too close to the wheel", "body": "Check the clearance.", "camera": CAMERA,
               "image": "data:image/png;base64," + base64.b64encode(png).decode()}
    status, body = running.request("POST", "/api/page/comment", {**comment, "seq": selection["seq"]})
    assert status == 409 and "pick changed" in body["error"]  # the form was filled for the earlier pick
    status, item = running.request("POST", "/api/page/comment", {**comment, "seq": vertex["seq"]})
    assert status == 200, item
    assert (item["kind"], item["status"], item["targets"]) == ("comment", "waiting_agent", [{"kind": "part", "label": "base", "assembly": "demo"}])
    assert item["attachments"] == ["review/assets/R-0001/evidence.json", "review/assets/R-0001/thumbnail.png"]
    evidence = json.loads((running.project.root / "review/assets/R-0001/evidence.json").read_text())
    assert (evidence["point"], evidence["camera"], evidence["build"], evidence["kind"], evidence["part"]) == (
        [1.0, 2.0, 3.0], CAMERA, shown["build"], "vertex", "base")
    assert running.request("GET", "/api/asset/R-0001/thumbnail.png")[1] == png
    pins = page.wait("wb_review", lambda m: m["pins"], after=mark)["pins"]
    assert pins == [{"item": "R-0001", "assembly": "demo", "point": [1.0, 2.0, 3.0], "normal": None, "status": "waiting_agent"}]


# -- the app window ----------------------------------------------------------------------------

def _fake_chromium(bin_dir, record) -> None:
    """A stand-in `chromium` on PATH that records its argv and environment and exits."""
    fake = bin_dir / "chromium"
    fake.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ)}}, open({str(record)!r} + '.tmp', 'w'))\n"
        f"os.replace({str(record)!r} + '.tmp', {str(record)!r})\n"
    )
    fake.chmod(0o755)


def test_open_window_starts_a_native_wayland_app_window_without_the_stale_variables(project, tmp_path, monkeypatch):
    # open_window() itself (workbench.hub.launch): the subprocess mechanics, independent of
    # whether it is called from `wbench serve --open` or the `open` operation (both below).
    record = tmp_path / "chromium.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_chromium(bin_dir, record)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    monkeypatch.setenv("VIRTUAL_ENV", "/home/sandro/.venv")
    for name in STALE_GL_VARS:
        monkeypatch.setenv(name, "nvidia")

    with pytest.raises(HubNotRunning):  # the open operation needs a hub running in this process
        default_registry().get("open").call({}, project=project)

    result = open_window(project, HubInfo(pid=1234, port=5678, token="tok-123"), "page-cred-xyz")
    seen = json.loads(wait_until(lambda: record.exists() and record.read_text(), 10, "the fake chromium to run"))
    assert seen["argv"][0] == "--app=http://127.0.0.1:5678/#token=tok-123&pc=page-cred-xyz"
    assert "--ozone-platform=wayland" in seen["argv"]
    assert f"--user-data-dir={project.state_dir / 'chromium'}" in seen["argv"]
    assert sorted(set(STALE_GL_VARS) & set(seen["env"])) == [] and "VIRTUAL_ENV" not in seen["env"]
    assert seen["env"]["WAYLAND_DISPLAY"] == "wayland-test"
    assert result["url"] == "http://127.0.0.1:5678/"
    dumped = json.dumps(result)
    assert "tok-123" not in dumped and "page-cred-xyz" not in dumped  # neither secret in the result


def test_open_is_forwarded_to_the_hub_and_its_response_never_carries_the_page_credential(running, tmp_path, monkeypatch):
    # `open` is needs_hub=True (like show, selection, agents): the CLI forwards it over HTTP with
    # only the hub.json token, so the page credential it hands the real browser (workbench.hub.
    # launch) never has to pass through the CLI or appear in the op's own JSON result.
    record = tmp_path / "chromium.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_chromium(bin_dir, record)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")

    # open is not an owner write: the base token alone, with no page credential, is enough.
    status, result = running.request("POST", "/api/op/open", {}, page_token=None)
    assert status == 200, result
    dumped = json.dumps(result)
    assert running.page_credential not in dumped and running.token not in dumped

    seen = json.loads(wait_until(lambda: record.exists() and record.read_text(), 10, "the fake chromium to run"))
    url = next(a for a in seen["argv"] if a.startswith("--app="))
    assert f"token={running.token}" in url and f"pc={running.page_credential}" in url


def test_wbench_open_cli_forwards_over_http_and_never_sees_the_page_credential(running, tmp_path, monkeypatch):
    # The real `wbench open` subprocess this time (argv parsing, ops.client.forward's own HTTP
    # call), not just the operation or the route: proves the installed CLI, not only the Python
    # layer underneath it, never receives or prints the page credential. The browser itself is
    # launched by the hub (the `running` fixture, in this test process), not by the `wbench open`
    # subprocess, so PATH/WAYLAND_DISPLAY are set on this process, matching where open_window()
    # actually runs.
    record = tmp_path / "chromium.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_chromium(bin_dir, record)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    write_hub_info(running.project, port=running.port, token=running.token)  # as `wbench serve` would
    try:
        result = subprocess.run(
            [str(WB), "-C", str(running.project.root), "open", "--json"],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        (running.project.state_dir / "hub.json").unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    assert running.page_credential not in result.stdout and running.token not in result.stdout

    seen = json.loads(wait_until(lambda: record.exists() and record.read_text(), 10, "the fake chromium to run"))
    url = next(a for a in seen["argv"] if a.startswith("--app="))
    assert f"token={running.token}" in url and f"pc={running.page_credential}" in url
