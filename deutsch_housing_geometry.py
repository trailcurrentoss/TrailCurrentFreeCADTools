"""Load and position TrailCurrent termination housings for Deutsch receptacles.

One module serves every connector size. Each housing is a BREP bundled with
the plugin in the same canonical orientation the DTM2P/DTM4P/DTM12P tools use:
  - Centred at the origin in the XY plane
  - Physical bottom at Z=0, housing extends along +Z
  - X is the width axis, Y is the depth axis (the connector axis)
  - Front opening (mating end) at Y_min, wire end at Y_max

The front offset is read from each shape's bounding box, so adding a size is
a matter of dropping its BREP into resources/shapes/ and listing it in
VARIANTS. Source models live in TrailCurrentCommonCADFiles/ and are traced
from the official TE/Deutsch STEP files.

Placement maths (face frame, edge frame, fuse) is identical to the
single-size modules so a housing lands in the same place whichever command
created it.
"""

import os
import math

import FreeCAD
from FreeCAD import Vector, Matrix
import Part

_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_shape_dir = os.path.join(_plugin_dir, "resources", "shapes")

# key -> (menu label, Deutsch receptacle, BREP file)
VARIANTS = {
    "DTM2P": ("DTM 2-position", "DTM04-2P", "TrailCurrentTerminationHousing2P.brep"),
    "DTM3P": ("DTM 3-position", "DTM04-3P", "TrailCurrentTerminationHousingDTM3P.brep"),
    "DTM4P": ("DTM 4-position", "DTM04-4P", "TrailCurrentTerminationHousing.brep"),
    "DTM6P": ("DTM 6-position", "DTM04-6P", "TrailCurrentTerminationHousingDTM6P.brep"),
    "DTM8P": ("DTM 8-position", "DTM04-08PA", "TrailCurrentTerminationHousingDTM8P.brep"),
    "DTM12P": ("DTM 12-position", "DTM04-12PA", "TrailCurrentTerminationHousing12P.brep"),
}

DEFAULT_VARIANT = "DTM4P"

_shape_cache = {}


def variant_keys():
    """Return the variant keys in menu order."""
    return list(VARIANTS.keys())


def variant_label(key):
    label, part, _ = VARIANTS[key]
    return "{} ({})".format(label, part)


def load_housing_shape(key):
    """Return a fresh copy of the canonical housing shape for *key*."""
    if key not in VARIANTS:
        raise ValueError("Unknown housing variant: {}".format(key))
    if key not in _shape_cache:
        shape = Part.Shape()
        shape.importBrep(os.path.join(_shape_dir, VARIANTS[key][2]))
        _shape_cache[key] = shape
    return _shape_cache[key].copy()


def front_y(key):
    """Y coordinate of the housing front (opening) in the canonical frame."""
    return load_housing_shape(key).BoundBox.YMin


def compute_face_frame(face):
    """Compute a local coordinate frame for a planar face.

    U and V axes are aligned with the model's world axes (projected onto
    the face plane) so that X/Y offsets correspond to the global coordinate
    system rather than arbitrary directions.

    Returns (center, u_axis, v_axis, normal).
    """
    u_min, u_max, v_min, v_max = face.ParameterRange
    # normalAt already accounts for face orientation: it is the outward normal
    normal = face.normalAt(
        (u_min + u_max) / 2.0, (v_min + v_max) / 2.0
    )
    center = face.CenterOfMass

    # Project world X onto the face plane to get U.  If world X is
    # (nearly) parallel to the normal, fall back to world Y.
    world_x = Vector(1, 0, 0)
    world_y = Vector(0, 1, 0)

    proj_x = world_x - normal * normal.dot(world_x)
    if proj_x.Length > 1e-6:
        u_axis = proj_x
        u_axis.normalize()
    else:
        proj_y = world_y - normal * normal.dot(world_y)
        u_axis = proj_y
        u_axis.normalize()

    v_axis = normal.cross(u_axis)
    v_axis.normalize()

    return center, u_axis, v_axis, normal


def compute_edge_frame(face, edge, key=None):
    """Compute a frame where the housing front aligns to *edge*.

    The edge tangent direction becomes the housing width axis (local X).
    The perpendicular direction on the face (pointing away from the edge
    into the face) becomes the housing depth axis (local Y).

    With *key*, the anchor is shifted so that the front face of that
    housing lines up with the edge. Without it the anchor is the edge
    midpoint itself; use edge_anchor() to apply the shift later, which is
    what lets an existing feature switch sizes and stay on its edge.

    Returns (anchor, u_axis, v_axis, normal).
    """
    u_min, u_max, v_min, v_max = face.ParameterRange
    normal = face.normalAt(
        (u_min + u_max) / 2.0, (v_min + v_max) / 2.0
    )

    e_first, e_last = edge.ParameterRange
    tangent = edge.tangentAt((e_first + e_last) / 2.0)
    tangent.normalize()

    u_axis = tangent
    v_axis = normal.cross(u_axis)
    v_axis.normalize()

    # v_axis must point from the edge toward the face centre
    anchor = edge.CenterOfMass
    to_center = face.CenterOfMass - anchor
    if to_center.dot(v_axis) < 0:
        v_axis = v_axis * -1.0
        u_axis = u_axis * -1.0  # keep right-handed

    if key is not None:
        anchor = edge_anchor(anchor, v_axis, key)

    return anchor, u_axis, v_axis, normal


def edge_anchor(edge_point, v_axis, key):
    """Shift an edge midpoint so the front of housing *key* sits on the edge."""
    return Vector(edge_point) + Vector(v_axis) * (-front_y(key))


def housing_matrix(anchor, u_axis, v_axis, normal,
                   x_offset=0.0, y_offset=0.0, rotation=0.0):
    """Matrix taking the canonical housing frame onto the given face frame."""
    u_axis = Vector(u_axis)
    v_axis = Vector(v_axis)
    normal = Vector(normal)

    if rotation:
        rad = math.radians(rotation)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        u_rot = u_axis * cos_r + v_axis * sin_r
        v_rot = -u_axis * sin_r + v_axis * cos_r
        u_axis, v_axis = u_rot, v_rot

    placement = Vector(anchor) + u_axis * x_offset + v_axis * y_offset

    mat = Matrix()
    mat.A11 = u_axis.x;  mat.A12 = v_axis.x;  mat.A13 = normal.x;  mat.A14 = placement.x
    mat.A21 = u_axis.y;  mat.A22 = v_axis.y;  mat.A23 = normal.y;  mat.A24 = placement.y
    mat.A31 = u_axis.z;  mat.A32 = v_axis.z;  mat.A33 = normal.z;  mat.A34 = placement.z
    return mat


def placed_housing(key, anchor, u_axis, v_axis, normal,
                   x_offset=0.0, y_offset=0.0, rotation=0.0):
    """Return the housing for *key* positioned on the frame (not fused)."""
    housing = load_housing_shape(key)
    housing.transformShape(housing_matrix(
        anchor, u_axis, v_axis, normal, x_offset, y_offset, rotation))
    return housing


def place_housing_from_frame(body_shape, key, anchor, u_axis, v_axis, normal,
                             x_offset=0.0, y_offset=0.0, rotation=0.0):
    """Place the housing for *key* on a pre-computed frame and fuse it.

    The frame is the source of truth — fully decoupled from any face/edge
    topology, which makes placement robust to upstream geometry changes.
    """
    housing = placed_housing(key, anchor, u_axis, v_axis, normal,
                             x_offset, y_offset, rotation)
    result = body_shape.fuse(housing)
    return result.removeSplitter()


def place_housing(body_shape, face, key, x_offset=0.0, y_offset=0.0,
                  rotation=0.0, edge=None):
    """Convenience wrapper: derive the frame from a face (and optional edge)."""
    if edge is not None:
        anchor, u_axis, v_axis, normal = compute_edge_frame(face, edge, key)
    else:
        anchor, u_axis, v_axis, normal = compute_face_frame(face)
    return place_housing_from_frame(
        body_shape, key, anchor, u_axis, v_axis, normal,
        x_offset=x_offset, y_offset=y_offset, rotation=rotation,
    )
