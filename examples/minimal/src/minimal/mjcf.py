"""MuJoCo model generation from the same parameters as the CAD.

CAD is in mm; everything here is converted to metres at the boundary.
"""

import mujoco

from minimal.params import BRACKET, PIN, PLATE
from minimal.parts import bracket, pin, plate

MM = 1e-3


def _add_box(body, name, x, y, z, mass_kg, rgba, offset=(0.0, 0.0, 0.0)):
    """Box geom from its extents in mm (a part's own bounding box), shifted by `offset` mm."""
    return body.add_geom(
        name=name,
        type=mujoco.mjtGeom.mjGEOM_BOX,
        size=[(x[1] - x[0]) / 2 * MM, (y[1] - y[0]) / 2 * MM, (z[1] - z[0]) / 2 * MM],
        pos=[(sum(x) / 2 + offset[0]) * MM, (sum(y) / 2 + offset[1]) * MM, (sum(z) / 2 + offset[2]) * MM],
        mass=mass_kg,
        rgba=rgba,
    )


def minimal_spec(plate_params=PLATE, bracket_params=BRACKET, pin_params=PIN) -> mujoco.MjSpec:
    """Floor, a free body carrying the plate, bracket and pin as box geoms with masses
    from their CAD volume, and a lever on a hinge at the pin's axis with a position actuator.
    """
    spec = mujoco.MjSpec()
    spec.modelname = "minimal"
    spec.worldbody.add_geom(name="floor", type=mujoco.mjtGeom.mjGEOM_PLANE, size=[1, 1, 0.1], rgba=[0.9, 0.9, 0.9, 1])

    plate_part = plate.make(plate_params)
    bracket_part = bracket.make(bracket_params, pin_params)
    pin_part = pin.make(pin_params)

    # The body's own frame is the plate frame (origin on the plate's top face); this height
    # puts the plate's bottom face, at local z = -thickness, exactly on the floor.
    fixture = spec.worldbody.add_body(name="fixture", pos=[0, 0, plate_params.thickness * MM])
    fixture.add_freejoint(name="fixture")

    # Mass on a geom is ignored if its body has an explicit <inertial> (see CLAUDE.md); `fixture`
    # has none, so MuJoCo sums these three geoms' masses into the body automatically.
    plate_box = plate_part.bounding_box()
    _add_box(
        fixture,
        "plate",
        (plate_box.min.X, plate_box.max.X),
        (plate_box.min.Y, plate_box.max.Y),
        (plate_box.min.Z, plate_box.max.Z),
        plate_part.volume * plate_params.density * 1e-3,
        [0.75, 0.76, 0.78, 1],
    )
    bracket_box = bracket_part.bounding_box()
    _add_box(
        fixture,
        "bracket",
        (bracket_box.min.X, bracket_box.max.X),
        (bracket_box.min.Y, bracket_box.max.Y),
        (bracket_box.min.Z, bracket_box.max.Z),
        bracket_part.volume * bracket_params.density * 1e-3,
        [0.9, 0.5, 0.1, 1],
    )
    # pin.make() returns the pin centred on its own origin; its assembly position (the
    # bracket's pin axis) is passed as `offset` rather than baked into the box extents.
    pin_axis = bracket.pin_axis(bracket_params)
    pin_box = pin_part.bounding_box()
    _add_box(
        fixture,
        "pin",
        (pin_box.min.X, pin_box.max.X),
        (pin_box.min.Y, pin_box.max.Y),
        (pin_box.min.Z, pin_box.max.Z),
        pin_part.volume * pin_params.density * 1e-3,
        [0.3, 0.3, 0.3, 1],
        offset=pin_axis,
    )

    # Lever on the pin's axis: a hinge along the pin (local x), held by a position actuator.
    px, py, pz = pin_axis
    lever = fixture.add_body(name="lever", pos=[px * MM, py * MM, pz * MM])
    lever.add_joint(name="lever_hinge", type=mujoco.mjtJoint.mjJNT_HINGE, axis=[1, 0, 0], damping=0.05)
    lever.add_geom(
        name="lever",
        type=mujoco.mjtGeom.mjGEOM_BOX,
        size=[0.003, 0.002, 0.02],
        pos=[0, 0, 0.02],
        mass=0.01,  # kg, a nominal indicator mass: no CAD part backs the lever itself
        rgba=[0.2, 0.6, 0.2, 1],
    )
    actuator = spec.add_actuator(name="lever_hinge", target="lever_hinge", trntype=mujoco.mjtTrn.mjTRN_JOINT)
    actuator.set_to_position(kp=2.0, kv=0.1)
    actuator.ctrlrange = [-1.57, 1.57]

    return spec
