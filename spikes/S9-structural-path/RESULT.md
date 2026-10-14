# S9: one structural path against a closed form

Question (plan, spike S9): does gmsh plus CalculiX (`pixi exec -s calculix ccx`, ccx 2.23), with
scikit-fem as a cross-check, match a closed-form case within 5 %?

Date 2026-10-14. gmsh 4.15 and scikit-fem from PyPI in a throwaway uv environment (`run.py`
docstring); ccx 2.23 from conda-forge through pixi 0.46. **M** = measured by running.

Case: solid aluminium cantilever 200 x 20 x 10 mm, E 70 GPa, nu 0.33, clamped face at x = 0, 100 N
shear spread over the end face (equivalent nodal loads of 6-node triangles), second-order
tetrahedra (C3D10 / P2), three meshes (2004, 5242, 12832 elements).

| Check | Result | Verdict |
|---|---|---|
| Tip deflection against Timoshenko (2.2903 mm) | 2.2704 mm, -0.87 % | **PASS** (M) |
| First bending frequency against Euler-Bernoulli (205.6 Hz) | 206.4 Hz, +0.38 % | **PASS** (M) |
| Reaction at the clamp | 100.000 N against the 100 N load | PASS (M) |
| Strain energy against F * deflection / 2 | agrees to 1e-6 | PASS (M) |
| Convergence | 0.04 % between the two finest meshes; deflection rises monotonically | PASS (M) |
| scikit-fem on the same mesh | agrees with ccx to 6e-7 | PASS (M) |

The scikit-fem agreement shows that the ccx input deck (node order, loads, constraints, material)
is what was meant; it is not an independent check of the discretisation, which the closed forms are.
The fully clamped face makes the model about 0.9 % stiffer than Timoshenko's beam, as expected.

Lessons for the real path:
- gmsh writes the last two edge nodes of a tet10 as (2,3), (1,3); CalculiX wants (1,3), (2,3).
  `run.py` swaps them and asserts every edge node is its edge's midpoint.
- ccx's `.dat` has separate blocks per step and per mode; parse the static step and the eigenvalue
  table separately (`parse_dat`).
- 12832 tet10 solve plus frequency in a few seconds; the whole three-mesh run takes 40 s.

Not yet done: an independent re-measurement by a fresh reviewer, a geometry imported from a
build123d STEP (here gmsh's own box), and face selection by signature (analysis_toolbox note,
"Binding loads and supports to faces"). Those come with the carriage model in my-robot.

Rerun: `env -u VIRTUAL_ENV uv run --no-project --python 3.12 --with gmsh --with scikit-fem --with numpy --with scipy python run.py`
