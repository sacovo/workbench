"""update sends only what changed, and agrees with a full build of the same state; fingerprints
are placement-free and the same in another kernel process."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from minimal.params import BRACKET, PIN
from minimal.parts import bracket
from support import MINIMAL, KernelProcess, leaves

LOWER = 12.0  # a second pin height, inside the bracket's arm


@pytest.fixture(scope="module")
def other_kernel(tmp_path_factory):
    kernel = KernelProcess(MINIMAL, tmp_path_factory.mktemp("other") / "stderr.log")
    yield kernel
    kernel.close()


def _payload(result) -> dict:
    return json.loads(Path(result["blob"]["path"]).read_bytes())


def _instance(data: dict, leaf: dict) -> dict:
    shape = leaf["shape"]
    return data["instances"][shape["ref"]] if "ref" in shape else shape


def test_update_moves_and_re_tessellates_only_what_changed(minimal_kernel, other_kernel):
    first = minimal_kernel.ok("build", assembly="minimal", args={"pin_height": BRACKET.pin_height})
    update = minimal_kernel.ok("update", assembly="minimal", args={"pin_height": LOWER})

    assert update["incremental"] is True and update["reason"] is None
    assert update["previous"] == first["scene"]
    assert [m["label"] for m in update["moved"]] == ["pin"]
    assert [c["label"] for c in update["changed"]] == ["bracket"]

    # A full build of the same state, in another kernel, is the reference. The moved loc is the
    # loc a full show gives the pin, which is not its build123d location (spike S1, fix 1); the
    # re-tessellated bracket is the leaf a full show would send.
    full = other_kernel.ok("build", assembly="minimal", args={"pin_height": LOWER})
    full_data = _payload(full)["data"]
    full_leaves = {leaf["id"]: leaf for leaf in leaves(full_data["shapes"])}
    moved = update["moved"][0]
    assert moved["loc"][0] == pytest.approx(full_leaves[moved["path"]]["loc"][0], abs=1e-9)
    assert moved["loc"][1] == pytest.approx(full_leaves[moved["path"]]["loc"][1], abs=1e-12)
    path = update["changed"][0]["path"]
    sent = _payload(update)
    assert update["blob"]["kind"] == "parts" and list(sent["parts"]) == [path]
    (leaf,) = leaves(sent["parts"][path]["shapes"])
    assert leaf["loc"] == full_leaves[path]["loc"]
    assert _instance(sent["parts"][path], leaf) == _instance(full_data, full_leaves[path])
    for mine, reference in zip(update["parts"], full["parts"], strict=True):
        assert {k: mine[k] for k in ("path", "label", "fingerprint", "n_faces")} == {k: reference[k] for k in ("path", "label", "fingerprint", "n_faces")}
        assert mine["bbox"] == pytest.approx(reference["bbox"], abs=1e-9)

    # Picks resolve against the moved and re-cut parts; the previous scene still answers too.
    pin_path = moved["path"]
    now = minimal_kernel.ok("resolve", scene=update["scene"], ids=[pin_path])["results"][0]
    before = minimal_kernel.ok("resolve", scene=first["scene"], ids=[pin_path])["results"][0]
    assert now["world"]["centre"] == pytest.approx(list(bracket.pin_axis(replace(BRACKET, pin_height=LOWER))), abs=1e-9)
    assert before["world"]["centre"] == pytest.approx(list(bracket.pin_axis(BRACKET)), abs=1e-9)
    bracket_part = next(p for p in update["parts"] if p["label"] == "bracket")
    faces = minimal_kernel.ok("resolve", scene=update["scene"], ids=[f"{path}/faces/faces_{i}" for i in range(bracket_part["n_faces"])])["results"]
    pin_holes = [f["world"] for f in faces if f["world"]["geom_type"] == "cylinder" and f["world"]["radius"] == pytest.approx(PIN.diameter / 2 + BRACKET.pin_clearance)]
    assert len(pin_holes) == 1
    assert pin_holes[0]["axis"]["origin"][2] == pytest.approx(bracket.pin_axis(replace(BRACKET, pin_height=LOWER))[2], abs=1e-9)

    # Nothing moved, nothing changed: nothing is sent.
    same = minimal_kernel.ok("update", assembly="minimal", args={"pin_height": LOWER})
    assert (same["incremental"], same["moved"], same["changed"], same["blob"]) == (True, [], [], None)


def test_update_falls_back_to_a_full_build_and_says_why(noisy_project):
    kernel = KernelProcess(noisy_project, noisy_project / "stderr.log", pythonpath=noisy_project)
    try:
        first = kernel.ok("update", assembly="boxes")
        assert first["incremental"] is False and "no earlier scene" in first["reason"]
        assert first["blob"]["kind"] == "show"
        grown = kernel.ok("update", assembly="boxes", args={"extra": True})
        assert grown["incremental"] is False and "added ['c']" in grown["reason"]
        assert grown["blob"]["kind"] == "show" and [p["label"] for p in grown["parts"]] == ["a", "b", "c"]
    finally:
        kernel.close()


def test_fingerprints_ignore_placement_and_match_across_processes(minimal_kernel, other_kernel):
    def fingerprints(kernel, pin_height):
        result = kernel.ok("build", assembly="minimal", args={"pin_height": pin_height})
        return {p["label"]: p["fingerprint"] for p in result["parts"]}

    here = fingerprints(minimal_kernel, BRACKET.pin_height)
    lower = fingerprints(minimal_kernel, LOWER)
    there = fingerprints(other_kernel, BRACKET.pin_height)

    assert there == here
    assert (lower["plate"], lower["pin"]) == (here["plate"], here["pin"])  # the pin only moved
    assert lower["bracket"] != here["bracket"]  # its pin hole was re-cut
