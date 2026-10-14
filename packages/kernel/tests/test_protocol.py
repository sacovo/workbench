"""The kernel's protocol: what hello describes, what build writes, how failures are answered, and
that stdout carries protocol lines and nothing else."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import minimal
import ocp_viewer_core
import pytest
from minimal.assembly import minimal_assembly
from minimal.params import BRACKET
from support import MINIMAL, KernelProcess, leaves

import workbench_kernel


def test_hello_describes_the_running_project(minimal_kernel):
    hello = minimal_kernel.ok("hello")

    assert hello["pid"] == minimal_kernel.proc.pid
    assert hello["kernel_version"] == workbench_kernel.__version__
    assert hello["project_root"] == str(MINIMAL)
    assert hello["code_root"] == str(Path(minimal.__file__).resolve().parent)
    assert hello["code_root_in_project"] is True
    assert hello["manifest_error"] is None
    manifest = hello["manifest"]
    assert manifest["name"] == "minimal" and manifest["units"] == "mm"
    # Arguments and defaults come from the builder's signature.
    assert manifest["assemblies"]["minimal"]["args"] == [{"name": "pin_height", "default": BRACKET.pin_height, "annotation": "float"}]
    assert manifest["params"] == {"minimal.params": ["PLATE", "BRACKET", "PIN"]}


def test_build_writes_the_payload_the_stock_page_expects(minimal_kernel):
    result = minimal_kernel.ok("build", assembly="minimal")

    blob = result["blob"]
    path = Path(blob["path"])
    data = path.read_bytes()
    assert path.parent == MINIMAL / ".workbench" / "blobs"
    assert blob["kind"] == "show"
    assert (hashlib.sha256(data).hexdigest(), len(data)) == (blob["sha"], blob["bytes"])
    payload = json.loads(data)
    assert payload["type"] == "data"
    # The config went through Session.send_data: camelCase names and the version handshake
    # (spike S5: snake_case keys are silently ignored by the page).
    config = payload["config"]
    assert config["_core_version"] == ocp_viewer_core.__version__
    assert "resetCamera" in config and "reset_camera" not in config
    assert payload["count"] == 3

    expected = {c.label: c for c in minimal_assembly().children}
    parts = result["parts"]
    assert [p["label"] for p in parts] == list(expected)
    assert [leaf["id"] for leaf in leaves(payload["data"]["shapes"])] == [p["path"] for p in parts]
    for part in parts:
        child = expected[part["label"]]
        assert part["n_faces"] == len(child.faces()), part["label"]
        box = child.bounding_box()
        assert part["bbox"] == pytest.approx([box.min.X, box.min.Y, box.min.Z, box.max.X, box.max.Y, box.max.Z], abs=1e-6)


def test_the_page_state_passed_with_a_build_decides_camera_and_toolbar(minimal_kernel):
    """A page still on the logo gets a camera reset and default settings; a page showing a model
    keeps its camera and its own toolbar state, as the stock pipeline decides from what it is told."""

    def config(**viewer):
        return json.loads(Path(minimal_kernel.ok("build", assembly="minimal", **viewer)["blob"]["path"]).read_bytes())["config"]

    fresh = config()
    assert (fresh["resetCamera"], fresh["transparent"]) == ("reset", False)
    showing = config(viewer={"splash": False, "status": {"transparent": True}})
    assert (showing["resetCamera"], showing["transparent"]) == ("keep", True)


def test_facts_measure_each_part(minimal_kernel):
    result = minimal_kernel.ok("facts", assembly="minimal", args={"pin_height": 12.0})

    expected = {c.label: c for c in minimal_assembly(bracket_params=replace(BRACKET, pin_height=12.0)).children}
    assert [p["label"] for p in result["parts"]] == list(expected)
    for part in result["parts"]:
        child = expected[part["label"]]
        assert part["is_valid"] is True
        assert part["volume"] == pytest.approx(child.volume, rel=1e-9)
        box = child.bounding_box()
        assert part["bbox"] == pytest.approx([box.min.X, box.min.Y, box.min.Z, box.max.X, box.max.Y, box.max.Z], abs=1e-6)


@pytest.mark.parametrize(
    ("line", "error_type", "message"),
    [
        (b"not json", "ValueError", "one JSON object per line"),
        (b"[1, 2]", "ValueError", "a JSON object"),
        (b'{"id": 7, "op": "explode"}', "ValueError", "unknown op"),
        (b'{"id": 7, "op": "build", "args": {"assembly": "nope"}}', "RequestError", "no assembly 'nope'"),
        (b'{"id": 7, "op": "build", "args": {"assembly": "minimal", "args": {"height": 1}}}', "TypeError", "no argument 'height'"),
        (b'{"id": 7, "op": "resolve", "args": {"scene": "x", "ids": ["/a"], "extra": 1}}', "TypeError", "extra"),
        (b'{"id": 7, "op": "resolve", "args": {"scene": "nope", "ids": ["/a"]}}', "RequestError", "no scene 'nope'"),
    ],
)
def test_a_bad_request_gets_an_error_and_the_kernel_goes_on(minimal_kernel, line, error_type, message):
    minimal_kernel.send_line(line)
    response = json.loads(minimal_kernel.read_line())

    assert response["ok"] is False
    assert response["id"] == (7 if line.startswith(b'{"id": 7') else None)
    assert response["error"]["type"] == error_type
    assert message in response["error"]["message"]
    assert minimal_kernel.ok("hello")["pid"] == minimal_kernel.proc.pid


def test_an_error_inside_a_builder_is_answered_and_the_kernel_stays_alive(minimal_kernel):
    response = minimal_kernel.request("build", assembly="minimal", args={"pin_height": "high"})

    assert response["ok"] is False
    assert response["error"]["type"] == "TypeError"
    assert "minimal/parts/bracket.py" in response["error"]["traceback"]  # raised in the project's code
    assert [p["label"] for p in minimal_kernel.ok("build", assembly="minimal")["parts"]] == ["plate", "bracket", "pin"]


def test_stdout_carries_only_protocol_lines(noisy_project):
    kernel = KernelProcess(noisy_project, noisy_project / "stderr.log", pythonpath=noisy_project)
    try:
        assert kernel.ok("hello")["manifest"]["name"] == "noisy"
        assert [p["label"] for p in kernel.ok("build", assembly="boxes")["parts"]] == ["a", "b"]
        failed = kernel.request("build", assembly="broken")
        assert failed["ok"] is False and failed["error"]["type"] == "RuntimeError"
        assert "deliberate failure in a builder" in failed["error"]["message"]
        assert kernel.ok("hello")["manifest_error"] is None
    finally:
        rest = kernel.close()

    assert rest == b""
    assert kernel.proc.returncode == 0
    responses = [json.loads(line) for line in kernel.stdout_lines]
    assert [r["id"] for r in responses] == [1, 2, 3, 4]
    stderr = kernel.stderr()
    for noise in ("noise from print at import", "noise on fd 1 at import", "noise from print in a builder",
                  "noise on fd 1 in a builder", "noise from a builder about to fail"):
        assert noise in stderr


def test_a_manifest_that_fails_to_import_is_reported_and_the_kernel_answers(noisy_project):
    (noisy_project / "broken_manifest.py").write_text('raise ImportError("manifest broken on purpose")\n')
    pyproject = noisy_project / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace('"noisy_manifest"', '"broken_manifest"'))
    kernel = KernelProcess(noisy_project, noisy_project / "stderr.log", pythonpath=noisy_project)
    try:
        hello = kernel.ok("hello")
        assert hello["manifest"] is None
        assert hello["manifest_error"]["type"] == "ImportError"
        assert "manifest broken on purpose" in hello["manifest_error"]["traceback"]
        refused = kernel.request("build", assembly="boxes")
        assert refused["ok"] is False and refused["error"]["type"] == "ManifestError"
        assert "manifest broken on purpose" in refused["error"]["traceback"]
    finally:
        kernel.close()


def test_a_root_with_its_own_location_is_measured_where_it_is_drawn(noisy_project):
    """The stock backend places leaves by their own loc only; the kernel composes the root's."""
    kernel = KernelProcess(noisy_project, noisy_project / "stderr.log", pythonpath=noisy_project)
    try:
        build = kernel.ok("build", assembly="located", args={"x": 100.0})
        payload = json.loads(Path(build["blob"]["path"]).read_bytes())
        assert payload["data"]["shapes"]["loc"][0] == [100.0, 0.0, 0.0]  # drawn: the root group's loc
        part_a = next(p for p in build["parts"] if p["label"] == "a")
        assert part_a["bbox"] == pytest.approx([95, -5, -5, 105, 5, 5], abs=1e-6)

        resolved = kernel.ok("resolve", scene=build["scene"], ids=["/located/a"])["results"][0]
        assert resolved["world"]["centre"] == pytest.approx([100, 0, 0], abs=1e-9)
        assert resolved["local"]["centre"] == pytest.approx([0, 0, 0], abs=1e-9)

        kernel.ok("backend", message={"activeTool": "PropertiesMeasurement"})
        reply = kernel.ok("backend", message={"selectedShapeIDs": ["/located/a", False]})["reply"]
        assert reply["refpoint"] == pytest.approx([100, 0, 0], abs=1e-9)
    finally:
        kernel.close()
