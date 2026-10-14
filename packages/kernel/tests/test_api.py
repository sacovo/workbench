"""The manifest API: registering builds nothing, and builders the kernel could not call are refused."""

import importlib

import pytest

from workbench_kernel.api import ManifestError, Project


def test_registering_calls_nothing_and_lists_arguments_with_defaults():
    calls = []
    wb = Project("p")

    @wb.assembly("thing")
    def thing(height: float | None = None, *, side: str = "left"):
        """A thing."""
        calls.append((height, side))

    assert calls == []
    described = wb.describe()["assemblies"]["thing"]
    assert described["doc"] == "A thing."
    assert described["args"] == [
        {"name": "height", "default": None, "annotation": "float | None"},
        {"name": "side", "default": "left", "annotation": "str"},
    ]
    spec = wb.assemblies["thing"]
    assert spec.resolve_args({"height": 3.0}) == {"height": 3.0, "side": "left"}
    with pytest.raises(TypeError, match="no argument 'width'"):
        spec.resolve_args({"width": 1})


def test_importing_the_example_manifest_builds_nothing(monkeypatch):
    import minimal.assembly
    import minimal.manifest

    def refuse(*args, **kwargs):
        raise AssertionError("the manifest built an assembly at import")

    monkeypatch.setattr(minimal.assembly, "minimal_assembly", refuse)
    assert list(importlib.reload(minimal.manifest).wb.assemblies) == ["minimal"]


@pytest.mark.parametrize(
    ("define", "message"),
    [
        (lambda wb: wb.assembly("a")(lambda height: None), "needs a default"),
        (lambda wb: wb.assembly("a")(lambda *parts: None), "only named arguments"),
        (lambda wb: wb.assembly("a")(lambda **options: None), "only named arguments"),
        (lambda wb: wb.assembly("a")(lambda platform=object(): None), "use None, bool, int, float or str"),  # noqa: B008 - the point
        (lambda wb: [wb.assembly("a")(lambda: None), wb.assembly("a")(lambda: None)], "registered twice"),
    ],
)
def test_builders_the_kernel_could_not_call_are_refused_when_registered(define, message):
    with pytest.raises(ManifestError, match=message):
        define(Project("p"))


def test_declared_parameters_must_exist():
    Project("p", params={"minimal.params": ["PLATE"]}).check_params()
    with pytest.raises(ManifestError, match=r"minimal\.params\.NOPE"):
        Project("p", params={"minimal.params": ["PLATE", "NOPE"]}).check_params()
