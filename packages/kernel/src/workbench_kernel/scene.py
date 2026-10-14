"""A built assembly as the kernel holds it: its parts, where the viewer has them, and its backend table.

Parts are the assembly's children, identified by label. A part's `location` is the child's own
build123d location (relative to the assembly root); its `placement` is the root's location times
that, i.e. the part frame in the viewer's world. "Part-local" coordinates are world coordinates
taken through the inverse of `placement`, which is also the frame the fingerprint hashes in
(the shape with its location stripped).

The viewer's loc for a leaf is not the part's location: ocp-tessellate folds nested locations
into the leaf (a `Pos(c) * Box(...)` part sits at the box's corner frame), so a moved part's new
viewer loc is the change of its location composed onto the loc the viewer already holds (spike
S1, fix 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.TopLoc import TopLoc_Location

from workbench_kernel import host
from workbench_kernel.fingerprint import fingerprint


@dataclass
class Draft:
    """A part of a freshly built compound, before it is shown."""

    label: str
    shape: Any  # the build123d child
    location: TopLoc_Location  # the child's own location, relative to the assembly root
    placement: TopLoc_Location  # root location * child location
    fingerprint: str
    look: tuple  # what the viewer shows besides geometry: colour and material


@dataclass
class Part:
    label: str
    path: str
    shape: Any
    location: TopLoc_Location
    placement: TopLoc_Location
    fingerprint: str
    look: tuple
    viewer_loc: list | None  # the loc the viewer holds for this part's node
    leaf: bool  # one leaf in the viewer's tree (False: a group with leaves under it)
    n_faces: int
    bbox: list[float]

    def describe(self) -> dict:
        return {"path": self.path, "label": self.label, "fingerprint": self.fingerprint, "n_faces": self.n_faces, "bbox": self.bbox}


@dataclass
class Scene:
    id: str
    assembly: str
    args: dict
    root_path: str
    root_location: TopLoc_Location
    compound: Any
    parts: dict[str, Part] = field(default_factory=dict)  # by label, in assembly order
    model: dict = field(default_factory=dict)  # backend id -> shape in the viewer's world
    blob: dict | None = None  # the blob this scene's build or update wrote, if any

    def part_of(self, shape_id: str) -> Part | None:
        """The part a viewer id belongs to: the path segment after the root names it."""
        if not shape_id.startswith(self.root_path + "/"):
            return None
        return self.parts.get(shape_id[len(self.root_path) + 1 :].split("/", 1)[0])


def children_of(compound, assembly: str) -> list:
    """The labelled children of what a builder returned, or a ValueError that says what is wrong."""
    children = getattr(compound, "children", None)
    if getattr(compound, "wrapped", None) is None or not children:
        raise ValueError(
            f"builder of {assembly!r} returned {type(compound).__name__}; "
            "expected a build123d Compound whose children carry labels"
        )
    labels = [getattr(c, "label", "") for c in children]
    unlabelled = [i for i, label in enumerate(labels) if not label]
    if unlabelled:
        raise ValueError(f"children of {assembly!r} at positions {unlabelled} have no label")
    with_slash = [label for label in labels if "/" in label]
    if with_slash:
        raise ValueError(f"labels of {assembly!r} must not contain '/': {with_slash}")
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        raise ValueError(f"labels of {assembly!r} must be unique; repeated: {duplicates}")
    return list(children)


def _look(child) -> tuple:
    color = getattr(child, "color", None)
    material = getattr(child, "material", None)
    return (None if color is None else repr(color), material if isinstance(material, str) else None)


def drafts(compound, assembly: str) -> list[Draft]:
    root = compound.location.wrapped
    out = []
    for child in children_of(compound, assembly):
        location = child.location.wrapped
        out.append(Draft(child.label, child, location, root.Multiplied(location), fingerprint(child), _look(child)))
    return out


def bbox_of(shapes) -> list[float]:
    """Exact axis-aligned box [xmin, ymin, zmin, xmax, ymax, zmax] of OCCT shapes, from their
    geometry, not their triangulation, and without changing them (build123d's bounding_box
    strips the mesh from the shape it measures)."""
    box = Bnd_Box()
    for shape in shapes:
        BRepBndLib.AddOptimal_s(shape, box, False, False)
    if box.IsVoid():
        raise ValueError("bounding box of nothing")
    lo, hi = box.CornerMin(), box.CornerMax()
    return [lo.X(), lo.Y(), lo.Z(), hi.X(), hi.Y(), hi.Z()]


def ids_by_part(model: dict, root_path: str) -> dict[str, list[str]]:
    """Backend ids grouped by the part they belong to (the path segment after the root), in one pass."""
    groups: dict[str, list[str]] = {}
    prefix = root_path + "/"
    for k in model:
        if k.startswith(prefix):
            groups.setdefault(k[len(prefix) :].split("/", 1)[0], []).append(k)
    return groups


def leaf_shapes(model: dict, ids: list[str], path: str) -> list:
    """The shapes of a part's leaves (one for a leaf part), without its faces, edges and vertices."""
    return [model[k] for k in ids if not host.is_sub_shape(k, path)]


def count_faces(ids: list[str], path: str) -> int:
    return sum(1 for k in ids if "/faces/" in k[len(path) :])


def attach(drafts_: list[Draft], tree: dict, model: dict) -> dict[str, Part]:
    """Parts from drafts and the viewer tree a show made of them."""
    nodes = {node.get("name"): node for node in tree.get("parts") or []}
    groups = ids_by_part(model, tree["id"])
    parts = {}
    for d in drafts_:
        node = nodes.get(d.label)
        if node is None:
            raise RuntimeError(f"the viewer tree has no node for part {d.label!r}")
        path, ids = node["id"], groups.get(d.label, [])
        parts[d.label] = Part(
            d.label, path, d.shape, d.location, d.placement, d.fingerprint, d.look,
            node.get("loc"), node.get("parts") is None, count_faces(ids, path), bbox_of(leaf_shapes(model, ids, path)),
        )
    return parts


def same_location(a: TopLoc_Location, b: TopLoc_Location, tol: float = 1e-9) -> bool:
    ta, tb = a.Transformation(), b.Transformation()
    return all(abs(ta.Value(i, j) - tb.Value(i, j)) <= tol for i in (1, 2, 3) for j in (1, 2, 3, 4))


def plan_update(previous: Scene, compound, new: list[Draft]) -> tuple[list[str], list[str]] | str:
    """(moved labels, changed labels) against the scene the viewer shows, or the reason an
    incremental update cannot express the change."""
    if (compound.label or "") != (previous.compound.label or ""):
        return f"the assembly root is now labelled {compound.label!r}, the viewer shows {previous.root_path!r}"
    if not same_location(compound.location.wrapped, previous.root_location):
        return "the assembly root moved"
    old_labels, new_labels = list(previous.parts), [d.label for d in new]
    if old_labels != new_labels:
        added = [x for x in new_labels if x not in previous.parts]
        removed = [x for x in old_labels if x not in new_labels]
        if added or removed:
            return f"parts added {added} or removed {removed}"
        return "the parts are in a different order"
    moved, changed = [], []
    for d in new:
        old = previous.parts[d.label]
        if d.fingerprint != old.fingerprint or d.look != old.look:
            if not old.leaf:
                return f"part {d.label!r} changed and is a group in the viewer, not one leaf"
            changed.append(d.label)
        elif not same_location(d.location, old.location):
            moved.append(d.label)
    return moved, changed
