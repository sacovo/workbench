"""`wb serve` with the real kernel on examples/minimal, as processes: the CLI shows and selects, a
page client receives the scene and its update, picks are resolved by the kernel and measured by the
stock backend. Expected geometry comes from the example's parameters, not from the kernel.

Needs examples/minimal's environment (cd examples/minimal && env -u VIRTUAL_ENV uv sync).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
import subprocess
import types
from pathlib import Path

import pytest

from hub_helpers import WB
from hub_running import PageClient, kernel_processes, wait_until

MINIMAL = Path(__file__).resolve().parents[1] / "examples" / "minimal"

# examples/minimal/src/minimal/params.py
PLATE = {"length": 80.0, "width": 60.0, "thickness": 4.0, "corner_radius": 4.0, "hole_diameter": 3.4}
BRACKET = {"base_thickness": 6.0, "pin_height": 18.0}


def env() -> dict:
    return {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}


def wb(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run([str(WB), "-C", str(MINIMAL), *args], capture_output=True, text=True, timeout=300, env=env())
    if check:
        assert result.returncode == 0, result.stderr
    return result


def api(info: dict, path: str):
    import urllib.request

    request = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}", headers={"X-Workbench-Token": info["token"]})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=30) as response:
        return json.loads(response.read())


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    assert (MINIMAL / ".venv").is_dir(), "examples/minimal has no environment: cd examples/minimal && env -u VIRTUAL_ENV uv sync"
    assert not (MINIMAL / "review").exists(), "examples/minimal/review exists; this test deletes the review/ it creates"
    hub_json = MINIMAL / ".workbench" / "hub.json"
    assert not hub_json.exists(), f"{hub_json} exists: is a hub running on examples/minimal?"
    log = tmp_path_factory.mktemp("serve") / "serve.log"
    with open(log, "wb") as out:
        proc = subprocess.Popen([str(WB), "-C", str(MINIMAL), "serve"], stdout=out, stderr=subprocess.STDOUT, env=env(), start_new_session=True)
    try:
        info = wait_until(lambda: hub_json.exists() and json.loads(hub_json.read_text()), 30, "hub.json")
        wait_until(lambda: _ready(info), 180, "the kernel to be ready")
        yield types.SimpleNamespace(proc=proc, info=info, port=info["port"], token=info["token"], log=log)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(10)
        shutil.rmtree(MINIMAL / "review", ignore_errors=True)
    assert not hub_json.exists(), "hub.json left behind"
    wait_until(lambda: kernel_processes(MINIMAL) == [], 10, "the kernel to stop with the hub")


def _ready(info: dict) -> bool:
    try:
        state = api(info, "/api/state")
    except OSError:
        return False
    assert state["kernel"]["state"] != "crashed", state["kernel"]["error"]
    return state["kernel"]["state"] == "ready"


def plate_volume() -> float:
    p = PLATE
    area = p["length"] * p["width"] - 4 * (p["corner_radius"] ** 2 - math.pi * p["corner_radius"] ** 2 / 4) - 4 * math.pi * (p["hole_diameter"] / 2) ** 2
    return area * p["thickness"]


def leaves(node: dict) -> list[dict]:
    out = []
    for child in node.get("parts") or []:
        out += leaves(child) if child.get("parts") is not None else [child]
    return out


def test_show_pick_measure_and_update_through_the_real_kernel(served):
    shown = json.loads(wb("show", "minimal", "--json").stdout)
    assert (shown["incremental"], shown["parts"]) == (False, ["plate", "bracket", "pin"])

    page = PageClient(served)
    try:
        scene = page.wait("wb_scene", lambda m: m["scene"] == shown["scene"], timeout=30)
        payload = api(served.info, f"/api/blob/{scene['blob']['sha']}")
        assert payload["type"] == "data" and "resetCamera" in payload["config"] and "_core_version" in payload["config"]
        by_label = {leaf["name"]: leaf for leaf in leaves(payload["data"]["shapes"])}
        assert sorted(by_label) == ["bracket", "pin", "plate"]
        page.send({"type": "shown", "scene": scene["scene"]})

        # A double-click on the plate: the whole part, measured exactly.
        page.send({"type": "status", "scene": scene["scene"], "status": {"lastPick": {"path": scene["root"], "name": "plate"}},
                   "pick": {"point": [10.0, 5.0, 0.0], "camera": {"position": [0, 0, 500], "target": [0, 0, 0]}}})
        page.wait("wb_pick", timeout=30)
        selection = json.loads(wb("selection", "--json").stdout)
        half_x, half_y = PLATE["length"] / 2, PLATE["width"] / 2
        assert (selection["label"], selection["kind"], selection["via"]) == ("plate", "solid", "double-click")
        assert selection["world"]["bbox"] == pytest.approx([-half_x, -half_y, -PLATE["thickness"], half_x, half_y, 0.0], abs=1e-6)
        assert selection["world"]["volume"] == pytest.approx(plate_volume(), rel=1e-9)
        assert "plate (solid) in minimal" in wb("selection").stdout

        # The Properties tool on a face: the stock backend answers through the hub, the pick resolves.
        mark = page.mark()
        page.send({"type": "status", "scene": scene["scene"],
                   "status": {"activeTool": "PropertiesMeasurement", "selectedShapeIDs": [f"{scene['root']}/plate/faces/faces_0", False]}})
        reply = page.wait("wb_backend", after=mark, timeout=30)["reply"]
        face = page.wait("wb_pick", after=mark, timeout=30)["selection"]
        assert (reply["type"], reply["tool_type"]) == ("backend_response", "PropertiesMeasurement")
        backend_area = next(section["area"] for section in reply["result"] if "area" in section)
        assert (face["label"], face["kind"]) == ("plate", "face")
        assert face["world"]["area"] == pytest.approx(backend_area, rel=1e-9)  # resolve.py and the stock measure agree

        # New arguments: an incremental update; the pin moves by the change of its axis height.
        mark = page.mark()
        lower = 12.0
        update = json.loads(wb("show", "minimal", "--args", json.dumps({"pin_height": lower}), "--json").stdout)
        assert (update["incremental"], update["moved"], update["changed"]) == (True, ["pin"], ["bracket"])
        message = page.wait("wb_update", after=mark, timeout=30)
        (moved,) = message["moved"]
        dz = moved["loc"][0][2] - by_label["pin"]["loc"][0][2]
        assert dz == pytest.approx(lower - BRACKET["pin_height"], abs=1e-9)
        parts = api(served.info, f"/api/blob/{message['blob']['sha']}")["parts"]
        assert list(parts) == [by_label["bracket"]["id"]]
    finally:
        page.close()

    second = wb("serve", check=False)
    assert second.returncode == 1 and "a hub already runs" in second.stderr
