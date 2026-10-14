"""Spike S9: does one structural path match a closed form?

gmsh (second-order tetrahedra) -> CalculiX ccx 2.23 (pixi exec -s calculix ccx) for statics and the
first natural frequency, with scikit-fem on the same mesh as an independent solver. Case: a solid
aluminium cantilever, clamped at x = 0, a shear load F at x = L spread over the end face.

Pass (plan, S9): within 5 % of the closed form. Also asserted: reactions balance the load, strain
energy equals F * deflection / 2, and the answer converges over three meshes.

Run from the my-robot project (it has gmsh and scikit-fem):
    env -u VIRTUAL_ENV uv run --project ~/work/privat/my-robot python run.py
"""

import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

L, B, H = 200.0, 20.0, 10.0  # mm: length x, width y, height z
E, NU, RHO = 70000.0, 0.33, 2.70e-9  # MPa, -, t/mm^3 (consistent with N, mm, s)
F = 100.0  # N, along -z
G = E / (2 * (1 + NU))
I = B * H**3 / 12
A = B * H


def closed_form():
    bending = F * L**3 / (3 * E * I)
    shear = F * L / (5 / 6 * G * A)  # Timoshenko, rectangular section
    frequency = 1.8751**2 / (2 * math.pi) * math.sqrt(E * I / (RHO * A * L**4))
    return {"deflection_bending": bending, "deflection": bending + shear, "frequency": frequency}


def mesh(size):
    """Tet10 mesh of the bar: node coordinates (n, 3), elements (m, 10) in Abaqus/CalculiX order,
    and the node indices on the clamped and loaded faces with the loaded face's 6-node triangles."""
    import gmsh

    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.model.add("bar")
    gmsh.model.occ.addBox(0, 0, 0, L, B, H)
    gmsh.model.occ.synchronize()
    gmsh.option.setNumber("Mesh.MeshSizeMax", size)
    gmsh.option.setNumber("Mesh.ElementOrder", 2)
    gmsh.option.setNumber("Mesh.SecondOrderLinear", 1)  # straight edges: the box is flat
    gmsh.model.mesh.generate(3)
    tags, coords, _ = gmsh.model.mesh.getNodes()
    index = {int(t): i for i, t in enumerate(tags)}
    nodes = coords.reshape(-1, 3)
    types, _, node_tags = gmsh.model.mesh.getElements(3)
    (tets,) = [np.array(n, dtype=np.int64).reshape(-1, 10) for t, n in zip(types, node_tags) if t == 11]
    tets = np.vectorize(index.get)(tets)
    # gmsh's tet10 lists the last two edge nodes as (2,3), (1,3); CalculiX wants (1,3), (2,3).
    tets = tets[:, [0, 1, 2, 3, 4, 5, 6, 7, 9, 8]]
    edges = [(0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3)]
    for k, (a, b) in enumerate(edges):
        assert np.allclose(nodes[tets[:, 4 + k]], (nodes[tets[:, a]] + nodes[tets[:, b]]) / 2), f"edge node {4 + k}"
    faces = {}
    for name, x in (("clamp", 0.0), ("load", L)):
        (surface,) = [s for _, s in gmsh.model.getEntities(2) if abs(gmsh.model.occ.getCenterOfMass(2, s)[0] - x) < 1e-9]
        types, _, tags = gmsh.model.mesh.getElements(2, surface)
        (tri,) = [np.array(n, dtype=np.int64).reshape(-1, 6) for t, n in zip(types, tags) if t == 9]
        faces[name] = np.vectorize(index.get)(tri)
    gmsh.finalize()
    return nodes, tets, faces


def face_loads(nodes, triangles, total):
    """Nodal forces of a uniform traction over 6-node triangles: corners get nothing, each midside
    node a third of its triangle's share."""
    loads = np.zeros(len(nodes))
    areas = 0.5 * np.linalg.norm(np.cross(nodes[triangles[:, 1]] - nodes[triangles[:, 0]], nodes[triangles[:, 2]] - nodes[triangles[:, 0]]), axis=1)
    for tri, area in zip(triangles, areas):
        loads[tri[3:]] += area / 3
    return loads * total / areas.sum()


def calculix(nodes, tets, faces, workdir):
    clamp = np.unique(faces["clamp"])
    loads = face_loads(nodes, faces["load"], -F)
    lines = ["*HEADING", "S9 cantilever", "*NODE, NSET=NALL"]
    lines += [f"{i + 1}, {x:.9g}, {y:.9g}, {z:.9g}" for i, (x, y, z) in enumerate(nodes)]
    lines.append("*ELEMENT, TYPE=C3D10, ELSET=EALL")
    lines += [f"{e + 1}, " + ", ".join(str(n + 1) for n in tet) for e, tet in enumerate(tets)]
    lines.append("*NSET, NSET=CLAMP")
    lines += [", ".join(str(n + 1) for n in clamp[i : i + 12]) for i in range(0, len(clamp), 12)]
    tip = np.unique(faces["load"])
    lines.append("*NSET, NSET=TIP")
    lines += [", ".join(str(n + 1) for n in tip[i : i + 12]) for i in range(0, len(tip), 12)]
    lines += ["*MATERIAL, NAME=ALU", "*ELASTIC", f"{E}, {NU}", "*DENSITY", f"{RHO}", "*SOLID SECTION, ELSET=EALL, MATERIAL=ALU", "*BOUNDARY", "CLAMP, 1, 3"]
    lines += ["*STEP", "*STATIC", "*CLOAD"]
    lines += [f"{n + 1}, 3, {f:.9g}" for n, f in enumerate(loads) if f]
    lines += ["*NODE PRINT, NSET=TIP", "U", "*NODE PRINT, NSET=CLAMP, TOTALS=ONLY", "RF", "*EL PRINT, ELSET=EALL, TOTALS=ONLY", "ELSE", "*END STEP"]
    lines += ["*STEP", "*FREQUENCY", "3", "*END STEP"]
    (workdir / "bar.inp").write_text("\n".join(lines) + "\n")
    done = subprocess.run(["pixi", "exec", "-s", "calculix", "ccx", "-i", "bar"], cwd=workdir, capture_output=True, text=True)
    if done.returncode or "error" in done.stdout.lower():
        raise RuntimeError(done.stdout[-2000:] + done.stderr[-2000:])
    return parse_dat((workdir / "bar.dat").read_text())


def parse_dat(text):
    """Tip displacements, total reaction and strain energy (static step) and eigenfrequencies (the
    frequency step's eigenvalue table) from ccx's .dat."""
    number = r"[-+]?\d+\.?\d*(?:[Ee][-+]?\d+)?"
    static, frequency = text.split("S T E P       2")

    def rows_after(block, heading):
        lines = block.split(heading, 1)[1].splitlines()[1:]
        rows = []
        for line in lines:
            if not line.strip():
                if rows:
                    break
                continue
            values = re.findall(number, line)
            if len(values) != len(line.split()):
                break
            rows.append([float(v) for v in values])
        return rows

    eigen = frequency.split("E I G E N V A L U E   O U T P U T", 1)[1].split("P A R T I C I P A T I O N", 1)[0]
    return {
        "tip_uz": float(np.mean([row[3] for row in rows_after(static, "displacements (vx,vy,vz) for set TIP")])),
        "reaction": rows_after(static, "total force (fx,fy,fz) for set CLAMP")[0],
        "energy": rows_after(static, "total internal energy for set EALL")[0][0],
        "frequencies": [float(v[3]) for v in (re.findall(number, line) for line in eigen.splitlines()) if len(v) == 5],
    }


def scikit_fem(nodes, tets, faces):
    """Same mesh, P2 tetrahedra, linear elasticity: mean tip displacement along z."""
    from skfem import Basis, ElementTetP2, ElementVector, FacetBasis, LinearForm, MeshTet, asm, condense, solve
    from skfem.models.elasticity import lame_parameters, linear_elasticity

    # scikit-fem builds its own P2 nodes from the vertices; the geometry is straight-edged, so they
    # coincide with gmsh's.
    vertices = np.unique(tets[:, :4])
    renumber = -np.ones(len(nodes), dtype=int)
    renumber[vertices] = np.arange(len(vertices))
    mesh = MeshTet(np.ascontiguousarray(nodes[vertices].T), np.ascontiguousarray(renumber[tets[:, :4]].T))
    element = ElementVector(ElementTetP2())
    basis = Basis(mesh, element, intorder=4)
    stiffness = asm(linear_elasticity(*lame_parameters(E, NU)), basis)
    end = FacetBasis(mesh, element, facets=mesh.facets_satisfying(lambda x: np.isclose(x[0], L)), intorder=4)

    @LinearForm
    def traction(v, w):
        return -F / A * v[2]

    load = asm(traction, end)
    fixed = basis.get_dofs(lambda x: np.isclose(x[0], 0.0)).all()
    u = solve(*condense(stiffness, load, D=fixed))
    tip = basis.get_dofs(lambda x: np.isclose(x[0], L)).nodal["u^3"]
    return {"tip_uz": float(np.mean(u[tip])), "energy": float(0.5 * u @ stiffness @ u)}


def main():
    expected = closed_form()
    rows = []
    for size in (5.0, 3.5, 2.5):
        nodes, tets, faces = mesh(size)
        with tempfile.TemporaryDirectory() as tmp:
            ccx = calculix(nodes, tets, faces, Path(tmp))
        skfem = scikit_fem(nodes, tets, faces)
        rows.append({"size": size, "elements": len(tets), "nodes": len(nodes), "ccx": ccx, "skfem": skfem})
        print(f"size {size}: {len(tets)} tet10, ccx tip {ccx['tip_uz']:.5f} mm, skfem tip {skfem['tip_uz']:.5f} mm, f1 {ccx['frequencies'][0]:.1f} Hz", flush=True)
    finest = rows[-1]
    deflection = -finest["ccx"]["tip_uz"]
    checks = {
        "deflection vs Timoshenko": deflection / expected["deflection"] - 1,
        "deflection vs Euler-Bernoulli": deflection / expected["deflection_bending"] - 1,
        "frequency vs Euler-Bernoulli": finest["ccx"]["frequencies"][0] / expected["frequency"] - 1,
        "skfem vs ccx deflection": finest["skfem"]["tip_uz"] / finest["ccx"]["tip_uz"] - 1,
        "reaction z + F": finest["ccx"]["reaction"][2] - F,
        "energy vs F*d/2": finest["ccx"]["energy"] / (F * deflection / 2) - 1,
        "change, last two meshes": rows[-1]["ccx"]["tip_uz"] / rows[-2]["ccx"]["tip_uz"] - 1,
    }
    print(json.dumps({"closed_form": expected, "checks": checks}, indent=2))
    (Path(__file__).parent / "results.json").write_text(json.dumps({"closed_form": expected, "rows": rows, "checks": checks}, indent=2) + "\n")
    ok = abs(checks["deflection vs Timoshenko"]) < 0.05 and abs(checks["frequency vs Euler-Bernoulli"]) < 0.05 and abs(checks["skfem vs ccx deflection"]) < 0.02
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
