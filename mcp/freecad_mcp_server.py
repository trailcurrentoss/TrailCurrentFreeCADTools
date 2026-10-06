#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP server for FreeCAD integration.

Connects to the XML-RPC server running inside FreeCAD and exposes
FreeCAD operations as MCP tools for use by Claude Code and other
MCP-compatible AI assistants.

Usage
-----
  # With uv (recommended — zero-install):
  uv run --with mcp python3 freecad_mcp_server.py

  # Or if mcp is already installed:
  python3 freecad_mcp_server.py

  # Add to Claude Code:
  claude mcp add freecad -- uv run --with mcp python3 /path/to/freecad_mcp_server.py
"""

# -- Path fix: prevent our parent addon's mcp/ directory (this very
# directory) from shadowing the 'mcp' pip package. Python prepends the
# script's directory to sys.path, and the parent also ends up there when
# the CWD is the addon root.
import os as _os
import sys as _sys

_this_dir = _os.path.dirname(_os.path.abspath(__file__))
_addon_dir = _os.path.dirname(_this_dir)
for _p in ("", _this_dir, _addon_dir):
    while _p in _sys.path:
        _sys.path.remove(_p)
del _this_dir, _addon_dir, _p
# -- End path fix

import json
import os
import textwrap
import xmlrpc.client

# The SDK renamed FastMCP to MCPServer in mcp 2.x. `uv run --with mcp` resolves
# to the newest release, so pinning the import to either name breaks the server
# outright the day the environment moves -- it stops starting, and the tools
# simply never appear rather than failing visibly. Accept both.
try:
    from mcp.server.fastmcp import FastMCP as _MCPServer  # mcp 1.x
except ImportError:  # pragma: no cover - depends on installed SDK
    from mcp.server.mcpserver import MCPServer as _MCPServer  # mcp 2.x

# ── Configuration ──────────────────────────────────────────────────

RPC_HOST = os.environ.get("FREECAD_RPC_HOST", "127.0.0.1")
RPC_PORT = int(os.environ.get("FREECAD_RPC_PORT", "12785"))
RPC_URL = "http://{}:{}/RPC2".format(RPC_HOST, RPC_PORT)

mcp = _MCPServer(
    name="FreeCAD",
    instructions=textwrap.dedent("""\
        MCP server for interacting with a running FreeCAD instance.
        FreeCAD must be open with the MCP RPC server started
        (TrailCurrent FreeCAD Tools workbench → MCP Server button).

        The most powerful tool is execute_code, which runs arbitrary
        Python inside FreeCAD's interpreter. The convenience tools
        cover common operations without needing to write Python.

        In FreeCAD's Python environment, these modules are pre-imported:
          FreeCAD / App, FreeCADGui / Gui, Part, Mesh, Draft,
          Sketcher, PartDesign, Import, BOPTools

        Key patterns:
          doc = App.ActiveDocument
          obj = doc.getObject("Box")
          obj.Height = 20
          doc.recompute()
    """),
)


def _rpc():
    """Get an XML-RPC proxy to FreeCAD."""
    return xmlrpc.client.ServerProxy(RPC_URL, allow_none=True)


def _format(result: dict) -> str:
    """Render an RPC result dict as text."""
    parts = []
    if result.get("stdout"):
        parts.append(result["stdout"].rstrip())
    if result.get("result"):
        parts.append(result["result"])
    if result.get("stderr"):
        parts.append("[stderr]\n" + result["stderr"].rstrip())
    if result.get("error"):
        parts.append("[error]\n" + result["error"].rstrip())
    if result.get("dialogs"):
        # A prompt was answered automatically. The operation may have taken a
        # default branch it would not have taken interactively, so say so
        # loudly rather than letting it look like a clean success.
        parts.append("[dialogs auto-dismissed -- the answer below was assumed]\n  "
                     + "\n  ".join(result["dialogs"]))
    return "\n".join(parts) if parts else "(no output)"


def _call(code: str) -> str:
    """Execute code in FreeCAD and return formatted output."""
    try:
        proxy = _rpc()
        result = proxy.execute(code)
    except ConnectionRefusedError:
        return (
            "ERROR: Cannot connect to FreeCAD RPC server at {}. "
            "Make sure FreeCAD is running and the MCP RPC server is started "
            "(TrailCurrent FreeCAD Tools workbench > MCP Server toolbar button)."
        ).format(RPC_URL)
    except Exception as e:
        return "ERROR: RPC call failed: {}".format(e)

    return _format(result)


# ── Tools ──────────────────────────────────────────────────────────


@mcp.tool()
def execute_code(code: str) -> str:
    """Execute arbitrary Python code inside FreeCAD's interpreter.

    The code runs on FreeCAD's main thread with full access to all
    FreeCAD modules (App, Gui, Part, Mesh, Draft, etc.).

    Use print() to produce output. If the code is a single expression,
    its repr() is returned automatically.

    Examples:
      # List all objects in the active document
      for obj in App.ActiveDocument.Objects:
          print(obj.Name, obj.TypeId)

      # Create a box
      doc = App.ActiveDocument or App.newDocument("Unnamed")
      box = doc.addObject("Part::Box", "MyBox")
      box.Length = 50
      box.Width = 30
      box.Height = 10
      doc.recompute()

      # Boolean cut
      cut = doc.addObject("Part::Cut", "MyCut")
      cut.Base = doc.getObject("Box")
      cut.Tool = doc.getObject("Cylinder")
      doc.recompute()
    """
    return _call(code)


@mcp.tool()
def list_documents() -> str:
    """List all open FreeCAD documents with their names and file paths."""
    return _call(textwrap.dedent("""\
        import json
        docs = []
        for name, doc in App.listDocuments().items():
            docs.append({
                "name": doc.Name,
                "label": doc.Label,
                "filename": doc.FileName or "(unsaved)",
                "object_count": len(doc.Objects),
            })
        print(json.dumps(docs, indent=2))
    """))


@mcp.tool()
def get_document_info(document: str = "") -> str:
    """Get detailed info about a document including all its objects.

    Args:
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import json
        doc_name = {doc!r}
        doc = App.getDocument(doc_name) if doc_name else App.ActiveDocument
        if doc is None:
            print("ERROR: No active document")
        else:
            objects = []
            for obj in doc.Objects:
                info = {{
                    "name": obj.Name,
                    "label": obj.Label,
                    "type": obj.TypeId,
                    "visibility": obj.ViewObject.Visibility if hasattr(obj, "ViewObject") and obj.ViewObject else None,
                }}
                if hasattr(obj, "Shape") and obj.Shape and not obj.Shape.isNull():
                    s = obj.Shape
                    shape_info = {{
                        "type": s.ShapeType,
                        "volume": round(s.Volume, 4),
                        "area": round(s.Area, 4),
                    }}
                    try:
                        bb = s.BoundBox
                        shape_info["bounds"] = {{
                            "min": [round(bb.XMin, 4), round(bb.YMin, 4), round(bb.ZMin, 4)],
                            "max": [round(bb.XMax, 4), round(bb.YMax, 4), round(bb.ZMax, 4)],
                        }}
                    except Exception:
                        pass
                    info["shape"] = shape_info
                objects.append(info)
            result = {{
                "name": doc.Name,
                "label": doc.Label,
                "filename": doc.FileName or "(unsaved)",
                "objects": objects,
            }}
            print(json.dumps(result, indent=2, default=str))
    """).format(doc=document))


@mcp.tool()
def get_object_properties(object_name: str, document: str = "") -> str:
    """Get all properties of a FreeCAD object.

    Args:
        object_name: The internal Name of the object.
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        import json
        doc_name = {doc!r}
        doc = App.getDocument(doc_name) if doc_name else App.ActiveDocument
        if doc is None:
            print("ERROR: No active document")
        else:
            obj = doc.getObject({name!r})
            if obj is None:
                print("ERROR: Object {name!r} not found")
            else:
                props = {{}}
                for p in obj.PropertiesList:
                    try:
                        val = getattr(obj, p)
                        # Make values JSON-serializable
                        if hasattr(val, "x") and hasattr(val, "y") and hasattr(val, "z"):
                            val = {{"x": val.x, "y": val.y, "z": val.z}}
                        elif hasattr(val, "Base") and hasattr(val, "Rotation"):
                            val = {{
                                "Base": {{"x": val.Base.x, "y": val.Base.y, "z": val.Base.z}},
                                "Rotation": list(val.Rotation.Q),
                            }}
                        elif isinstance(val, (list, tuple)):
                            val = [str(v) for v in val]
                        elif not isinstance(val, (int, float, bool, str, type(None))):
                            val = str(val)
                        props[p] = val
                    except Exception as e:
                        props[p] = "<error: {{}}>".format(e)
                info = {{
                    "name": obj.Name,
                    "label": obj.Label,
                    "type": obj.TypeId,
                    "properties": props,
                }}
                print(json.dumps(info, indent=2, default=str))
    """).format(name=object_name, doc=document))


@mcp.tool()
def set_object_property(
    object_name: str,
    property_name: str,
    value: str,
    document: str = "",
) -> str:
    """Set a property on a FreeCAD object.

    The value is evaluated as a Python expression in FreeCAD's context,
    so you can use FreeCAD types like App.Vector(1, 2, 3).

    Args:
        object_name: The internal Name of the object.
        property_name: The property to set (e.g. "Height", "Label").
        value: Python expression for the value (e.g. "42.0", "'MyLabel'",
               "App.Vector(0, 0, 1)").
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        obj = doc.getObject({name!r})
        if obj is None:
            print("ERROR: Object {name!r} not found")
        else:
            setattr(obj, {prop!r}, {value})
            doc.recompute()
            new_val = getattr(obj, {prop!r})
            print("Set {name}.{prop} = " + repr(new_val))
    """).format(
        name=object_name,
        prop=property_name,
        value=value,
        doc=document,
    ))


@mcp.tool()
def set_expression(
    object_name: str,
    property_name: str,
    expression: str,
    document: str = "",
) -> str:
    """Bind an object property to a FreeCAD expression so it recomputes itself.

    PREFER THIS OVER WRITING A LITERAL VALUE. A property set to a number is dead
    geometry -- only whoever ran the script can change it. A property bound to an
    expression is driven by the model, and the user can edit it in the GUI.

    Typical use is driving dimensions from a spreadsheet alias:
        set_expression("pad_Rib01", "Length", "Design.thickness")
        set_expression("Pocket", "Length", "Design.wall * 2")

    Args:
        object_name: Object whose property to bind (e.g. "pad_Rib01").
        property_name: Property to drive (e.g. "Length", "Radius", "Angle").
        expression: FreeCAD expression, e.g. "Design.thickness" or "Box.Height / 2".
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        obj = doc.getObject({name!r})
        if obj is None:
            print("ERROR: no object named", {name!r})
        else:
            obj.setExpression({prop!r}, {expr!r})
            doc.recompute()
            print("bound", {name!r} + "." + {prop!r}, "->", {expr!r})
            print("value now:", getattr(obj, {prop!r}, "?"))
    """).format(name=object_name, prop=property_name, expr=expression, doc=document))


@mcp.tool()
def clear_expression(object_name: str, property_name: str, document: str = "") -> str:
    """Remove an expression binding, leaving the property at its current value.

    Args:
        object_name: Object to unbind.
        property_name: Property to release.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        obj = doc.getObject({name!r})
        obj.setExpression({prop!r}, None)
        doc.recompute()
        print("cleared expression on", {name!r} + "." + {prop!r})
    """).format(name=object_name, prop=property_name, doc=document))


@mcp.tool()
def spreadsheet_set(
    sheet_name: str,
    cell: str,
    value: str,
    alias: str = "",
    document: str = "",
) -> str:
    """Write a cell in a Spreadsheet, optionally giving it an alias.

    A Spreadsheet of aliased inputs is FreeCAD's native place for design
    parameters. Put the inputs there and bind geometry to them with
    set_expression, instead of baking numbers into the objects.

    Values carry units: "19.05 mm", "3 in". Formulas start with "=" and may
    reference other aliases: "=rail_wing + extra_roll".

    Args:
        sheet_name: Spreadsheet object name. Created if it does not exist.
        cell: Cell address, e.g. "B4".
        value: Cell content -- a quantity, a number, text, or an "=" formula.
        alias: Optional alias for the cell, making it referenceable by name.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        sh = doc.getObject({sheet!r})
        if sh is None:
            sh = doc.addObject("Spreadsheet::Sheet", {sheet!r})
            print("created spreadsheet", {sheet!r})
        sh.set({cell!r}, {value!r})
        if {alias!r}:
            sh.setAlias({cell!r}, {alias!r})
        doc.recompute()
        print({cell!r}, "=", {value!r}, ("alias " + {alias!r}) if {alias!r} else "")
        if {alias!r}:
            print("resolves to:", sh.get({alias!r}))
    """).format(sheet=sheet_name, cell=cell, value=value, alias=alias, doc=document))


@mcp.tool()
def spreadsheet_get(sheet_name: str, cell_or_alias: str = "", document: str = "") -> str:
    """Read a spreadsheet cell or alias, or list every alias if none is given.

    Args:
        sheet_name: Spreadsheet object name.
        cell_or_alias: Cell address or alias. Empty lists all aliases.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        sh = doc.getObject({sheet!r})
        if sh is None:
            print("ERROR: no spreadsheet named", {sheet!r})
        elif {key!r}:
            print({key!r}, "=", sh.get({key!r}))
        else:
            props = [p for p in sh.PropertiesList if sh.getTypeIdOfProperty(p) != "App::PropertyString"]
            found = 0
            for p in sh.PropertiesList:
                try:
                    v = sh.get(p)
                except Exception:
                    continue
                print("  {{:<14s}} {{}}".format(p, v))
                found += 1
            if not found:
                print("(no aliases defined)")
    """).format(sheet=sheet_name, key=cell_or_alias, doc=document))


@mcp.tool()
def create_sketch(
    name: str,
    plane: str = "XY",
    offset: float = 0.0,
    body: str = "",
    document: str = "",
) -> str:
    """Create a Sketch attached to an origin plane, optionally inside a Body.

    Attach to origin planes with an offset. Never attach to a face name like
    "Face6" -- face numbering is unstable and re-orders when the model changes.

    Args:
        name: Name for the new sketch.
        plane: "XY", "XZ" or "YZ".
        offset: Distance along the plane's normal, in mm. Note the normals are
            counterintuitive: XZ points -Y and YZ points -X, so a positive
            offset moves in the negative world direction for those two.
        body: Optional PartDesign Body to create the sketch inside.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        plane_obj = doc.getObject({plane!r} + "_Plane")
        if plane_obj is None and {body!r}:
            b = doc.getObject({body!r})
            plane_obj = b.getObject({plane!r} + "_Plane") if b else None
        sk = doc.addObject("Sketcher::SketchObject", {name!r})
        if {body!r}:
            bd = doc.getObject({body!r})
            if bd is not None:
                bd.addObject(sk)
                plane_obj = plane_obj or bd.Origin.OriginFeatures[
                    {{"XY": 3, "XZ": 4, "YZ": 5}}[{plane!r}]]
        if plane_obj is not None:
            sk.AttachmentSupport = [(plane_obj, "")]
            sk.MapMode = "FlatFace"
            if {off!r}:
                sk.AttachmentOffset = App.Placement(
                    App.Vector(0, 0, {off!r}), App.Rotation())
        doc.recompute()
        print("created sketch", sk.Name, "on", {plane!r},
              "offset", {off!r}, "mm")
        print("world placement:", sk.Placement.Base)
    """).format(name=name, plane=plane, off=offset, body=body, doc=document))


@mcp.tool()
def sketch_add_polyline(
    sketch: str,
    points: str,
    closed: bool = True,
    construction: bool = False,
    document: str = "",
) -> str:
    """Add a polyline to a sketch, coincident-constrained at every vertex.

    Args:
        sketch: Sketch object name.
        points: JSON list of [x, y] pairs in mm, e.g. "[[0,0],[10,0],[10,5]]".
        closed: Close the loop back to the first point. A Pad profile must be closed.
        construction: Mark the geometry as construction (not part of the profile).
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import json, Part, Sketcher
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        sk = doc.getObject({sk!r})
        pts = json.loads({pts!r})
        base = len(sk.Geometry)
        segs = []
        n = len(pts)
        last = n if {closed!r} else n - 1
        for i in range(last):
            a = pts[i]
            b = pts[(i + 1) % n]
            if abs(a[0] - b[0]) > 1e-9 or abs(a[1] - b[1]) > 1e-9:
                segs.append(Part.LineSegment(App.Vector(a[0], a[1], 0),
                                             App.Vector(b[0], b[1], 0)))
        sk.addGeometry(segs, {constr!r})
        made = len(segs)
        for i in range(made if {closed!r} else made - 1):
            sk.addConstraint(Sketcher.Constraint(
                "Coincident", base + i, 2, base + (i + 1) % made, 1))
        doc.recompute()
        sk.solve()
        print("added", made, "segments to", sk.Name)
        print("geometry:", len(sk.Geometry), " constraints:", len(sk.Constraints))
        print("open vertices (should be 0 for a closed profile):",
              len(sk.OpenVertices) if hasattr(sk, "OpenVertices") else "n/a")
        print("fully constrained:", sk.FullyConstrained)
    """).format(sk=sketch, pts=points, closed=closed,
                constr=construction, doc=document))


@mcp.tool()
def sketch_add_circle(
    sketch: str,
    x: float,
    y: float,
    radius: float,
    construction: bool = False,
    document: str = "",
) -> str:
    """Add a circle to a sketch. Several circles in one sketch pad or pocket together.

    Args:
        sketch: Sketch object name.
        x: Centre X in mm.
        y: Centre Y in mm.
        radius: Radius in mm.
        construction: Mark as construction geometry.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import Part
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        sk = doc.getObject({sk!r})
        sk.addGeometry(Part.Circle(App.Vector({x!r}, {y!r}, 0),
                                   App.Vector(0, 0, 1), {r!r}), {constr!r})
        doc.recompute()
        print("circle r={r!r} at ({x!r}, {y!r}); geometry now", len(sk.Geometry))
    """).format(sk=sketch, x=x, y=y, r=radius, constr=construction, doc=document))


@mcp.tool()
def sketch_info(sketch: str, document: str = "") -> str:
    """Report a sketch's geometry, constraints, closure and degrees of freedom.

    An unclosed profile is the usual reason a Pad produces nothing. Check this
    before padding rather than after.

    Args:
        sketch: Sketch object name.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        sk = doc.getObject({sk!r})
        if sk is None:
            print("ERROR: no sketch named", {sk!r})
        else:
            sk.solve()
            bb = sk.Shape.BoundBox
            print("sketch          :", sk.Name)
            print("geometry        :", len(sk.Geometry))
            print("constraints     :", len(sk.Constraints))
            print("fully constrained:", sk.FullyConstrained)
            try:
                print("open vertices   :", len(sk.OpenVertices))
            except Exception:
                pass
            print("wires / faces   :", len(sk.Shape.Wires), "/", len(sk.Shape.Faces))
            print("bbox            : {{:.4f}} x {{:.4f}} mm".format(
                bb.XLength, bb.YLength))
            closed = all(w.isClosed() for w in sk.Shape.Wires) if sk.Shape.Wires else False
            print("all wires closed:", closed,
                  "" if closed else "  <-- a Pad will fail or produce nothing")
    """).format(sk=sketch, doc=document))


@mcp.tool()
def partdesign_feature(
    body: str,
    sketch: str,
    kind: str = "pad",
    length: float = 10.0,
    reversed_dir: bool = False,
    midplane: bool = False,
    through_all: bool = False,
    document: str = "",
) -> str:
    """Add a PartDesign Pad or Pocket to a Body from a sketch.

    Use this rather than Part booleans. PartDesign builds an editable feature
    tree; Part::Cut/Fuse build a dependency graph that is fragile to modify.

    A sketch drawn on the body's outer surface almost always needs
    reversed_dir=True for a Pocket, or it cuts away from the material.

    Args:
        body: PartDesign Body name.
        sketch: Profile sketch name.
        kind: "pad" or "pocket".
        length: Depth in mm. Bind it to a spreadsheet alias afterwards with
            set_expression to keep the model parametric.
        reversed_dir: Reverse the feature direction.
        midplane: Extrude symmetrically about the sketch plane.
        through_all: Pocket only -- cut through everything.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        bd = doc.getObject({body!r})
        sk = doc.getObject({sk!r})
        if bd is None or sk is None:
            print("ERROR: missing body or sketch", {body!r}, {sk!r})
        else:
            tid = "PartDesign::Pad" if {kind!r} == "pad" else "PartDesign::Pocket"
            f = doc.addObject(tid, {kind!r} + "_" + sk.Name)
            bd.addObject(f)
            f.Profile = sk
            f.Length = {length!r}
            f.Reversed = {rev!r}
            f.Midplane = {mid!r}
            if {kind!r} == "pocket" and {through!r}:
                f.Type = 1
            doc.recompute()
            ok = bd.Shape is not None and not bd.Shape.isNull() and bd.Shape.Solids
            print("created", f.Name, "on", bd.Name)
            bad = [t for t in f.State if t in ("Invalid", "Error")]
            print("body is a solid:", bool(ok), " valid:", f.isValid(),
                  (" STATE: " + ",".join(bad)) if bad else "")
            if ok:
                b = bd.Shape.BoundBox
                print("body bbox: {{:.4f}} x {{:.4f}} x {{:.4f}} mm".format(
                    b.XLength, b.YLength, b.ZLength))
    """).format(body=body, sk=sketch, kind=kind, length=length,
                rev=reversed_dir, mid=midplane, through=through_all, doc=document))


@mcp.tool()
def partdesign_pattern(
    body: str,
    feature: str,
    kind: str = "linear",
    count: int = 2,
    length: float = 50.0,
    direction: str = "X",
    document: str = "",
) -> str:
    """Repeat a PartDesign feature with a pattern instead of a Python loop.

    A loop that creates N independent bodies is not the same thing: a pattern
    stays parametric, so changing the count or spacing updates every instance.

    Args:
        body: PartDesign Body name.
        feature: Feature to repeat (e.g. "pocket_Sketch001").
        kind: "linear", "polar" or "mirrored".
        count: Number of instances including the original.
        length: Total span for linear, or angle in degrees for polar.
        direction: "X", "Y" or "Z" -- the pattern axis, or the mirror plane normal.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        bd = doc.getObject({body!r})
        ft = doc.getObject({feat!r})
        axis_map = {{"X": "X_Axis", "Y": "Y_Axis", "Z": "Z_Axis"}}
        plane_map = {{"X": "YZ_Plane", "Y": "XZ_Plane", "Z": "XY_Plane"}}
        if bd is None or ft is None:
            print("ERROR: missing body or feature")
        else:
            if {kind!r} == "mirrored":
                p = doc.addObject("PartDesign::Mirrored", "Mirrored")
                bd.addObject(p)
                p.Originals = [ft]
                p.MirrorPlane = (bd.getObject(plane_map[{dirn!r}]), [""])
            elif {kind!r} == "polar":
                p = doc.addObject("PartDesign::PolarPattern", "PolarPattern")
                bd.addObject(p)
                p.Originals = [ft]
                p.Axis = (bd.getObject(axis_map[{dirn!r}]), [""])
                p.Angle = {length!r}
                p.Occurrences = {count!r}
            else:
                p = doc.addObject("PartDesign::LinearPattern", "LinearPattern")
                bd.addObject(p)
                p.Originals = [ft]
                p.Direction = (bd.getObject(axis_map[{dirn!r}]), [""])
                p.Length = {length!r}
                p.Occurrences = {count!r}
            doc.recompute()
            print("created", p.Name, "-", {kind!r}, "x", {count!r},
                  "along", {dirn!r})
            bad = [t for t in p.State if t in ("Invalid", "Error")]
            print("valid:", p.isValid(),
                  (" STATE: " + ",".join(bad)) if bad else "")
            if bd.Shape and not bd.Shape.isNull():
                b = bd.Shape.BoundBox
                print("body bbox: {{:.4f}} x {{:.4f}} x {{:.4f}} mm".format(
                    b.XLength, b.YLength, b.ZLength))
    """).format(body=body, feat=feature, kind=kind, count=count,
                length=length, dirn=direction, doc=document))


@mcp.tool()
def set_placement(
    object_name: str,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    axis_x: float = 0.0,
    axis_y: float = 0.0,
    axis_z: float = 1.0,
    angle: float = 0.0,
    document: str = "",
) -> str:
    """Position and rotate an object, then report where it actually ended up.

    Rotation composition is easy to get wrong and the error is invisible in the
    numbers, so this echoes the resulting bounding box. Check it against what
    you expected rather than trusting the placement you passed in.

    For an App::Link this sets LinkPlacement, which is what actually moves the
    instance.

    Args:
        object_name: Object to place.
        x: Position X in mm.
        y: Position Y in mm.
        z: Position Z in mm.
        axis_x: Rotation axis X component.
        axis_y: Rotation axis Y component.
        axis_z: Rotation axis Z component.
        angle: Rotation angle in degrees.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        o = doc.getObject({name!r})
        if o is None:
            print("ERROR: no object named", {name!r})
        else:
            plc = App.Placement(
                App.Vector({x!r}, {y!r}, {z!r}),
                App.Rotation(App.Vector({ax!r}, {ay!r}, {az!r}), {ang!r}))
            if o.TypeId == "App::Link":
                o.LinkPlacement = plc
            else:
                o.Placement = plc
            doc.recompute()
            if o.TypeId == "App::Link":
                b = o.LinkedObject.Shape
                s = b.copy()
                s.Placement = o.LinkPlacement.multiply(b.Placement)
            else:
                s = getattr(o, "Shape", None)
            print("placed", o.Name, "at", plc.Base, "rot", {ang!r}, "deg")
            if s is not None and not s.isNull():
                bb = s.BoundBox
                print("resulting bbox: X {{:.4f}}..{{:.4f}}  Y {{:.4f}}..{{:.4f}}  Z {{:.4f}}..{{:.4f}} mm".format(
                    bb.XMin, bb.XMax, bb.YMin, bb.YMax, bb.ZMin, bb.ZMax))
    """).format(name=object_name, x=x, y=y, z=z, ax=axis_x, ay=axis_y,
                az=axis_z, ang=angle, doc=document))


@mcp.tool()
def create_assembly(name: str = "Assembly", document: str = "") -> str:
    """Create an Assembly container (falls back to App::Part if unavailable).

    Args:
        name: Name for the assembly.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        try:
            a = doc.addObject("Assembly::AssemblyObject", {name!r})
            kind = "Assembly::AssemblyObject"
        except Exception:
            a = doc.addObject("App::Part", {name!r})
            kind = "App::Part"
        doc.recompute()
        print("created", a.Name, "as", kind)
    """).format(name=name, doc=document))


@mcp.tool()
def add_link(
    assembly: str,
    source: str,
    name: str = "",
    document: str = "",
) -> str:
    """Add an App::Link instance of an object into an assembly.

    Links let one Body appear many times without duplicating geometry. Position
    each instance afterwards with set_placement.

    Note: a Link renders nothing if its source Body's Tip is hidden -- use
    set_visibility rather than setting Visibility by hand.

    Args:
        assembly: Assembly or App::Part container name.
        source: Object to instance (usually a PartDesign Body).
        name: Optional name for the link.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        asm = doc.getObject({asm!r})
        src = doc.getObject({src!r})
        if asm is None or src is None:
            print("ERROR: missing assembly or source", {asm!r}, {src!r})
        else:
            l = doc.addObject("App::Link", {name!r} or ("L_" + src.Name))
            l.LinkedObject = src
            asm.addObject(l)
            doc.recompute()
            print("linked", src.Name, "into", asm.Name, "as", l.Name)
            print("children now:", len(asm.Group) if hasattr(asm, "Group") else "?")
    """).format(asm=assembly, src=source, name=name, doc=document))


@mcp.tool()
def transaction(action: str, name: str = "MCP edit", document: str = "") -> str:
    """Open, commit or roll back an undo transaction around a multi-step build.

    Without this a failure halfway through leaves a half-built document with no
    way back. Open before a batch, commit on success, abort on failure.

    Args:
        action: "begin", "commit" or "abort".
        name: Label shown in the undo stack.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        act = {act!r}
        if act == "begin":
            doc.openTransaction({name!r})
            print("transaction opened:", {name!r})
        elif act == "commit":
            doc.commitTransaction()
            print("transaction committed")
        elif act == "abort":
            doc.abortTransaction()
            doc.recompute()
            print("transaction rolled back; objects now:", len(doc.Objects))
        else:
            print("ERROR: action must be begin, commit or abort")
    """).format(act=action, name=name, doc=document))


@mcp.tool()
def create_document(name: str = "Unnamed") -> str:
    """Create a new empty FreeCAD document.

    Args:
        name: Name for the new document.
    """
    return _call("doc = App.newDocument({!r})\nprint('Created document:', doc.Name)".format(name))


@mcp.tool()
def open_document(file_path: str) -> str:
    """Open a FreeCAD document file (.FCStd).

    Args:
        file_path: Absolute path to the .FCStd file.
    """
    return _call(textwrap.dedent("""\
        doc = App.openDocument({path!r})
        print("Opened:", doc.Name, "({{}})".format(doc.FileName))
        print("Objects:", len(doc.Objects))
    """).format(path=file_path))


@mcp.tool()
def save_document(file_path: str = "", document: str = "") -> str:
    """Save a FreeCAD document.

    Args:
        file_path: Path to save to. Empty to save in-place.
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        if doc is None:
            print("ERROR: No active document")
        else:
            path = {path!r}
            if path:
                doc.saveAs(path)
                print("Saved as:", path)
            else:
                doc.save()
                print("Saved:", doc.FileName)
    """).format(path=file_path, doc=document))


@mcp.tool()
def close_document(document: str = "") -> str:
    """Close a FreeCAD document.

    Args:
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        if doc is None:
            print("ERROR: No active document")
        else:
            name = doc.Name
            App.closeDocument(name)
            print("Closed document:", name)
    """).format(doc=document))


@mcp.tool()
def add_object(
    type_id: str,
    name: str,
    document: str = "",
) -> str:
    """Add a new object to a FreeCAD document.

    Common type_ids:
      Part::Box, Part::Cylinder, Part::Sphere, Part::Cone, Part::Torus,
      Part::Cut, Part::Fuse, Part::Common, Part::Extrusion,
      Part::Feature, Part::FeaturePython,
      Mesh::Feature, Sketcher::SketchObject,
      PartDesign::Body, PartDesign::Pad, PartDesign::Pocket

    Args:
        type_id: FreeCAD object type (e.g. "Part::Box").
        name: Name for the new object.
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        if doc is None:
            doc = App.newDocument("Unnamed")
        obj = doc.addObject({type_id!r}, {name!r})
        doc.recompute()
        print("Created", obj.Name, "(" + obj.TypeId + ")")
    """).format(type_id=type_id, name=name, doc=document))


@mcp.tool()
def remove_object(object_name: str, document: str = "") -> str:
    """Remove an object from a FreeCAD document.

    Args:
        object_name: The internal Name of the object to remove.
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        if doc is None:
            print("ERROR: No active document")
        else:
            doc.removeObject({name!r})
            doc.recompute()
            print("Removed:", {name!r})
    """).format(name=object_name, doc=document))


@mcp.tool()
def get_selection() -> str:
    """Get the currently selected objects in FreeCAD."""
    return _call(textwrap.dedent("""\
        import json
        sel = Gui.Selection.getSelectionEx()
        if not sel:
            print("(nothing selected)")
        else:
            items = []
            for s in sel:
                info = {
                    "object": s.ObjectName,
                    "document": s.DocumentName,
                    "type": s.Object.TypeId if s.Object else None,
                    "sub_elements": list(s.SubElementNames) if s.SubElementNames else [],
                }
                items.append(info)
            print(json.dumps(items, indent=2))
    """))


@mcp.tool()
def recompute(document: str = "") -> str:
    """Recompute (rebuild) all objects in a FreeCAD document.

    Args:
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        if doc is None:
            print("ERROR: No active document")
        else:
            touched = doc.recompute()
            print("Recomputed", len(touched) if touched else 0, "objects in", doc.Name)
    """).format(doc=document))


@mcp.tool()
def export_object(
    object_names: str,
    file_path: str,
    document: str = "",
) -> str:
    """Export one or more objects to a file (STEP, STL, BREP, OBJ, etc.).

    The format is determined by the file extension.

    Args:
        object_names: Comma-separated list of object names to export.
        file_path: Output file path with extension (e.g. "/tmp/part.step").
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        import Part as _Part
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        if doc is None:
            print("ERROR: No active document")
        else:
            names = [n.strip() for n in {names!r}.split(",")]
            objs = [doc.getObject(n) for n in names]
            missing = [n for n, o in zip(names, objs) if o is None]
            if missing:
                print("ERROR: Objects not found:", missing)
            else:
                shapes = [o.Shape for o in objs if hasattr(o, "Shape")]
                if not shapes:
                    print("ERROR: No exportable shapes found")
                else:
                    _Part.export(shapes, {path!r})
                    print("Exported", len(shapes), "shape(s) to", {path!r})
    """).format(names=object_names, path=file_path, doc=document))


@mcp.tool()
def set_visibility(
    object_names: str,
    visible: bool = True,
    document: str = "",
) -> str:
    """Show or hide objects, handling the cases that silently render nothing.

    Visibility in FreeCAD is not one flag. Three traps make a model vanish while
    every object still reports Visibility=True, and all three are handled here:

      * PartDesign: the Body's Tip feature is what draws, not the Body.
      * App::Link: renders nothing when its source Body's Tip is hidden.
      * Assembly / App::Part containers: hiding one hides every child.

    Args:
        object_names: Comma-separated names, or "*" for everything.
        visible: True to show, False to hide.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        names = {names!r}
        objs = doc.Objects if names.strip() == "*" else [
            doc.getObject(n.strip()) for n in names.split(",") if n.strip()]
        vis = {vis!r}
        done = 0
        for o in objs:
            if o is None:
                continue
            try:
                o.ViewObject.Visibility = vis
                done += 1
            except Exception:
                continue
            # Only force the Tip on when SHOWING. Forcing it on while hiding
            # leaves the tip features drawn and the model stubbornly visible.
            if vis:
                # a Body draws through its Tip feature
                if o.TypeId == "PartDesign::Body" and getattr(o, "Tip", None) is not None:
                    try:
                        o.Tip.ViewObject.Visibility = True
                    except Exception:
                        pass
                # a Link draws through its source Body's Tip
                if o.TypeId == "App::Link":
                    src = getattr(o, "LinkedObject", None)
                    if src is not None and getattr(src, "Tip", None) is not None:
                        try:
                            src.Tip.ViewObject.Visibility = True
                        except Exception:
                            pass
            else:
                if o.TypeId == "PartDesign::Body" and getattr(o, "Tip", None) is not None:
                    try:
                        o.Tip.ViewObject.Visibility = False
                    except Exception:
                        pass
        doc.recompute()
        print("set Visibility=" + str(vis), "on", done, "object(s)")
        shown = [o.Name for o in doc.Objects
                 if getattr(getattr(o, "ViewObject", None), "Visibility", False)]
        print("visible now:", len(shown))
    """).format(names=object_names, vis=visible, doc=document))


@mcp.tool()
def measure(
    object_a: str,
    object_b: str = "",
    document: str = "",
) -> str:
    """Measure an object, or the distance and overlap between two.

    Use this to VERIFY a model rather than trusting that it built correctly.
    With one object you get its bounding box; with two you additionally get the
    minimum distance between them and their intersection volume.

    An intersection volume above zero on parts that should merely touch means
    they interfere. A distance above zero on parts that should mate means a gap.

    Args:
        object_a: First object name.
        object_b: Optional second object name.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        IN = 25.4
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        def shape_of(o):
            if o.TypeId == "App::Link":
                b = o.LinkedObject.Shape
                s = b.copy()
                s.Placement = o.LinkPlacement.multiply(b.Placement)
                return s
            return o.Shape
        a = doc.getObject({a!r})
        if a is None:
            print("ERROR: no object named", {a!r})
        else:
            sa = shape_of(a); bb = sa.BoundBox
            print("{{}}: {{:.4f}} x {{:.4f}} x {{:.4f}} mm  ({{:.4f}} x {{:.4f}} x {{:.4f}} in)".format(
                a.Name, bb.XLength, bb.YLength, bb.ZLength,
                bb.XLength/IN, bb.YLength/IN, bb.ZLength/IN))
            print("  origin ({{:.4f}}, {{:.4f}}, {{:.4f}}) mm   volume {{:.3f}} in3".format(
                bb.XMin, bb.YMin, bb.ZMin, sa.Volume/IN**3))
            if {b!r}:
                bo = doc.getObject({b!r})
                if bo is None:
                    print("ERROR: no object named", {b!r})
                else:
                    sb = shape_of(bo)
                    dist = sa.distToShape(sb)[0]
                    common = sa.common(sb).Volume
                    print("  distance to {{}}: {{:.6f}} mm ({{:.6f}} in)".format(
                        bo.Name, dist, dist/IN))
                    print("  intersection   : {{:.6f}} mm3 ({{:.6f}} in3){{}}".format(
                        common, common/IN**3, "  <-- INTERFERENCE" if common > 1e-3 else ""))
    """).format(a=object_a, b=object_b, doc=document))


@mcp.tool()
def check_interference(object_names: str = "*", document: str = "") -> str:
    """Pairwise interference check across many objects.

    Reports every pair whose solids actually overlap. Bounding boxes are used to
    skip non-touching pairs, so this stays usable on assemblies with dozens of
    parts. Run it before believing an assembly is correct -- coincident
    placement numbers do not prove the solids mate.

    Args:
        object_names: Comma-separated names, or "*" for all links and solids.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        IN = 25.4
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        names = {names!r}
        if names.strip() == "*":
            objs = [o for o in doc.Objects
                    if o.TypeId == "App::Link" or
                    (hasattr(o, "Shape") and o.Shape and o.Shape.Solids)]
        else:
            objs = [doc.getObject(n.strip()) for n in names.split(",") if n.strip()]
        def shape_of(o):
            if o.TypeId == "App::Link":
                b = o.LinkedObject.Shape
                s = b.copy()
                s.Placement = o.LinkPlacement.multiply(b.Placement)
                return s
            return o.Shape
        shp = [(o.Name, shape_of(o)) for o in objs if o is not None]
        hits = 0
        pairs = 0
        for i in range(len(shp)):
            for j in range(i + 1, len(shp)):
                n1, s1 = shp[i]; n2, s2 = shp[j]
                b1, b2 = s1.BoundBox, s2.BoundBox
                if not (b1.XMin < b2.XMax - 1e-7 and b2.XMin < b1.XMax - 1e-7 and
                        b1.YMin < b2.YMax - 1e-7 and b2.YMin < b1.YMax - 1e-7 and
                        b1.ZMin < b2.ZMax - 1e-7 and b2.ZMin < b1.ZMax - 1e-7):
                    continue
                pairs += 1
                v = s1.common(s2).Volume
                if v > 1e-3:
                    hits += 1
                    print("  INTERFERENCE {{}} x {{}}: {{:.6f}} in3".format(n1, n2, v/IN**3))
        print("checked {{}} object(s), {{}} overlapping bbox pair(s), {{}} real interference(s)".format(
            len(shp), pairs, hits))
    """).format(names=object_names, doc=document))


@mcp.tool()
def get_errors(document: str = "") -> str:
    """List objects that failed to recompute or are still touched.

    A FreeCAD document happily holds broken features. Nothing raises, the tree
    just shows a marker most scripted callers never look at, so a build can
    report success while several features are in error. Check this after any
    multi-step build.

    Args:
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        bad, touched, nullshape = [], [], []
        for o in doc.Objects:
            st = list(o.State)
            if [t for t in st if t in ("Invalid", "Error")] or not o.isValid():
                bad.append(o.Name)
            if o.State and "Touched" in o.State:
                touched.append(o.Name)
            if hasattr(o, "Shape") and o.Shape is not None and o.Shape.isNull() \\
               and o.TypeId not in ("App::Part", "App::Link"):
                nullshape.append(o.Name)
        print("objects        :", len(doc.Objects))
        print("in error       :", len(bad), bad[:12])
        print("still touched  :", len(touched), touched[:12])
        print("null shape     :", len(nullshape), nullshape[:12])
        if not bad and not touched:
            print("document is clean")
    """).format(doc=document))


@mcp.tool()
def export_dxf(
    object_names: str,
    file_path: str,
    units: str = "in",
    deviation: float = 0.01,
    document: str = "",
) -> str:
    """Export flat geometry to a 2D DXF, 1:1, with no external library.

    Writes edges straight out of the shapes as DXF LINE entities. Curves are
    discretised to `deviation`. This is 1:1 geometry suitable for CAM, not a
    projected drawing view.

    Why not the obvious routes: FreeCAD's importDXF exporter needs a helper
    library it prompts to download, and that prompt deadlocks scripted sessions;
    TechDraw's writeDXFView was verified to emit an empty file here even for
    solids. Writing the entities directly avoids both.

    The entity count is verified before reporting success -- a DXF containing
    only headers is a failure, not an export.

    Args:
        object_names: Comma-separated object names (sketches, solids, or links).
        file_path: Destination .dxf path.
        units: "in" or "mm". FreeCAD works in mm; "in" divides by 25.4 and sets
            the DXF unit headers to inches.
        deviation: Chord tolerance for discretising curves, in output units.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        scale = 25.4 if {units!r} == "in" else 1.0
        insunits = 1 if {units!r} == "in" else 4
        objs = [doc.getObject(n.strip()) for n in {names!r}.split(",") if n.strip()]
        objs = [o for o in objs if o is not None]

        def shape_of(o):
            if o.TypeId == "App::Link":
                b = o.LinkedObject.Shape
                s = b.copy()
                s.Placement = o.LinkPlacement.multiply(b.Placement)
                return s
            return getattr(o, "Shape", None)

        segs = []
        fell_back = 0
        for o in objs:
            sh = shape_of(o)
            if sh is None or sh.isNull():
                continue
            for e in sh.Edges:
                # NOTE: the keyword is Deflection. "Deviation" raises
                # Part.OCCError, and a silent fallback would emit every arc as a
                # single chord -- straight lines look fine, curves come out wrong.
                try:
                    pts = e.discretize(Deflection={dev!r} * scale)
                except Exception:
                    fell_back += 1
                    pts = [e.Vertexes[0].Point, e.Vertexes[-1].Point] \\
                          if len(e.Vertexes) >= 2 else []
                for i in range(len(pts) - 1):
                    a, b = pts[i], pts[i + 1]
                    if (a - b).Length > 1e-9:
                        segs.append((a.x / scale, a.y / scale,
                                     b.x / scale, b.y / scale))

        if not segs:
            print("FAILED: no edges found on", [o.Name for o in objs])
        else:
            out = ["999", "created by FreeCAD MCP",
                   "0", "SECTION", "2", "HEADER",
                   "9", "$INSUNITS", "70", str(insunits),
                   "9", "$MEASUREMENT", "70", "0" if {units!r} == "in" else "1",
                   "0", "ENDSEC",
                   "0", "SECTION", "2", "ENTITIES"]
            for x1, y1, x2, y2 in segs:
                out += ["0", "LINE", "8", "CUT",
                        "10", "%.6f" % x1, "20", "%.6f" % y1, "30", "0.0",
                        "11", "%.6f" % x2, "21", "%.6f" % y2, "31", "0.0"]
            out += ["0", "ENDSEC", "0", "EOF"]
            with open({path!r}, "w") as fh:
                fh.write("\\n".join(out) + "\\n")
            xs = [v for s in segs for v in (s[0], s[2])]
            ys = [v for s in segs for v in (s[1], s[3])]
            import os
            print("exported", {path!r}, os.path.getsize({path!r}), "bytes")
            print("  {{}} LINE entities from {{}} object(s)".format(len(segs), len(objs)))
            print("  extents {{:.4f}} x {{:.4f}} {{}}".format(
                max(xs) - min(xs), max(ys) - min(ys), {units!r}))
            if fell_back:
                print("  WARNING: {{}} edge(s) could not be discretised and were "
                      "emitted as a single straight chord. Any curve among them "
                      "is WRONG in this file.".format(fell_back))
    """).format(names=object_names, path=file_path, units=units,
                dev=deviation, doc=document))


@mcp.tool()
def capture_screenshot(
    file_path: str,
    width: int = 1920,
    height: int = 1080,
    method: str = "auto",
) -> str:
    """Capture a screenshot of the current FreeCAD 3D view.

    The image is checked for content before returning. A blank screenshot
    otherwise reads as success, and the caller carries on believing it saw
    the model. When the image is empty this retries with an on-screen grab
    and, if that is empty too, says the scene has nothing visible in it.

    Args:
        file_path: Where to save the image (e.g. "/tmp/freecad_view.png").
        width: Image width in pixels (offscreen only; a widget grab uses the
            viewport's own size).
        height: Image height in pixels.
        method: "auto", "offscreen", or "widget".
    """
    try:
        result = _rpc().screenshot(file_path, width, height, "Current", method)
    except Exception as e:  # server too old to expose screenshot()
        return _call(textwrap.dedent("""\
            view = Gui.ActiveDocument.ActiveView
            view.saveImage({path!r}, {w}, {h}, "Current")
            print("Screenshot saved to", {path!r})
            print("NOTE: reload the FreeCAD MCP workbench to enable "
                  "blank-render detection ({err})")
        """).format(path=file_path, w=width, h=height, err=e))
    return _format(result)


@mcp.tool()
def fit_view() -> str:
    """Fit the 3D view to show all objects."""
    return _call("Gui.SendMsgToActiveView('ViewFit')\nprint('View fitted')")


@mcp.tool()
def import_file(file_path: str, document: str = "") -> str:
    """Import a file (STEP, IGES, STL, SVG, DXF, etc.) into a document.

    Args:
        file_path: Path to the file to import.
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        import Import
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        if doc is None:
            doc = App.newDocument("Imported")
        Import.insert({path!r}, doc.Name)
        doc.recompute()
        print("Imported", {path!r}, "into", doc.Name)
        print("Objects now:", len(doc.Objects))
    """).format(path=file_path, doc=document))


# ── Entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
