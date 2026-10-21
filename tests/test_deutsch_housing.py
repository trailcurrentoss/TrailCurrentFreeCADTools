"""Placement checks for deutsch_housing_geometry, run without the GUI.

    freecadcmd tests/test_deutsch_housing.py

Every housing size is placed on faces of differently oriented bodies, with
and without edge alignment, offsets and rotation. Each placement must fuse
to one valid solid, sit flat on the chosen face, extend outward along the
face normal and, when edge-aligned, put its front exactly on the edge.
"""

import math
import os
import sys

import FreeCAD
from FreeCAD import Vector
import Part

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import deutsch_housing_geometry as g  # noqa: E402

TOL = 1e-4
failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def bodies():
    """Test bodies: plain, rotated off-axis, and one with a pocket."""
    plain = Part.makeBox(120, 90, 30)
    rotated = Part.makeBox(120, 90, 30)
    rotated.rotate(Vector(0, 0, 0), Vector(1, 1, 0.3), 37)
    rotated.translate(Vector(15, -40, 7))
    pocketed = Part.makeBox(120, 90, 30).cut(
        Part.makeCylinder(6, 40, Vector(100, 70, -5)))
    return {"plain": plain, "rotated": rotated, "pocketed": pocketed}


def outward_face(shape, direction):
    """The face whose outward normal points most along *direction*."""
    best = None
    for f in shape.Faces:
        if not isinstance(f.Surface, Part.Plane):
            continue
        n = f.normalAt(*f.Surface.parameter(f.CenterOfMass))   # outward
        score = n.dot(direction)
        if best is None or score > best[0]:
            best = (score, f, n)
    return best[1], best[2]


def longest_edge(face):
    return max(face.Edges, key=lambda e: e.Length)


def run_case(key, body_name, body, direction, use_edge, x_off, y_off, rot):
    label = "{} on {} {} edge={} off=({},{}) rot={}".format(
        key, body_name, tuple(round(c, 2) for c in direction), use_edge,
        x_off, y_off, rot)
    face, normal = outward_face(body, direction)
    edge = longest_edge(face) if use_edge else None
    if edge is not None:
        anchor, u, v, n = g.compute_edge_frame(face, edge, key)
    else:
        anchor, u, v, n = g.compute_face_frame(face)

    housing = g.placed_housing(key, anchor, u, v, n, x_off, y_off, rot)
    fused = g.place_housing_from_frame(body, key, anchor, u, v, n,
                                       x_off, y_off, rot)

    check(fused.isValid(), label + ": fused shape invalid")
    check(len(fused.Solids) == 1, label + ": {} solids".format(len(fused.Solids)))
    # sits on the face: every housing vertex is on or outside the face plane
    plane_pt = face.CenterOfMass
    heights = [(p.Point - plane_pt).dot(normal) for p in housing.Vertexes]
    check(abs(min(heights)) < TOL,
          label + ": housing bottom is {:.4f} off the face".format(min(heights)))
    canon = g.load_housing_shape(key)
    check(abs(max(heights) - canon.BoundBox.ZMax) < TOL,
          label + ": housing does not extend outward along the normal")
    # housing sits outside the body (no material lost from the body)
    added = fused.Volume - body.Volume
    check(abs(added - canon.Volume) < 1e-3 * canon.Volume + 0.5,
          label + ": added {:.1f} mm3, housing is {:.1f} mm3".format(
              added, canon.Volume))
    if edge is not None and not (x_off or y_off or rot):
        # front plane of the housing passes through the edge
        e0, e1 = edge.ParameterRange
        mid = edge.valueAt((e0 + e1) / 2)
        front = min((p.Point - mid).dot(v) for p in housing.Vertexes)
        check(abs(front) < TOL,
              label + ": front is {:.4f} from the edge".format(front))
        # and the housing runs from the edge into the face, not off it
        com = housing.Solids[0].CenterOfMass
        foot = com - normal * (com - plane_pt).dot(normal)
        check(face.distToShape(Part.Vertex(foot))[0] < TOL,
              label + ": housing hangs off the face")


def run_switch(body):
    """Changing size on an edge-aligned frame keeps the front on the edge."""
    face, normal = outward_face(body, Vector(0, 0, 1))
    edge = longest_edge(face)
    anchor, u, v, n = g.compute_edge_frame(face, edge)       # unshifted
    e0, e1 = edge.ParameterRange
    mid = edge.valueAt((e0 + e1) / 2)
    for key in g.variant_keys():
        a = g.edge_anchor(anchor, v, key)
        h = g.placed_housing(key, a, u, v, n)
        front = min((p.Point - mid).dot(v) for p in h.Vertexes)
        check(abs(front) < TOL,
              "switch to {}: front {:.4f} from the edge".format(key, front))


def run_shapes():
    for key in g.variant_keys():
        s = g.load_housing_shape(key)
        b = s.BoundBox
        check(s.isValid() and len(s.Solids) == 1, key + ": shape not one valid solid")
        check(abs(b.ZMin) < TOL, key + ": bottom not at Z=0")
        check(abs(b.XMin + b.XMax) < 1e-3 and abs(b.YMin + b.YMax) < 1e-3,
              key + ": not centred in XY")
        check(abs(g.front_y(key) - b.YMin) < TOL, key + ": front_y mismatch")


def main():
    run_shapes()
    cases = 0
    directions = [Vector(0, 0, 1), Vector(1, 0, 0), Vector(0, -1, 0),
                  Vector(0, 0, -1)]
    for key in g.variant_keys():
        for body_name, body in bodies().items():
            for d in directions:
                for use_edge in (False, True):
                    run_case(key, body_name, body, d, use_edge, 0, 0, 0)
                    cases += 1
            run_case(key, body_name, body, Vector(0, 0, 1), True, 7.5, 4.0, 30)
            run_case(key, body_name, body, Vector(0, 0, 1), False, -12.0, 9.0, -90)
            cases += 2
    for body in bodies().values():
        run_switch(body)
    print("deutsch_housing: {} placements over {} sizes, {} failures".format(
        cases, len(g.variant_keys()), len(failures)))
    for f in failures:
        print("FAIL", f)
    return 1 if failures else 0


rc = main()
if rc:
    raise SystemExit(rc)
