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
from typing import Optional

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

# Seconds the in-FreeCAD server waits for a call before giving up on it.
# Everything runs on FreeCAD's GUI thread, so this is also how long the
# application can be frozen by one call.
DEFAULT_TIMEOUT = 60

mcp = _MCPServer(
    name="FreeCAD",
    instructions=textwrap.dedent("""\
        Drives a running FreeCAD (1.0 or newer) over its in-process RPC
        server. FreeCAD must be open with the MCP RPC server started
        (TrailCurrent FreeCAD Tools workbench > MCP Server button).

        TOOL GROUPS
          documents   list/create/open/save/close_document, get_document_info,
                      find_objects, dependencies, list_object_types,
                      transaction, undo, redo
          objects     add_object, remove_object, get/set_object_property,
                      get_property_info, add_property, touch, set_placement,
                      set_visibility, set/get_appearance, get/set/clear_selection,
                      recompute, get_errors
          sketches    create_sketch, sketch_add_polyline/circle/rectangle/arc/
                      slot/polygon, sketch_constrain, sketch_delete_geometry,
                      sketch_list_geometry, sketch_info
          partdesign  partdesign_feature (pad/pocket), partdesign_revolve, _hole,
                      _loft, _pipe, _helix, _primitive, _pattern, _dressup,
                      set_tip, create_datum, create_shapebinder
          parametric  spreadsheet_set/get, set_expression, clear_expression
          assembly    create_assembly, add_link, assembly_ground,
                      assembly_add_joint, assembly_solve, assembly_bom
          inspection  measure, measure_element, measure_distance, point_inside,
                      check_interference, capture_screenshot, set_view,
                      set_camera, fit_view
          mesh        shape_to_mesh, mesh_info, mesh_to_shape, mesh_fix
          cam         cam_create_job, cam_add_tool, cam_list_tools,
                      cam_add_operation, cam_add_dressup, cam_post, cam_audit
          drawing     drawing_create_page, drawing_add_view, drawing_add_section,
                      drawing_add_dimension, drawing_export
          draft       draft_clone, draft_array, draft_wire
          io          import_file, export_object, export_dxf
          escape      execute_code -- arbitrary Python inside FreeCAD

        WORKFLOW
          Build with PartDesign (Body > Sketch > Pad/Pocket), never Part
          booleans. Put design inputs in a spreadsheet (spreadsheet_set with
          an alias) and bind geometry to them with set_expression; name sketch
          dimensions with sketch_constrain so they can be driven too. Verify
          with measure / point_inside / check_interference / get_errors rather
          than trusting that a build worked. Every modelling tool reports the
          Body volume before and after: a zero delta means the feature did
          nothing. Wrap multi-step builds in transaction("begin") / "commit".

        EVERY CALL RUNS ON FREECAD'S GUI THREAD
          The application is frozen for the duration. Keep calls short
          (well under a second). A call that exceeds its timeout (60 s by
          default; execute_code takes a timeout argument) is abandoned by
          the caller but keeps running in FreeCAD. Never open a dialog or
          task panel from execute_code: a modal dialog would block every
          later call.

        READING RESULTS
          Results carry FreeCAD's own Console output. "[FreeCAD warnings]"
          and "[FreeCAD errors]" are what the Report view showed during the
          call; recompute failures and deprecation notices appear there, not
          in stdout. "[dialogs auto-dismissed]" means a modal prompt was
          answered on your behalf with its default (usually No / Cancel), so
          the operation may have taken a branch you did not intend. A line
          starting "ERROR:" is an expected failure (missing object, bad
          argument); "[error]" with a traceback is an unexpected one.

        EXECUTE_CODE ENVIRONMENT
          Pre-imported: FreeCAD / App, FreeCADGui / Gui, Part, Mesh, MeshPart,
          Draft, Sketcher, PartDesign, Import, ImportGui, importSVG, importDXF,
          BOPTools. A single expression returns its repr; otherwise print().
          Reachable only through execute_code (no dedicated tools): Fem,
          Surface, Points, OpenSCAD, Inspection, Robot, ReverseEngineering,
          Material, BIM / Arch, Plot, Image, and any add-on workbench.

        Key pattern:
          doc = App.ActiveDocument
          obj = doc.getObject("Box")
          obj.Height = 20
          doc.recompute()
    """),
)


# Tool annotations tell a client which tools only read, which ones destroy
# state, and which are safe to repeat. Older SDKs lack them; degrade quietly.
try:
    from mcp.types import ToolAnnotations as _ToolAnnotations
except ImportError:  # pragma: no cover - depends on installed SDK
    _ToolAnnotations = None


def _tool(read_only: bool = False, destructive: bool = False, idempotent: bool = False):
    """mcp.tool() with annotations, tolerant of SDKs that do not take them."""
    if _ToolAnnotations is not None:
        ann = _ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive,
                               idempotent_hint=idempotent, open_world_hint=False)
        try:
            return mcp.tool(annotations=ann)
        except TypeError:
            pass
    return mcp.tool()


class _TimeoutTransport(xmlrpc.client.Transport):
    """HTTP transport with a socket timeout.

    The in-FreeCAD server enforces its own per-call limit. The client-side
    socket timeout sits a little above it so the client never outlives the
    server's verdict, but also never waits forever on a wedged GUI thread.
    """

    def __init__(self, timeout):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def _rpc(timeout: float = DEFAULT_TIMEOUT):
    """Get an XML-RPC proxy to FreeCAD."""
    return xmlrpc.client.ServerProxy(
        RPC_URL, allow_none=True, transport=_TimeoutTransport(timeout + 10))


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
    if result.get("console_warnings"):
        parts.append("[FreeCAD warnings]\n  " + "\n  ".join(result["console_warnings"]))
    if result.get("console_errors"):
        parts.append("[FreeCAD errors]\n  " + "\n  ".join(result["console_errors"]))
    if result.get("dialogs"):
        # A prompt was answered automatically. The operation may have taken a
        # default branch it would not have taken interactively, so say so
        # loudly rather than letting it look like a clean success.
        parts.append("[dialogs auto-dismissed -- the answer below was assumed]\n  "
                     + "\n  ".join(result["dialogs"]))
    return "\n".join(parts) if parts else "(no output)"


# ── Shared helpers, executed inside FreeCAD ────────────────────────
#
# Every convenience tool is a snippet of Python sent to FreeCAD. These helpers
# are prepended to each one so that document lookup, Link-shape resolution and
# Body-origin lookup exist in exactly one place. Three private copies of the
# Link code and two different origin lookups is how the tools drifted apart.
#
# _Fail is for expected failures (missing object, wrong kind). The snippet is
# wrapped so a _Fail prints one ERROR line; anything else is a real traceback.

_PRELUDE = textwrap.dedent('''\
    import os as _os
    import Part as _Part

    class _Fail(Exception):
        """An expected failure. Reported as one ERROR line, not a traceback."""

    def _doc(name=""):
        d = App.getDocument(name) if name else App.ActiveDocument
        if d is None:
            raise _Fail("no document named %r" % name if name else
                        "no active document (create_document or open_document first)")
        return d

    def _obj(doc, name, what="object"):
        o = doc.getObject(name) if name else None
        if o is None:
            raise _Fail("no %s named %r in document %s" % (what, name, doc.Name))
        return o

    def _shape_of(o):
        """Positioned shape of any object, resolving App::Link; None if it has none."""
        if o is None:
            return None
        try:
            s = _Part.getShape(o)
            if s is not None and not s.isNull():
                return s
        except Exception:
            pass
        if o.TypeId == "App::Link":
            base = _shape_of(o.LinkedObject)
            if base is None:
                return None
            s = base.copy()
            s.Placement = o.LinkPlacement.multiply(base.Placement)
            return s
        s = getattr(o, "Shape", None)
        return None if (s is None or s.isNull()) else s

    def _origin_feature(body, role):
        """An origin plane or axis of a Body, found by role, never by name.

        Document-level names (XY_Plane, XY_Plane001, ...) belong to whichever
        Body was created first, so a name lookup attaches a second Body's
        sketch to the first Body's origin.
        """
        r = role.strip()
        u = r.upper()
        if u in ("XY", "XZ", "YZ"):
            r = u + "_Plane"
        elif u in ("X", "Y", "Z"):
            r = u + "_Axis"
        for f in body.Origin.OriginFeatures:
            if f.Role == r:
                return f
        raise _Fail("%s has no origin feature with role %r" % (body.Name, r))

    def _volume(o):
        s = _shape_of(o)
        return s.Volume if s is not None else 0.0

    def _bbox_str(s):
        b = s.BoundBox
        return "%.4f x %.4f x %.4f mm  (X %.4f..%.4f  Y %.4f..%.4f  Z %.4f..%.4f)" % (
            b.XLength, b.YLength, b.ZLength,
            b.XMin, b.XMax, b.YMin, b.YMax, b.ZMin, b.ZMax)

    def _state_str(f):
        bad = [t for t in f.State if t in ("Invalid", "Error")]
        return "valid: %s%s" % (f.isValid(), ("  STATE: " + ",".join(bad)) if bad else "")

    def _names(csv):
        return [n.strip() for n in csv.split(",") if n.strip()]

    def _vstr(v):
        return "(%.4f, %.4f, %.4f)" % (v.x, v.y, v.z)

    def _body(doc, name):
        b = _obj(doc, name, "body")
        if b.TypeId != "PartDesign::Body":
            raise _Fail("%s is a %s, not a PartDesign::Body" % (b.Name, b.TypeId))
        return b

    def _sub_ref(doc, text, what="reference"):
        """Parse "Object", "Object:Face3" or "Object.Edge2" into (object, [sub])."""
        oname, _, sub = text.strip().replace(".", ":").partition(":")
        return _obj(doc, oname, what), ([sub] if sub else [""])

    def _report_feature(bd, f, vol0):
        vol1 = _volume(bd)
        s = _shape_of(bd)
        print("created", f.Name, "(" + f.TypeId + ") on", bd.Name,
              " Tip:", bd.Tip.Name if bd.Tip else None)
        print(_state_str(f))
        print("body volume: %.4f -> %.4f mm3 (delta %+.4f)" % (vol0, vol1, vol1 - vol0))
        if abs(vol1 - vol0) < 1e-6 and f.isValid():
            print("WARNING: the body volume did not change -- the feature added or removed nothing.")
        if s is not None and s.Solids:
            print("body bbox:", _bbox_str(s))

    def _axis_ref(doc, bd, sk, axis):
        """Resolve a revolve/helix axis: a sketch axis, a body origin axis, or Object:Edge."""
        a = axis.strip()
        if a in ("V_Axis", "H_Axis", "N_Axis"):
            return (sk, [a])
        if a.upper() in ("X", "Y", "Z"):
            return (_origin_feature(bd, a.upper() + "_Axis"), [""])
        return _sub_ref(doc, a, "axis")

''')


def _wrap(code: str) -> str:
    if not code.endswith("\n"):
        code += "\n"
    return (_PRELUDE + "try:\n" + textwrap.indent(code, "    ")
            + "except _Fail as _e:\n    print('ERROR:', _e)\n")


def _call(code: str, timeout: float = DEFAULT_TIMEOUT, raw: bool = False) -> str:
    """Execute code in FreeCAD and return formatted output.

    raw=True sends the code as-is (execute_code). Otherwise the shared helper
    prelude is prepended and expected failures are reported as one line.
    """
    try:
        proxy = _rpc(timeout)
        result = proxy.execute(code if raw else _wrap(code), timeout)
    except ConnectionRefusedError:
        return (
            "ERROR: Cannot connect to FreeCAD RPC server at {}. "
            "Make sure FreeCAD is running and the MCP RPC server is started "
            "(TrailCurrent FreeCAD Tools workbench > MCP Server toolbar button)."
        ).format(RPC_URL)
    except TimeoutError:
        return ("ERROR: no reply from FreeCAD within {}s. The call may still be "
                "running on the GUI thread.").format(timeout)
    except Exception as e:
        return "ERROR: RPC call failed: {}".format(e)

    return _format(result)


# File extensions each exporter handles. Mesh formats go through MeshPart so
# a deflection can be chosen and App::Link resolves to a positioned shape.
_CAD_EXTS = ("step", "stp", "iges", "igs")
_BREP_EXTS = ("brep", "brp")
_GLTF_EXTS = ("glb", "gltf")
_MESH_EXTS = ("stl", "ast", "obj", "ply", "off", "amf", "3mf", "smf", "bms", "wrl", "x3d")
_MESH_IN_EXTS = ("stl", "ast", "obj", "ply", "off", "amf", "3mf", "smf", "bms")


# ── Tools ──────────────────────────────────────────────────────────


@_tool()
def execute_code(code: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Execute arbitrary Python code inside FreeCAD's interpreter.

    The code runs on FreeCAD's main thread with full access to all
    FreeCAD modules (App, Gui, Part, Mesh, Draft, etc.). The application
    is frozen until the code returns, so keep each call short.

    Use print() to produce output. If the code is a single expression,
    its repr() is returned automatically. Warnings and errors FreeCAD
    writes to its Report view during the call are returned too.

    Examples:
      # List all objects in the active document
      for obj in App.ActiveDocument.Objects:
          print(obj.Name, obj.TypeId)

      # A parametric part: Body -> Sketch -> Pad
      doc = App.ActiveDocument or App.newDocument("Unnamed")
      body = doc.addObject("PartDesign::Body", "Body")
      sk = doc.addObject("Sketcher::SketchObject", "Profile")
      body.addObject(sk)
      sk.AttachmentSupport = [(body.Origin.OriginFeatures[3], "")]  # XY_Plane
      sk.MapMode = "FlatFace"
      sk.addGeometry(Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 10))
      pad = doc.addObject("PartDesign::Pad", "Pad")
      body.addObject(pad)
      pad.Profile = sk
      pad.Length = 5
      doc.recompute()

    Args:
        code: Python source to run.
        timeout: Seconds to wait for the call. The default is 60. A call
            that runs past it is abandoned here but keeps running in FreeCAD.
    """
    return _call(code, timeout=timeout, raw=True)


@_tool(read_only=True, idempotent=True)
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


@_tool(read_only=True, idempotent=True)
def get_document_info(
    document: str = "",
    include_shape_stats: bool = False,
    type_filter: str = "",
    label_contains: str = "",
) -> str:
    """List a document's objects with type, visibility and bounding box.

    Cheap by default. Volume and Area need a full evaluation of every shape,
    which is seconds on the GUI thread for a large assembly, so they are off
    unless asked for.

    Args:
        document: Document name. Empty string for the active document.
        include_shape_stats: Also report Volume and Area of every shape.
        type_filter: Only objects whose TypeId contains this text, e.g.
            "PartDesign::" or "App::Link".
        label_contains: Only objects whose Label contains this text
            (case-insensitive).
    """
    return _call(textwrap.dedent("""\
        import json
        doc = _doc({doc!r})
        objects = []
        for obj in doc.Objects:
            if {tf!r} and {tf!r} not in obj.TypeId:
                continue
            if {lc!r} and {lc!r}.lower() not in obj.Label.lower():
                continue
            vo = getattr(obj, "ViewObject", None)
            info = {{
                "name": obj.Name,
                "label": obj.Label,
                "type": obj.TypeId,
                "visibility": vo.Visibility if vo is not None else None,
            }}
            s = _shape_of(obj) if (hasattr(obj, "Shape") or obj.TypeId == "App::Link") else None
            if s is not None:
                shape_info = {{"type": s.ShapeType}}
                if {stats!r}:
                    shape_info["volume"] = round(s.Volume, 4)
                    shape_info["area"] = round(s.Area, 4)
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
            "object_count": len(doc.Objects),
            "listed": len(objects),
            "objects": objects,
        }}
        print(json.dumps(result, indent=2, default=str))
    """).format(doc=document, stats=include_shape_stats, tf=type_filter,
                lc=label_contains))


@_tool(read_only=True, idempotent=True)
def get_object_properties(object_name: str, document: str = "") -> str:
    """Get all properties of a FreeCAD object.

    Args:
        object_name: The internal Name of the object.
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        import json
        doc = _doc({doc!r})
        obj = _obj(doc, {name!r})
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


@_tool(idempotent=True)
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
        doc = _doc({doc!r})
        obj = _obj(doc, {name!r})
        if {prop!r} not in obj.PropertiesList:
            raise _Fail("%s has no property %r" % (obj.Name, {prop!r}))
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


@_tool(idempotent=True)
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

    A sketch dimension is addressed through its named constraint:
        set_expression("Profile", "Constraints.width", "Design.width")

    Args:
        object_name: Object whose property to bind (e.g. "pad_Rib01").
        property_name: Property to drive (e.g. "Length", "Radius", "Angle").
        expression: FreeCAD expression, e.g. "Design.thickness" or "Box.Height / 2".
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        obj = _obj(doc, {name!r})
        obj.setExpression({prop!r}, {expr!r})
        doc.recompute()
        print("bound", {name!r} + "." + {prop!r}, "->", {expr!r})
        try:
            print("value now:", obj.getPropertyByName({prop!r}.split(".")[0]) if "." in {prop!r}
                  else getattr(obj, {prop!r}, "?"))
        except Exception:
            pass
        bad = [t for t in obj.State if t in ("Invalid", "Error")]
        if bad:
            print("WARNING: object state is", bad, "-- the expression may not resolve")
    """).format(name=object_name, prop=property_name, expr=expression, doc=document))


@_tool(idempotent=True)
def clear_expression(object_name: str, property_name: str, document: str = "") -> str:
    """Remove an expression binding, leaving the property at its current value.

    Args:
        object_name: Object to unbind.
        property_name: Property to release.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        obj = _obj(doc, {name!r})
        obj.setExpression({prop!r}, None)
        doc.recompute()
        print("cleared expression on", {name!r} + "." + {prop!r})
    """).format(name=object_name, prop=property_name, doc=document))


@_tool(idempotent=True)
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
        doc = _doc({doc!r})
        sh = doc.getObject({sheet!r})
        if sh is None:
            sh = doc.addObject("Spreadsheet::Sheet", {sheet!r})
            print("created spreadsheet", {sheet!r})
        elif sh.TypeId != "Spreadsheet::Sheet":
            raise _Fail("%s is a %s, not a spreadsheet" % (sh.Name, sh.TypeId))
        sh.set({cell!r}, {value!r})
        if {alias!r}:
            sh.setAlias({cell!r}, {alias!r})
        doc.recompute()
        print({cell!r}, "=", {value!r}, ("alias " + {alias!r}) if {alias!r} else "")
        if {alias!r}:
            print("resolves to:", sh.get({alias!r}))
    """).format(sheet=sheet_name, cell=cell, value=value, alias=alias, doc=document))


@_tool(read_only=True, idempotent=True)
def spreadsheet_get(sheet_name: str, cell_or_alias: str = "", document: str = "") -> str:
    """Read a spreadsheet cell or alias, or list every aliased cell if none is given.

    The listing shows cell, alias, content (formula or literal) and the
    resolved value, one row per alias.

    Args:
        sheet_name: Spreadsheet object name.
        cell_or_alias: Cell address or alias. Empty lists all aliases.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        sh = _obj(doc, {sheet!r}, "spreadsheet")
        if sh.TypeId != "Spreadsheet::Sheet":
            raise _Fail("%s is a %s, not a spreadsheet" % (sh.Name, sh.TypeId))
        if {key!r}:
            cell = sh.getCellFromAlias({key!r}) or {key!r}
            print({key!r}, "=", sh.get(cell), "  (cell %s, content %r)" % (cell, sh.getContents(cell)))
        else:
            rows = 0
            plain = 0
            print("  {{:<6}} {{:<18}} {{:<28}} {{}}".format("cell", "alias", "content", "value"))
            for c in sh.getUsedCells():
                alias = sh.getAlias(c) or ""
                if not alias:
                    plain += 1
                    continue
                try:
                    v = sh.get(c)
                except Exception as e:
                    v = "<error: %s>" % e
                print("  {{:<6}} {{:<18}} {{:<28}} {{}}".format(c, alias, sh.getContents(c), v))
                rows += 1
            if not rows:
                print("  (no aliases defined)")
            print("{{}} aliased cell(s), {{}} other used cell(s)".format(rows, plain))
    """).format(sheet=sheet_name, key=cell_or_alias, doc=document))


@_tool()
def create_sketch(
    name: str,
    plane: str = "XY",
    offset: float = 0.0,
    body: str = "",
    support: str = "",
    document: str = "",
) -> str:
    """Create a Sketch on an origin plane or a datum, optionally inside a Body.

    Attach to origin planes with an offset. Never attach to a face name like
    "Face6" -- face numbering is unstable and re-orders when the model changes.

    With a body, the plane is taken from that body's own origin, so a second
    Body's sketch lands in the second Body, not the first.

    Args:
        name: Name for the new sketch.
        plane: "XY", "XZ" or "YZ". Ignored when support is given.
        offset: Distance along the plane's normal, in mm. XY's normal is +Z
            and YZ's is +X, but XZ's normal is -Y, so a positive offset on XZ
            moves toward negative world Y. The world placement is printed;
            check it.
        body: Optional PartDesign Body to create the sketch inside.
        support: Optional object to attach to instead of an origin plane,
            e.g. a datum plane name from create_datum.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        bd = _obj(doc, {body!r}, "body") if {body!r} else None
        if bd is not None and bd.TypeId != "PartDesign::Body":
            raise _Fail("%s is a %s, not a PartDesign::Body" % (bd.Name, bd.TypeId))
        if {support!r}:
            plane_obj = _obj(doc, {support!r}, "support")
        elif bd is not None:
            plane_obj = _origin_feature(bd, {plane!r})
        else:
            plane_obj = None
            if {plane!r}.upper() not in ("XY", "XZ", "YZ"):
                raise _Fail("plane must be XY, XZ or YZ")
        sk = doc.addObject("Sketcher::SketchObject", {name!r})
        if bd is not None:
            bd.addObject(sk)
        if plane_obj is not None:
            sk.AttachmentSupport = [(plane_obj, "")]
            sk.MapMode = "FlatFace"
            if {off!r}:
                sk.AttachmentOffset = App.Placement(
                    App.Vector(0, 0, {off!r}), App.Rotation())
        else:
            # No Body, no origin planes: place the sketch directly with the
            # same orientation an origin plane of that name would have.
            rot = {{"XY": App.Rotation(),
                   "XZ": App.Rotation(App.Vector(1, 0, 0), 90),
                   "YZ": App.Rotation(App.Vector(1, 1, 1), 120)}}[{plane!r}.upper()]
            sk.Placement = App.Placement(rot.multVec(App.Vector(0, 0, {off!r})), rot)
        doc.recompute()
        where = ("in " + bd.Name) if bd is not None else "outside any Body"
        on = plane_obj.Name if plane_obj is not None else ({plane!r}.upper() + " (free placement)")
        print("created sketch", sk.Name, where, "on", on, "offset", {off!r}, "mm")
        sup = sk.AttachmentSupport
        print("support:", ", ".join(o.Name + (":" + ",".join(x for x in s if x) if any(s) else "")
                                    for o, s in sup) if sup else "none")
        print("world placement:", _vstr(sk.Placement.Base),
              " normal:", _vstr(sk.Placement.Rotation.multVec(App.Vector(0, 0, 1))))
    """).format(name=name, plane=plane, off=offset, body=body, support=support, doc=document))


@_tool()
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
        import json, Sketcher
        doc = _doc({doc!r})
        sk = _obj(doc, {sk!r}, "sketch")
        pts = json.loads({pts!r})
        if len(pts) < 2:
            raise _Fail("need at least two points")
        base = len(sk.Geometry)
        segs = []
        n = len(pts)
        last = n if {closed!r} else n - 1
        for i in range(last):
            a = pts[i]
            b = pts[(i + 1) % n]
            if abs(a[0] - b[0]) > 1e-9 or abs(a[1] - b[1]) > 1e-9:
                segs.append(_Part.LineSegment(App.Vector(a[0], a[1], 0),
                                              App.Vector(b[0], b[1], 0)))
        sk.addGeometry(segs, {constr!r})
        made = len(segs)
        for i in range(made if {closed!r} else made - 1):
            sk.addConstraint(Sketcher.Constraint(
                "Coincident", base + i, 2, base + (i + 1) % made, 1))
        doc.recompute()
        sk.solve()
        print("added", made, "segments to", sk.Name, "as geometry", base, "..", base + made - 1)
        print("geometry:", len(sk.Geometry), " constraints:", len(sk.Constraints))
        print("open vertices (should be 0 for a closed profile):",
              len(sk.OpenVertices) if hasattr(sk, "OpenVertices") else "n/a")
        print("fully constrained:", sk.FullyConstrained)
    """).format(sk=sketch, pts=points, closed=closed,
                constr=construction, doc=document))


@_tool()
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
        doc = _doc({doc!r})
        sk = _obj(doc, {sk!r}, "sketch")
        idx = sk.addGeometry(_Part.Circle(App.Vector({x!r}, {y!r}, 0),
                                          App.Vector(0, 0, 1), {r!r}), {constr!r})
        doc.recompute()
        print("circle r={r!r} at ({x!r}, {y!r}) is geometry", idx, "; geometry now", len(sk.Geometry))
    """).format(sk=sketch, x=x, y=y, r=radius, constr=construction, doc=document))


@_tool(read_only=True, idempotent=True)
def sketch_info(sketch: str, document: str = "") -> str:
    """Report a sketch's geometry, constraints, closure and degrees of freedom.

    An unclosed profile is the usual reason a Pad produces nothing. Check this
    before padding rather than after.

    Args:
        sketch: Sketch object name.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        sk = _obj(doc, {sk!r}, "sketch")
        sk.solve()
        print("sketch          :", sk.Name)
        print("geometry        :", len(sk.Geometry))
        print("constraints     :", len(sk.Constraints))
        print("fully constrained:", sk.FullyConstrained)
        try:
            print("open vertices   :", len(sk.OpenVertices))
        except Exception:
            pass
        if sk.Shape.isNull():
            print("shape           : null (no geometry yet)")
        else:
            bb = sk.Shape.BoundBox
            print("wires / faces   :", len(sk.Shape.Wires), "/", len(sk.Shape.Faces))
            print("bbox            : {{:.4f}} x {{:.4f}} mm".format(bb.XLength, bb.YLength))
            closed = all(w.isClosed() for w in sk.Shape.Wires) if sk.Shape.Wires else False
            print("all wires closed:", closed,
                  "" if closed else "  <-- a Pad will fail or produce nothing")
    """).format(sk=sketch, doc=document))


@_tool()
def partdesign_feature(
    body: str,
    sketch: str,
    kind: str = "pad",
    length: float = 10.0,
    reversed_dir: bool = False,
    side_type: str = "One side",
    type: str = "Length",
    length2: float = 0.0,
    up_to_face: str = "",
    document: str = "",
) -> str:
    """Add a PartDesign Pad or Pocket to a Body from a sketch.

    Use this rather than Part booleans. PartDesign builds an editable feature
    tree; Part::Cut/Fuse build a dependency graph that is fragile to modify.

    A sketch drawn on the body's outer surface almost always needs
    reversed_dir=True for a Pocket, or it cuts away from the material.

    The Body's volume before and after is reported. A feature that leaves
    it unchanged did nothing -- usually the direction or the side.

    Args:
        body: PartDesign Body name.
        sketch: Profile sketch name.
        kind: "pad" or "pocket".
        length: Depth in mm. Bind it to a spreadsheet alias afterwards with
            set_expression to keep the model parametric.
        reversed_dir: Reverse the feature direction.
        side_type: "One side", "Two sides" or "Symmetric" (about the sketch
            plane). Replaces the deprecated Midplane flag.
        type: "Length", "ThroughAll" (pocket) / "UpToLast" (pad), "UpToFirst",
            "UpToFace", "TwoLengths" or "UpToShape".
        length2: Second length, used by type="TwoLengths".
        up_to_face: Face for type="UpToFace", as "Object:FaceN".
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        bd = _obj(doc, {body!r}, "body")
        sk = _obj(doc, {sk!r}, "sketch")
        if bd.TypeId != "PartDesign::Body":
            raise _Fail("%s is a %s, not a PartDesign::Body" % (bd.Name, bd.TypeId))
        kinds = {{"pad": "PartDesign::Pad", "pocket": "PartDesign::Pocket"}}
        if {kind!r} not in kinds:
            raise _Fail("kind must be pad or pocket")
        if sk.getParentGeoFeatureGroup() is not None and sk.getParentGeoFeatureGroup() != bd:
            raise _Fail("%s belongs to %s, not %s" % (sk.Name, sk.getParentGeoFeatureGroup().Name, bd.Name))
        vol0 = _volume(bd)
        f = doc.addObject(kinds[{kind!r}], {kind!r} + "_" + sk.Name)
        bd.addObject(f)
        f.Profile = sk

        enum = [e.lstrip("?") for e in f.getEnumerationsOfProperty("Type")]
        want = {type!r}.replace(" ", "").lower()
        synonyms = {{"throughall": ("ThroughAll", "UpToLast"), "uptolast": ("UpToLast", "ThroughAll")}}
        chosen = None
        for cand in synonyms.get(want, ()) + tuple(enum):
            if cand.lower() == want or (want in synonyms and cand in synonyms[want]):
                if cand in enum:
                    chosen = cand
                    break
        if chosen is None:
            raise _Fail("type must be one of %s" % enum)
        f.Type = chosen
        f.Length = {length!r}
        if chosen == "TwoLengths":
            f.Length2 = {length2!r}
        if chosen == "UpToFace":
            if not {utf!r}:
                raise _Fail("type=UpToFace needs up_to_face='Object:FaceN'")
            oname, _, face = {utf!r}.replace(".", ":").partition(":")
            f.UpToFace = (_obj(doc, oname), [face])
        f.Reversed = {rev!r}

        senum = f.getEnumerationsOfProperty("SideType")
        swant = {side!r}.replace(" ", "").lower()
        smap = {{"oneside": "One side", "one": "One side", "twosides": "Two sides", "two": "Two sides",
                 "symmetric": "Symmetric", "midplane": "Symmetric"}}
        side = smap.get(swant)
        if side is None or side not in senum:
            raise _Fail("side_type must be one of %s" % senum)
        f.SideType = side

        doc.recompute()
        vol1 = _volume(bd)
        s = _shape_of(bd)
        print("created", f.Name, "on", bd.Name, " Tip:", bd.Tip.Name if bd.Tip else None)
        print("Type:", f.Type, " SideType:", f.SideType, " Length:", f.Length, " Reversed:", f.Reversed)
        print(_state_str(f))
        print("body is a solid:", bool(s is not None and s.Solids))
        print("body volume: {{:.4f}} -> {{:.4f}} mm3 (delta {{:+.4f}})".format(vol0, vol1, vol1 - vol0))
        if abs(vol1 - vol0) < 1e-6 and f.isValid():
            print("WARNING: the body volume did not change -- the feature cut or added nothing."
                  " Check reversed_dir, side_type and whether the profile overlaps material.")
        if s is not None and s.Solids:
            print("body bbox:", _bbox_str(s))
    """).format(body=body, sk=sketch, kind=kind, length=length, rev=reversed_dir,
                side=side_type, type=type, length2=length2, utf=up_to_face,
                doc=document))


@_tool()
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

    The axis or mirror plane comes from the Body's own origin, the pattern
    becomes the Body's Tip, and the volume change is reported.

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
        doc = _doc({doc!r})
        bd = _obj(doc, {body!r}, "body")
        ft = _obj(doc, {feat!r}, "feature")
        if bd.TypeId != "PartDesign::Body":
            raise _Fail("%s is a %s, not a PartDesign::Body" % (bd.Name, bd.TypeId))
        if ft.getParentGeoFeatureGroup() != bd:
            raise _Fail("%s is not a feature of %s" % (ft.Name, bd.Name))
        if {kind!r} not in ("linear", "polar", "mirrored"):
            raise _Fail("kind must be linear, polar or mirrored")
        dirn = {dirn!r}.upper()
        if dirn not in ("X", "Y", "Z"):
            raise _Fail("direction must be X, Y or Z")
        # Resolve the reference before creating anything, so a bad lookup
        # cannot leave an orphan pattern in the body.
        if {kind!r} == "mirrored":
            ref = _origin_feature(bd, {{"X": "YZ_Plane", "Y": "XZ_Plane", "Z": "XY_Plane"}}[dirn])
        else:
            ref = _origin_feature(bd, dirn + "_Axis")
        vol0 = _volume(bd)
        if {kind!r} == "mirrored":
            p = doc.addObject("PartDesign::Mirrored", "Mirrored")
            bd.addObject(p)
            p.Originals = [ft]
            p.MirrorPlane = (ref, [""])
        elif {kind!r} == "polar":
            p = doc.addObject("PartDesign::PolarPattern", "PolarPattern")
            bd.addObject(p)
            p.Originals = [ft]
            p.Axis = (ref, [""])
            p.Angle = {length!r}
            p.Occurrences = {count!r}
        else:
            p = doc.addObject("PartDesign::LinearPattern", "LinearPattern")
            bd.addObject(p)
            p.Originals = [ft]
            p.Direction = (ref, [""])
            p.Length = {length!r}
            p.Occurrences = {count!r}
        bd.Tip = p
        doc.recompute()
        vol1 = _volume(bd)
        print("created", p.Name, "-", {kind!r}, "x", {count!r}, "along", dirn,
              "using", ref.Name, " Tip:", bd.Tip.Name)
        print(_state_str(p))
        print("body volume: {{:.4f}} -> {{:.4f}} mm3 (delta {{:+.4f}})".format(vol0, vol1, vol1 - vol0))
        if abs(vol1 - vol0) < 1e-6 and p.isValid():
            print("WARNING: the body volume did not change -- the pattern instances may"
                  " coincide with the original or fall outside the body.")
        s = _shape_of(bd)
        if s is not None and s.Solids:
            print("body bbox:", _bbox_str(s))
    """).format(body=body, feat=feature, kind=kind, count=count,
                length=length, dirn=direction, doc=document))


@_tool(idempotent=True)
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
        doc = _doc({doc!r})
        o = _obj(doc, {name!r})
        plc = App.Placement(
            App.Vector({x!r}, {y!r}, {z!r}),
            App.Rotation(App.Vector({ax!r}, {ay!r}, {az!r}), {ang!r}))
        if o.TypeId == "App::Link":
            o.LinkPlacement = plc
        elif "Placement" in o.PropertiesList:
            o.Placement = plc
        else:
            raise _Fail("%s (%s) has no Placement" % (o.Name, o.TypeId))
        doc.recompute()
        print("placed", o.Name, "at", plc.Base, "rot", {ang!r}, "deg")
        s = _shape_of(o)
        if s is not None:
            print("resulting bbox:", _bbox_str(s))
    """).format(name=object_name, x=x, y=y, z=z, ax=axis_x, ay=axis_y,
                az=axis_z, ang=angle, doc=document))


@_tool()
def create_assembly(name: str = "Assembly", document: str = "") -> str:
    """Create an Assembly container (falls back to App::Part if unavailable).

    Args:
        name: Name for the assembly.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        try:
            a = doc.addObject("Assembly::AssemblyObject", {name!r})
            kind = "Assembly::AssemblyObject"
        except Exception:
            a = doc.addObject("App::Part", {name!r})
            kind = "App::Part"
        doc.recompute()
        print("created", a.Name, "as", kind)
    """).format(name=name, doc=document))


@_tool()
def add_link(
    assembly: str,
    source: str,
    name: str = "",
    count: int = 0,
    show_elements: bool = False,
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
        count: Make a link array of this many elements instead of a single
            link (0 for a plain link). Position elements through
            PlacementList, or set show_elements to place each one.
        show_elements: Expose each array element as its own child object.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        asm = _obj(doc, {asm!r}, "assembly")
        src = _obj(doc, {src!r}, "source")
        l = doc.addObject("App::Link", {name!r} or ("L_" + src.Name))
        l.LinkedObject = src
        if {count!r} > 0:
            l.ElementCount = {count!r}
            l.ShowElement = {show!r}
        asm.addObject(l)
        doc.recompute()
        print("linked", src.Name, "into", asm.Name, "as", l.Name,
              ("(array of %d)" % {count!r}) if {count!r} > 0 else "")
        print("children now:", len(asm.Group) if hasattr(asm, "Group") else "?")
        if _shape_of(src) is None:
            print("WARNING:", src.Name, "has no shape, so the link renders nothing")
    """).format(asm=assembly, src=source, name=name, count=count,
                show=show_elements, doc=document))


@_tool(destructive=True)
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
        doc = _doc({doc!r})
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
            raise _Fail("action must be begin, commit or abort")
    """).format(act=action, name=name, doc=document))


@_tool(destructive=True)
def undo(document: str = "") -> str:
    """Undo the most recent transaction and report the undo and redo stacks.

    Only edits made inside a transaction are undoable. The convenience tools
    do not open one, so wrap a batch with transaction("begin") first.

    Args:
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        before = list(doc.UndoNames)
        if not before:
            print("nothing to undo")
        else:
            doc.undo()
            doc.recompute()
            print("undid:", before[0])
        print("undo stack:", list(doc.UndoNames))
        print("redo stack:", list(doc.RedoNames))
    """).format(doc=document))


@_tool(destructive=True)
def redo(document: str = "") -> str:
    """Redo the most recently undone transaction and report both stacks.

    Args:
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        before = list(doc.RedoNames)
        if not before:
            print("nothing to redo")
        else:
            doc.redo()
            doc.recompute()
            print("redid:", before[0])
        print("undo stack:", list(doc.UndoNames))
        print("redo stack:", list(doc.RedoNames))
    """).format(doc=document))


@_tool()
def create_document(name: str = "Unnamed") -> str:
    """Create a new empty FreeCAD document.

    Args:
        name: Name for the new document.
    """
    return _call("doc = App.newDocument({!r})\nprint('Created document:', doc.Name)".format(name))


@_tool()
def open_document(file_path: str) -> str:
    """Open a FreeCAD document file (.FCStd).

    Args:
        file_path: Absolute path to the .FCStd file.
    """
    return _call(textwrap.dedent("""\
        if not _os.path.exists({path!r}):
            raise _Fail("no such file: %s" % {path!r})
        doc = App.openDocument({path!r})
        print("Opened:", doc.Name, "({{}})".format(doc.FileName))
        print("Objects:", len(doc.Objects))
    """).format(path=file_path))


@_tool(idempotent=True)
def save_document(file_path: str = "", document: str = "") -> str:
    """Save a FreeCAD document.

    Args:
        file_path: Path to save to. Empty to save in-place.
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        path = {path!r}
        if path:
            doc.saveAs(path)
            print("Saved as:", path)
        else:
            if not doc.FileName:
                raise _Fail("document has never been saved; pass file_path")
            doc.save()
            print("Saved:", doc.FileName)
        print("size:", _os.path.getsize(doc.FileName), "bytes")
    """).format(path=file_path, doc=document))


@_tool(destructive=True)
def close_document(document: str = "") -> str:
    """Close a FreeCAD document without saving.

    Args:
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        name = doc.Name
        App.closeDocument(name)
        print("Closed document:", name)
    """).format(doc=document))


@_tool()
def add_object(
    type_id: str,
    name: str,
    body: str = "",
    document: str = "",
) -> str:
    """Add a new object to a FreeCAD document, or into a PartDesign Body.

    Common type_ids:
      Part::Box, Part::Cylinder, Part::Sphere, Part::Cone, Part::Torus,
      Part::Extrusion, Part::Feature, Mesh::Feature, Spreadsheet::Sheet,
      Sketcher::SketchObject, PartDesign::Body

    A PartDesign feature (PartDesign::Pad, PartDesign::Pocket, ...) only
    works inside a Body, so those require the body argument. For a Pad or
    Pocket from a sketch, partdesign_feature is the better tool.

    Args:
        type_id: FreeCAD object type (e.g. "Part::Box").
        name: Name for the new object.
        body: PartDesign Body to add the object into. Required for
            PartDesign features; optional for sketches and datums.
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        if doc is None:
            doc = App.newDocument("Unnamed")
        tid = {type_id!r}
        bd = _obj(doc, {body!r}, "body") if {body!r} else None
        if bd is not None and bd.TypeId != "PartDesign::Body":
            raise _Fail("%s is a %s, not a PartDesign::Body" % (bd.Name, bd.TypeId))
        is_pd_feature = tid.startswith("PartDesign::") and tid != "PartDesign::Body"
        if is_pd_feature and bd is None:
            raise _Fail("%s only works inside a Body; pass body=<Body name>" % tid)
        try:
            obj = doc.addObject(tid, {name!r})
        except Exception as e:
            raise _Fail("cannot create %r: %s" % (tid, e))
        if bd is not None:
            bd.addObject(obj)
        doc.recompute()
        print("Created", obj.Name, "(" + obj.TypeId + ")",
              ("in " + bd.Name + "; Tip now " + (bd.Tip.Name if bd.Tip else "None")) if bd else "")
    """).format(type_id=type_id, name=name, body=body, doc=document))


@_tool(destructive=True)
def remove_object(object_name: str, document: str = "") -> str:
    """Remove an object from a FreeCAD document.

    Args:
        object_name: The internal Name of the object to remove.
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        o = _obj(doc, {name!r})
        dependents = [d.Name for d in o.InList]
        # Removing a Body's Tip leaves the Body with no Tip and therefore no
        # shape. Hand the Tip back to the previous feature first.
        parent = o.getParentGeoFeatureGroup()
        new_tip = None
        if parent is not None and parent.TypeId == "PartDesign::Body" and parent.Tip == o:
            new_tip = getattr(o, "BaseFeature", None)
            parent.Tip = new_tip
        doc.removeObject(o.Name)
        doc.recompute()
        print("Removed:", {name!r})
        if parent is not None and parent.TypeId == "PartDesign::Body" and new_tip is not None:
            print("Tip of", parent.Name, "is now", new_tip.Name)
        if dependents:
            print("objects that referenced it:", dependents)
    """).format(name=object_name, doc=document))


@_tool(read_only=True, idempotent=True)
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


@_tool(idempotent=True)
def recompute(document: str = "") -> str:
    """Recompute (rebuild) all objects in a FreeCAD document.

    Args:
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        touched = doc.recompute()
        print("Recomputed", touched if isinstance(touched, int) else len(touched or []),
              "objects in", doc.Name)
        bad = [o.Name for o in doc.Objects if [t for t in o.State if t in ("Invalid", "Error")]]
        if bad:
            print("objects in error:", bad)
    """).format(doc=document))


@_tool(idempotent=True)
def export_object(
    object_names: str,
    file_path: str,
    deflection: float = 0.1,
    document: str = "",
) -> str:
    """Export one or more objects to a file; the extension picks the format.

    STEP / IGES go through the colour-preserving exporter. BREP writes the
    exact shape. STL, OBJ, PLY, 3MF, AMF, OFF, WRL and X3D are tessellated
    at the given deflection. GLB / GLTF go through FreeCAD's glTF writer.
    App::Link objects export as their positioned shape.

    The file is checked to exist and be non-empty before success is reported.

    Args:
        object_names: Comma-separated list of object names to export.
        file_path: Output file path with extension (e.g. "/tmp/part.step").
        deflection: Mesh chord tolerance in mm for the mesh formats.
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        path = {path!r}
        ext = _os.path.splitext(path)[1].lower().lstrip(".")
        objs = [_obj(doc, n) for n in _names({names!r})]
        if not objs:
            raise _Fail("no object names given")
        if _os.path.exists(path):
            _os.remove(path)
        detail = ""
        if ext in {cad!r} or ext in {gltf!r}:
            import ImportGui
            ImportGui.export(objs, path)
            how = "ImportGui.export"
        elif ext in {brep!r}:
            shapes = [s for s in (_shape_of(o) for o in objs) if s is not None]
            if not shapes:
                raise _Fail("none of %s has a shape" % [o.Name for o in objs])
            comp = shapes[0] if len(shapes) == 1 else _Part.makeCompound(shapes)
            comp.exportBrep(path)
            how = "Shape.exportBrep"
            detail = "bbox " + _bbox_str(comp)
        elif ext == "amf":
            # AMF has no Mesh.write backend; only the object exporter writes it.
            import Mesh
            Mesh.export(objs, path, {defl!r})
            how = "Mesh.export"
        elif ext in {mesh!r}:
            import Mesh, MeshPart
            merged = Mesh.Mesh()
            for o in objs:
                if o.TypeId == "Mesh::Feature" or "Mesh" in o.PropertiesList:
                    m = o.Mesh.copy()
                    m.transform(m.Placement.toMatrix())
                    m.Placement = App.Placement()
                    merged.addMesh(m)
                    continue
                s = _shape_of(o)
                if s is None:
                    raise _Fail("%s (%s) has no shape to tessellate" % (o.Name, o.TypeId))
                merged.addMesh(MeshPart.meshFromShape(
                    Shape=s, LinearDeflection={defl!r}, AngularDeflection=0.5, Relative=False))
            if merged.CountFacets == 0:
                raise _Fail("tessellation produced no facets")
            merged.write(path)
            how = "MeshPart.meshFromShape + Mesh.write"
            detail = "%d facets, bbox %s" % (merged.CountFacets, _bbox_str(merged))
        else:
            raise _Fail("unsupported extension %r; use one of %s" % (
                ext, sorted({cad!r} + {brep!r} + {gltf!r} + {mesh!r})))
        if not _os.path.exists(path) or _os.path.getsize(path) == 0:
            raise _Fail("%s reported success but %s is missing or empty" % (how, path))
        print("exported", len(objs), "object(s) to", path, "via", how)
        print("size:", _os.path.getsize(path), "bytes", detail)
    """).format(names=object_names, path=file_path, defl=deflection, doc=document,
                cad=_CAD_EXTS, brep=_BREP_EXTS, gltf=_GLTF_EXTS, mesh=_MESH_EXTS))


@_tool(idempotent=True)
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
        doc = _doc({doc!r})
        names = {names!r}
        objs = doc.Objects if names.strip() == "*" else [_obj(doc, n) for n in _names(names)]
        vis = {vis!r}
        done = 0
        for o in objs:
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


@_tool(read_only=True, idempotent=True)
def measure(
    object_a: str,
    object_b: str = "",
    document: str = "",
) -> str:
    """Measure an object, or the distance and overlap between two.

    Use this to VERIFY a model rather than trusting that it built correctly.
    With one object you get its bounding box and volume; with two you also
    get the minimum distance between them and their intersection volume.

    An intersection volume above zero on parts that should merely touch means
    they interfere. A distance above zero on parts that should mate means a gap.

    Args:
        object_a: First object name.
        object_b: Optional second object name.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        IN = 25.4
        doc = _doc({doc!r})
        def measurable(name):
            o = _obj(doc, name)
            s = _shape_of(o)
            if s is None:
                raise _Fail("%s is a %s and has no shape to measure" % (o.Name, o.TypeId))
            return o, s
        a, sa = measurable({a!r})
        bb = sa.BoundBox
        print("{{}}: {{:.4f}} x {{:.4f}} x {{:.4f}} mm  ({{:.4f}} x {{:.4f}} x {{:.4f}} in)".format(
            a.Name, bb.XLength, bb.YLength, bb.ZLength,
            bb.XLength/IN, bb.YLength/IN, bb.ZLength/IN))
        print("  origin ({{:.4f}}, {{:.4f}}, {{:.4f}}) mm   volume {{:.4f}} mm3 ({{:.4f}} in3)".format(
            bb.XMin, bb.YMin, bb.ZMin, sa.Volume, sa.Volume/IN**3))
        print("  solids {{}}  faces {{}}  edges {{}}".format(len(sa.Solids), len(sa.Faces), len(sa.Edges)))
        if {b!r}:
            bo, sb = measurable({b!r})
            dist = sa.distToShape(sb)[0]
            common = sa.common(sb).Volume
            print("  distance to {{}}: {{:.6f}} mm ({{:.6f}} in)".format(
                bo.Name, dist, dist/IN))
            print("  intersection   : {{:.6f}} mm3 ({{:.6f}} in3){{}}".format(
                common, common/IN**3, "  <-- INTERFERENCE" if common > 1e-3 else ""))
    """).format(a=object_a, b=object_b, doc=document))


@_tool(read_only=True, idempotent=True)
def check_interference(object_names: str = "*", document: str = "") -> str:
    """Pairwise interference check across many objects.

    Reports every pair whose solids actually overlap. Bounding boxes are used to
    skip non-touching pairs, so this stays usable on assemblies with dozens of
    parts. Run it before believing an assembly is correct -- coincident
    placement numbers do not prove the solids mate.

    With "*", features inside a Body (pads, pockets, patterns) and container
    objects are skipped: the Body's own shape already includes them, and
    listing them would report a Body interfering with itself.

    Args:
        object_names: Comma-separated names, or "*" for all links and solids.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        IN = 25.4
        doc = _doc({doc!r})
        names = {names!r}
        if names.strip() == "*":
            objs = []
            for o in doc.Objects:
                if o.TypeId in ("App::Origin", "App::Line", "App::Plane", "App::Point",
                                "App::Part", "Assembly::AssemblyObject"):
                    continue
                parent = o.getParentGeoFeatureGroup()
                if parent is not None and parent.TypeId == "PartDesign::Body":
                    continue
                if o.TypeId == "App::Link":
                    objs.append(o)
                    continue
                s = getattr(o, "Shape", None)
                if s is not None and not s.isNull() and s.Solids:
                    objs.append(o)
        else:
            objs = [_obj(doc, n) for n in _names(names)]
        shp = []
        for o in objs:
            s = _shape_of(o)
            if s is None:
                print("  skipping", o.Name, "(no shape)")
            else:
                shp.append((o.Name, s))
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
                    print("  INTERFERENCE {{}} x {{}}: {{:.4f}} mm3 ({{:.6f}} in3)".format(n1, n2, v, v/IN**3))
        print("checked {{}} object(s) {{}}, {{}} overlapping bbox pair(s), {{}} real interference(s)".format(
            len(shp), [n for n, _ in shp], pairs, hits))
    """).format(names=object_names, doc=document))


@_tool(read_only=True, idempotent=True)
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
        doc = _doc({doc!r})
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


@_tool(idempotent=True)
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
        doc = _doc({doc!r})
        if {units!r} not in ("in", "mm"):
            raise _Fail("units must be 'in' or 'mm'")
        scale = 25.4 if {units!r} == "in" else 1.0
        insunits = 1 if {units!r} == "in" else 4
        objs = [_obj(doc, n) for n in _names({names!r})]

        segs = []
        fell_back = 0
        for o in objs:
            sh = _shape_of(o)
            if sh is None:
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
            raise _Fail("no edges found on %s" % [o.Name for o in objs])
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
        print("exported", {path!r}, _os.path.getsize({path!r}), "bytes")
        print("  {{}} LINE entities from {{}} object(s)".format(len(segs), len(objs)))
        print("  extents {{:.4f}} x {{:.4f}} {{}}".format(
            max(xs) - min(xs), max(ys) - min(ys), {units!r}))
        if fell_back:
            print("  WARNING: {{}} edge(s) could not be discretised and were "
                  "emitted as a single straight chord. Any curve among them "
                  "is WRONG in this file.".format(fell_back))
    """).format(names=object_names, path=file_path, units=units,
                dev=deviation, doc=document))


@_tool(idempotent=True)
def capture_screenshot(
    file_path: str,
    width: int = 1920,
    height: int = 1080,
    method: str = "auto",
    view: str = "",
    fit: bool = False,
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
        view: Optional direction to set first: iso, front, top, right, left,
            rear, bottom, or "x,y,z".
        fit: Fit the view to everything visible before capturing.
    """
    if view or fit:
        pre = _call(_VIEW_CODE + "_apply_view(_view3d(), {!r}, {!r})\n".format(view, fit))
        if pre.startswith("ERROR") or "[error]" in pre:
            return pre
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


@_tool(idempotent=True)
def fit_view() -> str:
    """Fit the 3D view to show all objects."""
    return _call("Gui.SendMsgToActiveView('ViewFit')\nprint('View fitted')")


@_tool()
def import_file(file_path: str, document: str = "") -> str:
    """Import a file into a document; the extension picks the importer.

    STEP / IGES / BREP come in as shapes (with colours), STL / OBJ / PLY /
    3MF / AMF / OFF as meshes, SVG and DXF as 2D geometry, and an .FCStd is
    merged into the document. The names of the objects added are reported;
    an import that added nothing is a failure, not a success.

    Args:
        file_path: Path to the file to import.
        document: Document name. Empty string for active document.
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        if doc is None:
            doc = App.newDocument("Imported")
        path = {path!r}
        if not _os.path.exists(path):
            raise _Fail("no such file: %s" % path)
        ext = _os.path.splitext(path)[1].lower().lstrip(".")
        before = set(o.Name for o in doc.Objects)
        if ext in {cad!r}:
            import ImportGui
            ImportGui.insert(path, doc.Name)
            how = "ImportGui.insert"
        elif ext in {brep!r}:
            _Part.insert(path, doc.Name)
            how = "Part.insert"
        elif ext in {mesh!r}:
            import Mesh
            Mesh.insert(path, doc.Name)
            how = "Mesh.insert"
        elif ext == "svg":
            import importSVG
            importSVG.insert(path, doc.Name)
            how = "importSVG.insert"
        elif ext == "dxf":
            import importDXF
            importDXF.insert(path, doc.Name)
            how = "importDXF.insert"
        elif ext == "fcstd":
            doc.mergeProject(path)
            how = "Document.mergeProject"
        else:
            mods = App.getImportType(ext) if ext else []
            if not mods:
                raise _Fail("no importer registered for .%s" % ext)
            __import__(mods[0]).insert(path, doc.Name)
            how = mods[0] + ".insert"
        doc.recompute()
        added = [o for o in doc.Objects if o.Name not in before]
        print("imported", path, "into", doc.Name, "via", how)
        print("added", len(added), "object(s):", [(o.Name, o.TypeId) for o in added][:30])
        if not added:
            raise _Fail("the importer added no objects (empty or unreadable file?)")
    """).format(path=file_path, doc=document, cad=_CAD_EXTS, brep=_BREP_EXTS,
                mesh=_MESH_IN_EXTS))


# ── Sketch constraints and geometry ────────────────────────────────

_CONSTRAINT_KINDS = ("Coincident", "PointOnObject", "Horizontal", "Vertical", "Parallel",
                     "Perpendicular", "Tangent", "Equal", "Symmetric", "Distance",
                     "DistanceX", "DistanceY", "Radius", "Diameter", "Angle", "Block")
_DIMENSIONAL_KINDS = ("Distance", "DistanceX", "DistanceY", "Radius", "Diameter", "Angle")


@_tool()
def sketch_constrain(
    sketch: str,
    kind: str,
    geo_a: int,
    pos_a: int = 0,
    geo_b: Optional[int] = None,
    pos_b: int = 0,
    geo_c: Optional[int] = None,
    pos_c: int = 0,
    value: Optional[float] = None,
    name: str = "",
    document: str = "",
) -> str:
    """Add a constraint to a sketch, optionally named so an expression can drive it.

    Geometry is addressed by index (see sketch_list_geometry). Point positions:
    0 = the whole element, 1 = start, 2 = end, 3 = centre. The sketch axes are
    geometry -1 (horizontal) and -2 (vertical); the origin is point (-1, 1).

    Kinds: Coincident, PointOnObject, Horizontal, Vertical, Parallel,
    Perpendicular, Tangent, Equal, Symmetric, Distance, DistanceX, DistanceY,
    Radius, Diameter, Angle, Block. Lengths are in mm, Angle in degrees.

    Examples:
      Coincident endpoints:  geo_a=0, pos_a=2, geo_b=1, pos_b=1
      Horizontal line:       kind="Horizontal", geo_a=0
      Line length:           kind="Distance", geo_a=0, value=25, name="width"
      Point to line:         kind="Distance", geo_a=2, pos_a=1, geo_b=0, value=5
      Radius:                kind="Radius", geo_a=3, value=4, name="hole_r"
      Fix a point in X:      kind="DistanceX", geo_a=-1, pos_a=1, geo_b=0, pos_b=1, value=10
      Symmetric about origin: kind="Symmetric", geo_a=0, pos_a=1, geo_b=2, pos_b=1, geo_c=-1, pos_c=1

    A named dimensional constraint is driven with
    set_expression(sketch, "Constraints.<name>", "<sheet>.<alias>"), which is
    what makes a sketch parametric.

    If the new constraint makes the sketch conflicting, redundant or malformed
    it is removed again and the failure reported, so the sketch is never left
    broken.
    """
    return _call(textwrap.dedent("""\
        import math, Sketcher
        doc = _doc({doc!r})
        sk = _obj(doc, {sk!r}, "sketch")
        kinds = {{k.lower(): k for k in {kinds!r}}}
        kind = kinds.get({kind!r}.replace(" ", "").replace("_", "").lower())
        if kind is None:
            raise _Fail("kind must be one of %s" % list({kinds!r}))
        args = [kind, {ga!r}]
        if {pa!r}:
            args.append({pa!r})
        if {gb!r} is not None:
            args.append({gb!r})
            if {pb!r}:
                args.append({pb!r})
        if {gc!r} is not None:
            args.append({gc!r})
            if {pc!r}:
                args.append({pc!r})
        if {val!r} is not None:
            args.append(math.radians({val!r}) if kind == "Angle" else float({val!r}))
        elif kind in {dims!r}:
            raise _Fail("%s needs a value" % kind)
        try:
            idx = sk.addConstraint(Sketcher.Constraint(*args))
        except Exception as e:
            raise _Fail("FreeCAD rejected Constraint%s: %s" % (tuple(args), e))
        if {name!r}:
            sk.renameConstraint(idx, {name!r})
        code = sk.solve()
        problems = {{}}
        for attr in ("ConflictingConstraints", "RedundantConstraints",
                     "MalformedConstraints", "PartiallyRedundantConstraints"):
            lst = list(getattr(sk, attr, []) or [])
            if lst:
                problems[attr] = lst
        if code != 0 or problems:
            sk.delConstraint(idx)
            sk.solve()
            doc.recompute()
            raise _Fail("%s was removed again: solve() returned %d, %s" % (
                kind, code, problems or "solver failure"))
        doc.recompute()
        print("added", kind, "as constraint", idx, ("named " + {name!r}) if {name!r} else "(unnamed)")
        print("arguments:", tuple(args[1:]))
        print("DoF:", getattr(sk, "DoF", "?"), " fully constrained:", sk.FullyConstrained,
              " constraints:", len(sk.Constraints))
        if {name!r} and kind in {dims!r}:
            print("drive it with: set_expression(%r, 'Constraints.%s', '<sheet>.<alias>')" % (sk.Name, {name!r}))
    """).format(sk=sketch, kind=kind, ga=geo_a, pa=pos_a, gb=geo_b, pb=pos_b, gc=geo_c,
                pc=pos_c, val=value, name=name, doc=document, kinds=_CONSTRAINT_KINDS,
                dims=_DIMENSIONAL_KINDS))


@_tool()
def sketch_add_rectangle(
    sketch: str,
    x: float,
    y: float,
    width: float,
    height: float,
    centered: bool = False,
    constrain: bool = True,
    name_prefix: str = "",
    construction: bool = False,
    document: str = "",
) -> str:
    """Add a rectangle to a sketch as four lines, closed and (optionally) fully constrained.

    With constrain=True the sides get Horizontal/Vertical constraints, the
    width and height get dimensional constraints, and the position is fixed
    (corner at x, y; or centred on x, y). With a name_prefix the dimensions are
    named <prefix>_width, <prefix>_height, <prefix>_x and <prefix>_y so a
    spreadsheet can drive them through set_expression.

    Args:
        sketch: Sketch object name.
        x: Corner X (or centre X when centered), in mm.
        y: Corner Y (or centre Y when centered), in mm.
        width: Size along sketch X, in mm.
        height: Size along sketch Y, in mm.
        centered: Interpret x, y as the centre.
        constrain: Add the constraints described above.
        name_prefix: Name the dimensional constraints.
        construction: Make the rectangle construction geometry.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import Sketcher
        doc = _doc({doc!r})
        sk = _obj(doc, {sk!r}, "sketch")
        w, h = float({w!r}), float({h!r})
        if w <= 0 or h <= 0:
            raise _Fail("width and height must be positive")
        cx, cy = float({x!r}), float({y!r})
        x0, y0 = (cx - w / 2, cy - h / 2) if {centered!r} else (cx, cy)
        pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
        base = len(sk.Geometry)
        sk.addGeometry([_Part.LineSegment(App.Vector(pts[i][0], pts[i][1], 0),
                                          App.Vector(pts[(i + 1) % 4][0], pts[(i + 1) % 4][1], 0))
                        for i in range(4)], {constr!r})
        for i in range(4):
            sk.addConstraint(Sketcher.Constraint("Coincident", base + i, 2, base + (i + 1) % 4, 1))
        named = {{}}
        pre = {prefix!r}
        if {constrain!r}:
            sk.addConstraint(Sketcher.Constraint("Horizontal", base))
            sk.addConstraint(Sketcher.Constraint("Horizontal", base + 2))
            sk.addConstraint(Sketcher.Constraint("Vertical", base + 1))
            sk.addConstraint(Sketcher.Constraint("Vertical", base + 3))
            iw = sk.addConstraint(Sketcher.Constraint("DistanceX", base, w))
            ih = sk.addConstraint(Sketcher.Constraint("DistanceY", base + 1, h))
            if pre:
                sk.renameConstraint(iw, pre + "_width"); named["width"] = pre + "_width"
                sk.renameConstraint(ih, pre + "_height"); named["height"] = pre + "_height"
            if {centered!r}:
                if abs(cx) < 1e-9 and abs(cy) < 1e-9:
                    sk.addConstraint(Sketcher.Constraint("Symmetric", base, 1, base + 1, 2, -1, 1))
                else:
                    cp = sk.addGeometry(_Part.Point(App.Vector(cx, cy, 0)), True)
                    ix = sk.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, cp, 1, cx))
                    iy = sk.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, cp, 1, cy))
                    sk.addConstraint(Sketcher.Constraint("Symmetric", base, 1, base + 1, 2, cp, 1))
                    if pre:
                        sk.renameConstraint(ix, pre + "_x"); named["x"] = pre + "_x"
                        sk.renameConstraint(iy, pre + "_y"); named["y"] = pre + "_y"
            else:
                ix = sk.addConstraint(Sketcher.Constraint("DistanceX", -1, 1, base, 1, x0))
                iy = sk.addConstraint(Sketcher.Constraint("DistanceY", -1, 1, base, 1, y0))
                if pre:
                    sk.renameConstraint(ix, pre + "_x"); named["x"] = pre + "_x"
                    sk.renameConstraint(iy, pre + "_y"); named["y"] = pre + "_y"
        code = sk.solve()
        doc.recompute()
        print("rectangle %g x %g at (%g, %g)%s: geometry %d..%d in %s" % (
            w, h, cx, cy, " centred" if {centered!r} else "", base, base + 3, sk.Name))
        if named:
            print("named constraints:", named)
        print("solve:", code, " DoF:", getattr(sk, "DoF", "?"), " fully constrained:", sk.FullyConstrained)
    """).format(sk=sketch, x=x, y=y, w=width, h=height, centered=centered,
                constrain=constrain, prefix=name_prefix, constr=construction, doc=document))


@_tool()
def sketch_add_arc(
    sketch: str,
    cx: float,
    cy: float,
    radius: float,
    start_angle: float,
    end_angle: float,
    construction: bool = False,
    document: str = "",
) -> str:
    """Add a circular arc to a sketch, counter-clockwise from start_angle to end_angle.

    Args:
        sketch: Sketch object name.
        cx: Centre X in mm.
        cy: Centre Y in mm.
        radius: Radius in mm.
        start_angle: Start angle in degrees, measured from +X.
        end_angle: End angle in degrees. The arc runs counter-clockwise.
        construction: Mark as construction geometry.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import math
        doc = _doc({doc!r})
        sk = _obj(doc, {sk!r}, "sketch")
        a0, a1 = math.radians({a0!r}), math.radians({a1!r})
        while a1 <= a0:
            a1 += 2 * math.pi
        arc = _Part.ArcOfCircle(_Part.Circle(App.Vector({cx!r}, {cy!r}, 0), App.Vector(0, 0, 1), {r!r}), a0, a1)
        idx = sk.addGeometry(arc, {constr!r})
        doc.recompute()
        g = sk.Geometry[idx]
        print("arc r=%g at (%g, %g) from %g to %g deg is geometry %d" % (
            {r!r}, {cx!r}, {cy!r}, {a0!r}, math.degrees(a1), idx))
        print("start (pos 1):", g.StartPoint, " end (pos 2):", g.EndPoint, " centre (pos 3):", g.Center)
    """).format(sk=sketch, cx=cx, cy=cy, r=radius, a0=start_angle, a1=end_angle,
                constr=construction, doc=document))


@_tool()
def sketch_add_slot(
    sketch: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    construction: bool = False,
    document: str = "",
) -> str:
    """Add a slot (two semicircles joined by two lines) between two centres.

    The four pieces are tangent-constrained at their joints and the two arcs
    constrained equal, so the slot stays a slot when it is moved or resized.

    Args:
        sketch: Sketch object name.
        x1: First centre X in mm.
        y1: First centre Y in mm.
        x2: Second centre X in mm.
        y2: Second centre Y in mm.
        radius: Slot half-width in mm.
        construction: Mark as construction geometry.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import math, Sketcher
        doc = _doc({doc!r})
        sk = _obj(doc, {sk!r}, "sketch")
        r = float({r!r})
        c1, c2 = App.Vector({x1!r}, {y1!r}, 0), App.Vector({x2!r}, {y2!r}, 0)
        d = c2 - c1
        if d.Length < 1e-9 or r <= 0:
            raise _Fail("the two centres must differ and radius must be positive")
        th = math.atan2(d.y, d.x)
        n = App.Vector(-math.sin(th), math.cos(th), 0)
        base = len(sk.Geometry)
        arc1 = _Part.ArcOfCircle(_Part.Circle(c1, App.Vector(0, 0, 1), r), th + math.pi / 2, th + 3 * math.pi / 2)
        arc2 = _Part.ArcOfCircle(_Part.Circle(c2, App.Vector(0, 0, 1), r), th - math.pi / 2, th + math.pi / 2)
        l1 = _Part.LineSegment(c1 + n * r, c2 + n * r)
        l2 = _Part.LineSegment(c2 - n * r, c1 - n * r)
        sk.addGeometry([arc1, l1, arc2, l2], {constr!r})
        a1, i1, a2, i2 = base, base + 1, base + 2, base + 3
        sk.addConstraint(Sketcher.Constraint("Tangent", a1, 1, i1, 1))
        sk.addConstraint(Sketcher.Constraint("Tangent", i1, 2, a2, 2))
        sk.addConstraint(Sketcher.Constraint("Tangent", a2, 1, i2, 1))
        sk.addConstraint(Sketcher.Constraint("Tangent", i2, 2, a1, 2))
        sk.addConstraint(Sketcher.Constraint("Equal", a1, a2))
        code = sk.solve()
        doc.recompute()
        print("slot r=%g from (%g, %g) to (%g, %g): arcs %d, %d  lines %d, %d" % (
            r, c1.x, c1.y, c2.x, c2.y, a1, a2, i1, i2))
        print("solve:", code, " geometry now:", len(sk.Geometry), " constraints:", len(sk.Constraints))
    """).format(sk=sketch, x1=x1, y1=y1, x2=x2, y2=y2, r=radius, constr=construction, doc=document))


@_tool()
def sketch_add_polygon(
    sketch: str,
    cx: float,
    cy: float,
    radius: float,
    sides: int = 6,
    rotation: float = 0.0,
    construction: bool = False,
    document: str = "",
) -> str:
    """Add a regular polygon inscribed in a circle, constrained to stay regular.

    A construction circle carries the vertices (PointOnObject) and the sides
    are constrained Equal, the same way the Sketcher GUI builds one.

    Args:
        sketch: Sketch object name.
        cx: Centre X in mm.
        cy: Centre Y in mm.
        radius: Circumscribed radius in mm.
        sides: Number of sides (3 or more).
        rotation: Rotation of the first vertex from +X, in degrees.
        construction: Mark the polygon as construction geometry.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import math, Sketcher
        doc = _doc({doc!r})
        sk = _obj(doc, {sk!r}, "sketch")
        n = int({n!r})
        if n < 3:
            raise _Fail("sides must be 3 or more")
        r = float({r!r})
        c = App.Vector({cx!r}, {cy!r}, 0)
        pts = [c + App.Vector(r * math.cos(math.radians({rot!r}) + 2 * math.pi * i / n),
                              r * math.sin(math.radians({rot!r}) + 2 * math.pi * i / n), 0)
               for i in range(n)]
        base = len(sk.Geometry)
        sk.addGeometry([_Part.LineSegment(pts[i], pts[(i + 1) % n]) for i in range(n)], {constr!r})
        circ = sk.addGeometry(_Part.Circle(c, App.Vector(0, 0, 1), r), True)
        for i in range(n):
            sk.addConstraint(Sketcher.Constraint("Coincident", base + i, 2, base + (i + 1) % n, 1))
            sk.addConstraint(Sketcher.Constraint("PointOnObject", base + i, 1, circ))
            if i:
                sk.addConstraint(Sketcher.Constraint("Equal", base, base + i))
        code = sk.solve()
        doc.recompute()
        print("%d-gon r=%g at (%g, %g): sides %d..%d, construction circle %d" % (
            n, r, c.x, c.y, base, base + n - 1, circ))
        print("solve:", code, " geometry now:", len(sk.Geometry), " constraints:", len(sk.Constraints))
    """).format(sk=sketch, cx=cx, cy=cy, r=radius, n=sides, rot=rotation,
                constr=construction, doc=document))


@_tool(destructive=True)
def sketch_delete_geometry(sketch: str, indices: str, document: str = "") -> str:
    """Delete geometry from a sketch by index; constraints on it go with it.

    Args:
        sketch: Sketch object name.
        indices: JSON list of geometry indices, e.g. "[3, 4]".
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import json
        doc = _doc({doc!r})
        sk = _obj(doc, {sk!r}, "sketch")
        idx = sorted(set(int(i) for i in json.loads({idx!r})), reverse=True)
        n = len(sk.Geometry)
        bad = [i for i in idx if i < 0 or i >= n]
        if bad:
            raise _Fail("no geometry at %s (sketch has %d)" % (bad, n))
        c0 = len(sk.Constraints)
        for i in idx:
            sk.delGeometry(i)
        sk.solve()
        doc.recompute()
        print("deleted geometry", sorted(idx), "from", sk.Name)
        print("geometry now:", len(sk.Geometry), " constraints:", len(sk.Constraints),
              "(%d removed)" % (c0 - len(sk.Constraints)))
        print("NOTE: indices above the deleted ones have shifted down")
    """).format(sk=sketch, idx=indices, doc=document))


@_tool(read_only=True, idempotent=True)
def sketch_list_geometry(sketch: str, document: str = "") -> str:
    """List a sketch's geometry and constraints with the indices the other tools use.

    Args:
        sketch: Sketch object name.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import math
        doc = _doc({doc!r})
        sk = _obj(doc, {sk!r}, "sketch")
        def v(p):
            return "(%.4g, %.4g)" % (p.x, p.y)
        print("geometry (%d):" % len(sk.Geometry))
        for i, g in enumerate(sk.Geometry):
            t = g.TypeId.replace("Part::Geom", "")
            try:
                cons = sk.getConstruction(i)
            except Exception:
                cons = getattr(g, "Construction", False)
            if t == "LineSegment":
                d = "%s -> %s  len %.4g" % (v(g.StartPoint), v(g.EndPoint), g.length())
            elif t == "Circle":
                d = "centre %s  r %.4g" % (v(g.Center), g.Radius)
            elif t == "ArcOfCircle":
                d = "centre %s  r %.4g  %s -> %s (%.1f..%.1f deg)" % (
                    v(g.Center), g.Radius, v(g.StartPoint), v(g.EndPoint),
                    math.degrees(g.FirstParameter), math.degrees(g.LastParameter))
            elif t == "Point":
                d = "(%.4g, %.4g)" % (g.X, g.Y)
            else:
                d = ""
            print("  %3d  %-14s %s%s" % (i, t, d, "  [construction]" if cons else ""))
        print("constraints (%d):" % len(sk.Constraints))
        for i, c in enumerate(sk.Constraints):
            refs = []
            for g, p in ((c.First, c.FirstPos), (c.Second, c.SecondPos), (c.Third, c.ThirdPos)):
                if g != -2000:
                    refs.append("%d.%d" % (g, p) if p else str(g))
            val = ""
            if c.Type in {dims!r}:
                val = "  = %.4g%s" % (math.degrees(c.Value) if c.Type == "Angle" else c.Value,
                                     " deg" if c.Type == "Angle" else " mm")
            print("  %3d  %-14s %-18s%s%s%s" % (
                i, c.Type, " ".join(refs), val,
                ("  name=" + c.Name) if c.Name else "",
                "" if c.Driving else "  [reference]"))
        print("DoF:", getattr(sk, "DoF", "?"), " fully constrained:", sk.FullyConstrained)
    """).format(sk=sketch, doc=document, dims=_DIMENSIONAL_KINDS))


# ── Datums and binders ─────────────────────────────────────────────


@_tool()
def create_datum(
    body: str,
    kind: str = "plane",
    base: str = "XY",
    offset: float = 0.0,
    rotation_axis: str = "X",
    rotation_angle: float = 0.0,
    name: str = "",
    document: str = "",
) -> str:
    """Create a datum plane, line, point or coordinate system in a Body.

    The resulting WORLD placement is printed because the offset sign is the
    trap: XZ's normal points -Y, so offset=5 on XZ lands at y=-5. Read the
    printed position before building on the datum.

    Args:
        body: PartDesign Body name.
        kind: "plane", "line", "point" or "cs".
        base: What to attach to: an origin plane (XY, XZ, YZ), an origin axis
            (X, Y, Z) or the name of another datum in the body. A plane on a
            plane uses FlatFace; a line on an axis follows the axis; anything
            else copies the base's placement (ObjectXY).
        offset: Distance along the base's normal (its local Z), in mm.
        rotation_axis: "X", "Y" or "Z" of the base's frame for an extra rotation.
        rotation_angle: Extra rotation in degrees.
        name: Name for the datum. Defaults by kind.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        bd = _body(doc, {body!r})
        kinds = {{"plane": ("PartDesign::Plane", "DatumPlane"), "line": ("PartDesign::Line", "DatumLine"),
                 "point": ("PartDesign::Point", "DatumPoint"), "cs": ("PartDesign::CoordinateSystem", "DatumCS")}}
        if {kind!r} not in kinds:
            raise _Fail("kind must be plane, line, point or cs")
        tid, default = kinds[{kind!r}]
        base = {base!r}.strip()
        if base.upper() in ("XY", "XZ", "YZ", "X", "Y", "Z"):
            sup = _origin_feature(bd, base.upper())
        else:
            sup = _obj(doc, base, "base object")
        axes = {{"X": App.Vector(1, 0, 0), "Y": App.Vector(0, 1, 0), "Z": App.Vector(0, 0, 1)}}
        if {rax!r}.upper() not in axes:
            raise _Fail("rotation_axis must be X, Y or Z")
        dt = doc.addObject(tid, {name!r} or default)
        bd.addObject(dt)
        dt.AttachmentSupport = [(sup, "")]
        if {kind!r} == "plane" and sup.TypeId in ("App::Plane", "PartDesign::Plane"):
            dt.MapMode = "FlatFace"
        elif {kind!r} == "line" and sup.TypeId in ("App::Line", "PartDesign::Line"):
            dt.MapMode = "ObjectX"
        else:
            dt.MapMode = "ObjectXY"
        dt.AttachmentOffset = App.Placement(App.Vector(0, 0, {off!r}),
                                            App.Rotation(axes[{rax!r}.upper()], {rang!r}))
        doc.recompute()
        print("created", dt.Name, "(" + tid + ") in", bd.Name, "on", sup.Name, "mode", dt.MapMode)
        print(_state_str(dt))
        p = dt.Placement
        print("world position:", _vstr(p.Base))
        print("world axes: X", _vstr(p.Rotation.multVec(App.Vector(1, 0, 0))),
              " Y", _vstr(p.Rotation.multVec(App.Vector(0, 1, 0))),
              " Z (normal)", _vstr(p.Rotation.multVec(App.Vector(0, 0, 1))))
        if {kind!r} == "line":
            # A datum line runs along its local Z.
            print("line direction:", _vstr(p.Rotation.multVec(App.Vector(0, 0, 1))))
        print("attach a sketch with: create_sketch(name, body=%r, support=%r)" % (bd.Name, dt.Name))
    """).format(body=body, kind=kind, base=base, off=offset, rax=rotation_axis,
                rang=rotation_angle, name=name, doc=document))


@_tool()
def create_shapebinder(
    body: str,
    source: str,
    sub_elements: str = "",
    kind: str = "SubShapeBinder",
    name: str = "",
    document: str = "",
) -> str:
    """Reference another object's geometry inside a Body, parametrically.

    A binder lets one Body build on faces or edges of another without
    copying coordinates: when the source changes, the binder follows.
    SubShapeBinder is the modern one and can reference several elements;
    ShapeBinder is the older single-reference kind.

    Args:
        body: Body to create the binder in.
        source: Object to reference (a Body, feature, sketch or datum).
        sub_elements: Comma-separated sub-elements, e.g. "Face3,Edge7".
            Empty binds the whole shape.
        kind: "SubShapeBinder" or "ShapeBinder".
        name: Name for the binder.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        bd = _body(doc, {body!r})
        src = _obj(doc, {src!r}, "source")
        subs = _names({subs!r}) or [""]
        if {kind!r} == "SubShapeBinder":
            b = doc.addObject("PartDesign::SubShapeBinder", {name!r} or ("Binder_" + src.Name))
            bd.addObject(b)
            b.Support = [(src, subs)]
        elif {kind!r} == "ShapeBinder":
            b = doc.addObject("PartDesign::ShapeBinder", {name!r} or ("Binder_" + src.Name))
            bd.addObject(b)
            b.Support = (src, subs)
        else:
            raise _Fail("kind must be SubShapeBinder or ShapeBinder")
        doc.recompute()
        print("created", b.Name, "(" + b.TypeId + ") in", bd.Name, "referencing", src.Name, subs)
        print(_state_str(b))
        s = _shape_of(b)
        if s is None:
            print("WARNING: the binder has no shape -- does the source have geometry yet?")
        else:
            print("solids %d  faces %d  edges %d  bbox %s" % (len(s.Solids), len(s.Faces), len(s.Edges), _bbox_str(s)))
    """).format(body=body, src=source, subs=sub_elements, kind=kind, name=name, doc=document))


# ── PartDesign feature set ─────────────────────────────────────────


@_tool()
def partdesign_revolve(
    body: str,
    sketch: str,
    kind: str = "revolution",
    axis: str = "V_Axis",
    angle: float = 360.0,
    reversed_dir: bool = False,
    document: str = "",
) -> str:
    """Revolve a sketch into a Body (Revolution adds, Groove removes).

    Args:
        body: PartDesign Body name.
        sketch: Profile sketch. The profile must not cross the axis.
        kind: "revolution" or "groove".
        axis: "V_Axis" or "H_Axis" (the sketch's own axes), "X", "Y" or "Z"
            (the Body's origin axes), or "Object:EdgeN".
        angle: Sweep angle in degrees.
        reversed_dir: Reverse the sweep direction.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        bd = _body(doc, {body!r})
        sk = _obj(doc, {sk!r}, "sketch")
        kinds = {{"revolution": "PartDesign::Revolution", "groove": "PartDesign::Groove"}}
        if {kind!r} not in kinds:
            raise _Fail("kind must be revolution or groove")
        ref = _axis_ref(doc, bd, sk, {axis!r})
        vol0 = _volume(bd)
        f = doc.addObject(kinds[{kind!r}], {kind!r} + "_" + sk.Name)
        bd.addObject(f)
        f.Profile = sk
        f.ReferenceAxis = ref
        f.Angle = {angle!r}
        f.Reversed = {rev!r}
        doc.recompute()
        print("axis:", ref[0].Name, ref[1], " angle:", f.Angle)
        _report_feature(bd, f, vol0)
    """).format(body=body, sk=sketch, kind=kind, axis=axis, angle=angle,
                rev=reversed_dir, doc=document))


@_tool()
def partdesign_hole(
    body: str,
    sketch: str,
    diameter: float,
    depth: float = 10.0,
    through_all: bool = False,
    cut_type: str = "None",
    cut_diameter: float = 0.0,
    cut_depth: float = 0.0,
    threaded: bool = False,
    thread_type: str = "None",
    thread_size: str = "",
    reversed_dir: bool = False,
    document: str = "",
) -> str:
    """Drill holes at the circle centres of a sketch (PartDesign Hole).

    The sketch supplies positions only: draw one circle per hole on the face
    plane (any radius). The hole goes into the material along the sketch
    normal; use reversed_dir if it drills away from the body.

    Args:
        body: PartDesign Body name.
        sketch: Sketch of circles marking the hole centres.
        diameter: Hole diameter in mm.
        depth: Depth in mm (ignored when through_all).
        through_all: Drill through everything.
        cut_type: "None", "Counterbore", "Countersink" or "Counterdrill".
        cut_diameter: Counterbore/countersink diameter in mm.
        cut_depth: Counterbore depth in mm.
        threaded: Mark the hole as threaded (cosmetic unless ModelThread is set).
        thread_type: e.g. "ISOMetricProfile", "UNC". Only with threaded.
        thread_size: e.g. "M3", "1/4". Only with threaded.
        reversed_dir: Flip the drilling direction.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        bd = _body(doc, {body!r})
        sk = _obj(doc, {sk!r}, "sketch")
        vol0 = _volume(bd)
        h = doc.addObject("PartDesign::Hole", "hole_" + sk.Name)
        bd.addObject(h)
        h.Profile = sk
        h.Diameter = {dia!r}
        h.DepthType = "ThroughAll" if {thru!r} else "Dimension"
        if not {thru!r}:
            h.Depth = {depth!r}
        cuts = h.getEnumerationsOfProperty("HoleCutType")
        if {cut!r} not in cuts:
            raise _Fail("cut_type must be one of %s" % cuts)
        h.HoleCutType = {cut!r}
        if {cut!r} != "None":
            if {cdia!r}:
                h.HoleCutDiameter = {cdia!r}
            if {cdepth!r}:
                h.HoleCutDepth = {cdepth!r}
        if {thr!r}:
            h.Threaded = True
            types = h.getEnumerationsOfProperty("ThreadType")
            if {ttype!r} not in types:
                raise _Fail("thread_type must be one of %s" % types)
            h.ThreadType = {ttype!r}
            if {tsize!r}:
                sizes = h.getEnumerationsOfProperty("ThreadSize")
                if {tsize!r} not in sizes:
                    raise _Fail("thread_size must be one of %s" % sizes)
                h.ThreadSize = {tsize!r}
        h.Reversed = {rev!r}
        doc.recompute()
        print("holes:", len([g for g in sk.Geometry if g.TypeId == "Part::GeomCircle"]),
              " diameter:", h.Diameter, " depth:", h.DepthType if {thru!r} else h.Depth,
              " cut:", h.HoleCutType, " threaded:", h.Threaded)
        _report_feature(bd, h, vol0)
    """).format(body=body, sk=sketch, dia=diameter, depth=depth, thru=through_all,
                cut=cut_type, cdia=cut_diameter, cdepth=cut_depth, thr=threaded,
                ttype=thread_type, tsize=thread_size, rev=reversed_dir, doc=document))


@_tool()
def partdesign_loft(
    body: str,
    profiles: str,
    kind: str = "additive",
    ruled: bool = False,
    closed: bool = False,
    document: str = "",
) -> str:
    """Loft through two or more sketches (AdditiveLoft or SubtractiveLoft).

    Args:
        body: PartDesign Body name.
        profiles: Comma-separated sketch names in order, first to last.
        kind: "additive" or "subtractive".
        ruled: Straight (ruled) surfaces between sections instead of smooth.
        closed: Close the loft back to the first profile.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        bd = _body(doc, {body!r})
        sks = [_obj(doc, n, "sketch") for n in _names({profiles!r})]
        if len(sks) < 2:
            raise _Fail("a loft needs at least two profiles")
        kinds = {{"additive": "PartDesign::AdditiveLoft", "subtractive": "PartDesign::SubtractiveLoft"}}
        if {kind!r} not in kinds:
            raise _Fail("kind must be additive or subtractive")
        vol0 = _volume(bd)
        f = doc.addObject(kinds[{kind!r}], "loft_" + sks[0].Name)
        bd.addObject(f)
        f.Profile = sks[0]
        f.Sections = sks[1:]
        f.Ruled = {ruled!r}
        f.Closed = {closed!r}
        doc.recompute()
        print("profiles:", [s.Name for s in sks], " ruled:", f.Ruled, " closed:", f.Closed)
        _report_feature(bd, f, vol0)
    """).format(body=body, profiles=profiles, kind=kind, ruled=ruled, closed=closed, doc=document))


@_tool()
def partdesign_pipe(
    body: str,
    profile: str,
    spine: str,
    kind: str = "additive",
    mode: str = "Standard",
    transition: str = "Transformed",
    document: str = "",
) -> str:
    """Sweep a profile sketch along a spine (AdditivePipe or SubtractivePipe).

    Args:
        body: PartDesign Body name.
        profile: Profile sketch name. It should sit at the start of the spine.
        spine: Spine sketch or "Object:EdgeN" for a single edge.
        kind: "additive" or "subtractive".
        mode: Profile orientation: "Standard", "Fixed", "Frenet", "Auxiliary" or "Binormal".
        transition: Corner handling: "Transformed", "Right corner" or "Round corner".
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        bd = _body(doc, {body!r})
        prof = _obj(doc, {profile!r}, "profile sketch")
        sp_obj, sp_sub = _sub_ref(doc, {spine!r}, "spine")
        kinds = {{"additive": "PartDesign::AdditivePipe", "subtractive": "PartDesign::SubtractivePipe"}}
        if {kind!r} not in kinds:
            raise _Fail("kind must be additive or subtractive")
        vol0 = _volume(bd)
        f = doc.addObject(kinds[{kind!r}], "pipe_" + prof.Name)
        bd.addObject(f)
        f.Profile = prof
        f.Spine = (sp_obj, [s for s in sp_sub if s])
        for prop, val in (("Mode", {mode!r}), ("Transition", {trans!r})):
            allowed = f.getEnumerationsOfProperty(prop)
            if val not in allowed:
                raise _Fail("%s must be one of %s" % (prop.lower(), allowed))
            setattr(f, prop, val)
        doc.recompute()
        print("profile:", prof.Name, " spine:", sp_obj.Name, sp_sub, " mode:", f.Mode, " transition:", f.Transition)
        _report_feature(bd, f, vol0)
    """).format(body=body, profile=profile, spine=spine, kind=kind, mode=mode,
                trans=transition, doc=document))


@_tool()
def partdesign_helix(
    body: str,
    sketch: str,
    pitch: float,
    height: float,
    turns: float = 0.0,
    angle: float = 0.0,
    axis: str = "V_Axis",
    left_handed: bool = False,
    kind: str = "additive",
    document: str = "",
) -> str:
    """Sweep a profile along a helix (threads, springs): AdditiveHelix or SubtractiveHelix.

    Args:
        body: PartDesign Body name.
        sketch: Profile sketch, drawn beside the axis.
        pitch: Distance per turn in mm.
        height: Total height in mm. With turns > 0, height is derived instead.
        turns: Number of turns (0 to use height).
        angle: Cone angle in degrees (0 for a cylindrical helix).
        axis: "V_Axis" or "H_Axis" (sketch axes), "X", "Y", "Z" (body axes), or "Object:EdgeN".
        left_handed: Left-hand helix.
        kind: "additive" or "subtractive".
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        bd = _body(doc, {body!r})
        sk = _obj(doc, {sk!r}, "sketch")
        kinds = {{"additive": "PartDesign::AdditiveHelix", "subtractive": "PartDesign::SubtractiveHelix"}}
        if {kind!r} not in kinds:
            raise _Fail("kind must be additive or subtractive")
        ref = _axis_ref(doc, bd, sk, {axis!r})
        vol0 = _volume(bd)
        f = doc.addObject(kinds[{kind!r}], "helix_" + sk.Name)
        bd.addObject(f)
        f.Profile = sk
        f.ReferenceAxis = ref
        f.Mode = "pitch-turns-angle" if {turns!r} > 0 else "pitch-height-angle"
        f.Pitch = {pitch!r}
        if {turns!r} > 0:
            f.Turns = {turns!r}
        else:
            f.Height = {height!r}
        f.Angle = {angle!r}
        f.LeftHanded = {lh!r}
        doc.recompute()
        print("axis:", ref[0].Name, ref[1], " mode:", f.Mode, " pitch:", f.Pitch,
              " height:", f.Height, " turns:", f.Turns)
        _report_feature(bd, f, vol0)
    """).format(body=body, sk=sketch, pitch=pitch, height=height, turns=turns, angle=angle,
                axis=axis, lh=left_handed, kind=kind, doc=document))


@_tool()
def partdesign_primitive(
    body: str,
    kind: str = "box",
    properties: str = "{}",
    additive: bool = True,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    name: str = "",
    document: str = "",
) -> str:
    """Add a primitive solid to a Body (additive or subtractive).

    The primitive is attached to the Body's XY plane with an offset, so it
    stays put when the body moves. A Box grows from its corner; a Cylinder,
    Sphere, Cone and Torus are centred on their axis at the offset.

    Args:
        body: PartDesign Body name.
        kind: "box", "cylinder", "sphere", "cone", "ellipsoid", "torus", "prism" or "wedge".
        properties: JSON of dimensions, e.g. '{"Length": 10, "Width": 5, "Height": 3}',
            '{"Radius": 4, "Height": 10}', '{"Radius1": 5, "Radius2": 2, "Height": 8}'.
            Unknown property names are rejected and the valid ones listed.
        additive: True adds material, False removes it.
        x: Offset X in mm from the Body origin.
        y: Offset Y in mm.
        z: Offset Z in mm.
        name: Name for the feature.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import json
        doc = _doc({doc!r})
        bd = _body(doc, {body!r})
        kinds = {{"box": "Box", "cylinder": "Cylinder", "sphere": "Sphere", "cone": "Cone",
                 "ellipsoid": "Ellipsoid", "torus": "Torus", "prism": "Prism", "wedge": "Wedge"}}
        if {kind!r} not in kinds:
            raise _Fail("kind must be one of %s" % sorted(kinds))
        tid = "PartDesign::" + ("Additive" if {add!r} else "Subtractive") + kinds[{kind!r}]
        props = json.loads({props!r})
        vol0 = _volume(bd)
        f = doc.addObject(tid, {name!r} or (("" if {add!r} else "cut_") + kinds[{kind!r}]))
        dims = [p for p in f.PropertiesList if f.getGroupOfProperty(p) in ("Primitive", kinds[{kind!r}])]
        unknown = [k for k in props if k not in f.PropertiesList]
        if unknown:
            # Not in the Body yet, so removing it cannot disturb the Tip.
            doc.removeObject(f.Name)
            raise _Fail("unknown properties %s; this primitive has %s" % (unknown, dims))
        for k, v in props.items():
            setattr(f, k, v)
        bd.addObject(f)
        f.AttachmentSupport = [(_origin_feature(bd, "XY"), "")]
        f.MapMode = "ObjectXY"
        f.AttachmentOffset = App.Placement(App.Vector({x!r}, {y!r}, {z!r}), App.Rotation())
        doc.recompute()
        print("dimensions:", {{k: getattr(f, k) for k in dims}})
        _report_feature(bd, f, vol0)
    """).format(body=body, kind=kind, props=properties, add=additive, x=x, y=y, z=z,
                name=name, doc=document))


@_tool()
def partdesign_dressup(
    body: str,
    kind: str = "fillet",
    size: float = 1.0,
    elements: str = "all",
    feature: str = "",
    size2: float = 0.0,
    angle: float = 0.0,
    neutral: str = "",
    document: str = "",
) -> str:
    """Fillet, chamfer, draft or hollow out (thickness) a Body's current shape.

    Sub-element names such as Edge7 are fragile, so elements can also be
    chosen by selector. The elements actually used are printed; read them.

    Selector syntax for elements: groups separated by ";" are united; terms
    within a group separated by "&" must all match.
      Edges (fillet, chamfer): EdgeN, FaceN (all edges of that face), all,
        parallel:X|Y|Z, perpendicular:X|Y|Z, straight, circular,
        longer:L, shorter:L, above:Z, below:Z (whole edge above/below world Z)
      Faces (draft, thickness): FaceN, all, planar, normal:X|Y|Z,
        above:Z, below:Z, larger:A, smaller:A (area in mm2)
    Example: "parallel:Z & longer:5 ; Edge12"

    Args:
        body: PartDesign Body name.
        kind: "fillet", "chamfer", "draft" or "thickness".
        size: Fillet radius, chamfer size, draft angle in degrees, or wall
            thickness, in mm.
        elements: Selector string (see above). Default "all".
        feature: Feature whose shape to dress up. Default: the Body's Tip.
        size2: Second chamfer distance (makes a "Two distances" chamfer).
        angle: Chamfer angle in degrees (makes a "Distance and Angle" chamfer).
        neutral: Draft only: the neutral plane as "FaceN" of the feature or
            an origin plane role (XY, XZ, YZ).
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import re
        doc = _doc({doc!r})
        bd = _body(doc, {body!r})
        base = _obj(doc, {feat!r}, "feature") if {feat!r} else bd.Tip
        if base is None:
            raise _Fail("%s has no Tip feature yet" % bd.Name)
        shape = base.Shape
        if shape is None or shape.isNull():
            raise _Fail("%s has no shape" % base.Name)
        kind = {kind!r}
        want = "Face" if kind in ("draft", "thickness") else "Edge"
        if kind not in ("fillet", "chamfer", "draft", "thickness"):
            raise _Fail("kind must be fillet, chamfer, draft or thickness")
        axes = {{"X": App.Vector(1, 0, 0), "Y": App.Vector(0, 1, 0), "Z": App.Vector(0, 0, 1)}}
        edges, faces = list(shape.Edges), list(shape.Faces)
        all_elems = edges if want == "Edge" else faces
        eps = 1e-6

        def edge_dir(e):
            if len(e.Vertexes) < 2:
                return None
            d = e.Vertexes[-1].Point - e.Vertexes[0].Point
            return d.normalize() if d.Length > eps else None

        def match(tok, el):
            t = tok.strip()
            k, _, v = t.partition(":")
            k = k.lower()
            if k == "all":
                return True
            if want == "Edge":
                if k == "straight":
                    return "Line" in type(el.Curve).__name__
                if k == "circular":
                    return "Circle" in type(el.Curve).__name__
                if k in ("parallel", "perpendicular"):
                    d = edge_dir(el)
                    if d is None or v.upper() not in axes:
                        return False
                    dot = abs(d.dot(axes[v.upper()]))
                    return dot > 0.999 if k == "parallel" else dot < 0.001
                if k == "longer":
                    return el.Length > float(v)
                if k == "shorter":
                    return el.Length < float(v)
            else:
                if k == "planar":
                    return "Plane" in type(el.Surface).__name__
                if k == "normal":
                    if "Plane" not in type(el.Surface).__name__ or v.upper() not in axes:
                        return False
                    return abs(el.Surface.Axis.dot(axes[v.upper()])) > 0.999
                if k == "larger":
                    return el.Area > float(v)
                if k == "smaller":
                    return el.Area < float(v)
            if k == "above":
                return el.BoundBox.ZMin > float(v) - eps
            if k == "below":
                return el.BoundBox.ZMax < float(v) + eps
            raise _Fail("unknown selector term %r" % t)

        chosen = {{}}
        for group in {elems!r}.split(";"):
            toks = [t.strip() for t in group.split("&") if t.strip()]
            if not toks:
                continue
            cands = list(enumerate(all_elems, 1))
            for tok in toks:
                m = re.match(r"^(Edge|Face)(\\d+)$", tok)
                if m:
                    n = int(m.group(2))
                    if m.group(1) == want:
                        if n < 1 or n > len(all_elems):
                            raise _Fail("%s has no %s" % (base.Name, tok))
                        cands = [(i, el) for i, el in cands if i == n]
                    elif m.group(1) == "Face" and want == "Edge":
                        if n < 1 or n > len(faces):
                            raise _Fail("%s has no %s" % (base.Name, tok))
                        fe = faces[n - 1].Edges
                        cands = [(i, el) for i, el in cands if any(el.isSame(x) for x in fe)]
                    else:
                        raise _Fail("%s selects %ss, not %ss" % (kind, want.lower(), m.group(1).lower()))
                else:
                    cands = [(i, el) for i, el in cands if match(tok, el)]
            for i, el in cands:
                chosen[i] = el
        if not chosen:
            raise _Fail("selector %r matched no %ss of %s (it has %d)" % (
                {elems!r}, want.lower(), base.Name, len(all_elems)))
        names = [want + str(i) for i in sorted(chosen)]
        vol0 = _volume(bd)
        if kind == "fillet":
            f = doc.addObject("PartDesign::Fillet", "Fillet")
            bd.addObject(f)
            f.Base = (base, names)
            f.Radius = {size!r}
        elif kind == "chamfer":
            f = doc.addObject("PartDesign::Chamfer", "Chamfer")
            bd.addObject(f)
            f.Base = (base, names)
            f.Size = {size!r}
            if {size2!r}:
                f.ChamferType = "Two distances"
                f.Size2 = {size2!r}
            elif {angle!r}:
                f.ChamferType = "Distance and Angle"
                f.Angle = {angle!r}
        elif kind == "draft":
            f = doc.addObject("PartDesign::Draft", "Draft")
            bd.addObject(f)
            f.Base = (base, names)
            f.Angle = {size!r}
            if {neutral!r}:
                if {neutral!r}.upper() in ("XY", "XZ", "YZ"):
                    f.NeutralPlane = (_origin_feature(bd, {neutral!r}.upper()), [""])
                else:
                    f.NeutralPlane = (base, [{neutral!r}])
        else:
            f = doc.addObject("PartDesign::Thickness", "Thickness")
            bd.addObject(f)
            f.Base = (base, names)
            f.Value = {size!r}
        doc.recompute()
        print("%s on %s, %d %s(s):" % (kind, base.Name, len(names), want.lower()))
        for i in sorted(chosen):
            el = chosen[i]
            c = el.CenterOfGravity if hasattr(el, "CenterOfGravity") else el.BoundBox.Center
            if want == "Edge":
                print("  Edge%-4d len %8.4f  centre (%.3f, %.3f, %.3f)  %s" % (
                    i, el.Length, c.x, c.y, c.z, type(el.Curve).__name__))
            else:
                print("  Face%-4d area %8.4f  centre (%.3f, %.3f, %.3f)  %s" % (
                    i, el.Area, c.x, c.y, c.z, type(el.Surface).__name__))
        _report_feature(bd, f, vol0)
        if not f.isValid():
            print("HINT: a dress-up that fails usually has a size larger than the geometry"
                  " allows; remove it with remove_object and retry with a smaller size.")
    """).format(body=body, kind=kind, size=size, elems=elements, feat=feature,
                size2=size2, angle=angle, neutral=neutral, doc=document))


@_tool(idempotent=True)
def set_tip(body: str, feature: str, document: str = "") -> str:
    """Make a feature the Body's Tip: the feature whose result the Body shows and exports.

    Args:
        body: PartDesign Body name.
        feature: Feature in that body.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        bd = _body(doc, {body!r})
        ft = _obj(doc, {feat!r}, "feature")
        if ft.getParentGeoFeatureGroup() != bd:
            raise _Fail("%s is not a feature of %s" % (ft.Name, bd.Name))
        bd.Tip = ft
        doc.recompute()
        print("Tip of", bd.Name, "is now", bd.Tip.Name)
        s = _shape_of(bd)
        if s is not None and s.Solids:
            print("body volume: %.4f mm3  bbox: %s" % (s.Volume, _bbox_str(s)))
    """).format(body=body, feat=feature, doc=document))


# ── Appearance ─────────────────────────────────────────────────────


@_tool(idempotent=True)
def set_appearance(
    object_names: str,
    color: str = "",
    transparency: int = -1,
    display_mode: str = "",
    document: str = "",
) -> str:
    """Set colour, transparency and display mode on objects.

    Colours are what survive into a glTF export and the Blender handoff, so
    set them before exporting. On a Body the colour is applied to the Body
    and its Tip; on an App::Link it is applied as an override material.

    Args:
        object_names: Comma-separated names, or "*" for everything.
        color: "#rrggbb", or "r,g,b" with components 0..1 or 0..255. Empty keeps it.
        transparency: 0 (opaque) to 100. -1 keeps it.
        display_mode: "Flat Lines", "Shaded", "Wireframe" or "Points". Empty keeps it.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        names = {names!r}
        objs = doc.Objects if names.strip() == "*" else [_obj(doc, n) for n in _names(names)]
        rgb = None
        c = {color!r}.strip()
        if c:
            if c.startswith("#") and len(c) == 7:
                rgb = tuple(int(c[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
            else:
                parts = [float(p) for p in c.replace(";", ",").split(",")]
                if len(parts) != 3:
                    raise _Fail("color must be #rrggbb or r,g,b")
                rgb = tuple(p / 255.0 if max(parts) > 1.0 else p for p in parts)

        def paint(vo):
            if rgb is not None:
                if "ShapeAppearance" in vo.PropertiesList:
                    m = vo.ShapeAppearance[0] if vo.ShapeAppearance else App.Material()
                    m.DiffuseColor = (rgb[0], rgb[1], rgb[2], m.DiffuseColor[3] if len(m.DiffuseColor) > 3 else 0.0)
                    vo.ShapeAppearance = (m,)
                elif "ShapeColor" in vo.PropertiesList:
                    vo.ShapeColor = rgb
                elif "ShapeMaterial" in vo.PropertiesList:
                    m = vo.ShapeMaterial
                    m.DiffuseColor = (rgb[0], rgb[1], rgb[2], 0.0)
                    vo.ShapeMaterial = m
                    vo.OverrideMaterial = True
                else:
                    return False
            if {tr!r} >= 0 and "Transparency" in vo.PropertiesList:
                vo.Transparency = int({tr!r})
            if {dm!r}:
                modes = vo.getEnumerationsOfProperty("DisplayMode") if "DisplayMode" in vo.PropertiesList else []
                if {dm!r} not in modes:
                    raise _Fail("display_mode must be one of %s" % modes)
                vo.DisplayMode = {dm!r}
            return True

        done, skipped = [], []
        for o in objs:
            vo = getattr(o, "ViewObject", None)
            if vo is None:
                skipped.append(o.Name)
                continue
            ok = paint(vo)
            if o.TypeId == "PartDesign::Body" and getattr(o, "Tip", None) is not None:
                paint(o.Tip.ViewObject)
            (done if ok else skipped).append(o.Name)
        doc.recompute()
        print("painted", len(done), "object(s):", done)
        if skipped:
            print("skipped (no colour property):", skipped)
    """).format(names=object_names, color=color, tr=transparency, dm=display_mode, doc=document))


@_tool(read_only=True, idempotent=True)
def get_appearance(object_names: str = "*", document: str = "") -> str:
    """Report colour, transparency, display mode and visibility of objects.

    Args:
        object_names: Comma-separated names, or "*" for everything with a shape.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        names = {names!r}
        objs = doc.Objects if names.strip() == "*" else [_obj(doc, n) for n in _names(names)]
        def hexcol(t):
            return "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in t[:3])
        n = 0
        for o in objs:
            vo = getattr(o, "ViewObject", None)
            if vo is None:
                continue
            col = None
            if "ShapeAppearance" in vo.PropertiesList and vo.ShapeAppearance:
                col = hexcol(vo.ShapeAppearance[0].DiffuseColor)
            elif "ShapeColor" in vo.PropertiesList:
                col = hexcol(vo.ShapeColor)
            elif "ShapeMaterial" in vo.PropertiesList and getattr(vo, "OverrideMaterial", False):
                col = hexcol(vo.ShapeMaterial.DiffuseColor) + " (override)"
            if col is None and names.strip() == "*":
                continue
            n += 1
            print("  %-20s %-16s colour %-18s transparency %-4s mode %-12s visible %s" % (
                o.Name, o.TypeId.split("::")[-1], col or "-",
                vo.Transparency if "Transparency" in vo.PropertiesList else "-",
                vo.DisplayMode if "DisplayMode" in vo.PropertiesList else "-",
                vo.Visibility))
        print(n, "object(s)")
    """).format(names=object_names, doc=document))


# ── Mesh ───────────────────────────────────────────────────────────


@_tool()
def shape_to_mesh(
    object_name: str,
    deflection: float = 0.1,
    angular_deflection: float = 30.0,
    name: str = "",
    document: str = "",
) -> str:
    """Tessellate a shape into a Mesh::Feature object in the document.

    Args:
        object_name: Object with a shape (Body, feature, Part primitive, Link).
        deflection: Linear chord tolerance in mm. Smaller is finer.
        angular_deflection: Angular tolerance in degrees.
        name: Name for the mesh object.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import math, MeshPart
        doc = _doc({doc!r})
        o = _obj(doc, {name!r})
        s = _shape_of(o)
        if s is None:
            raise _Fail("%s (%s) has no shape" % (o.Name, o.TypeId))
        m = MeshPart.meshFromShape(Shape=s, LinearDeflection={defl!r},
                                   AngularDeflection=math.radians({ang!r}), Relative=False)
        mo = doc.addObject("Mesh::Feature", {mname!r} or (o.Name + "_Mesh"))
        mo.Mesh = m
        doc.recompute()
        print("created", mo.Name, "from", o.Name)
        print("facets %d  points %d  solid %s  volume %.4f mm3 (shape %.4f)" % (
            m.CountFacets, m.CountPoints, m.isSolid(), m.Volume, s.Volume))
    """).format(name=object_name, defl=deflection, ang=angular_deflection, mname=name, doc=document))


@_tool(read_only=True, idempotent=True)
def mesh_info(object_name: str, document: str = "") -> str:
    """Report a mesh's size, closure, manifoldness and self-intersections.

    A mesh that is not solid or has non-manifold edges will not slice
    cleanly; a mesh with self-intersections will not convert to a shape.

    Args:
        object_name: Mesh::Feature object name.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        o = _obj(doc, {name!r})
        if "Mesh" not in o.PropertiesList:
            raise _Fail("%s (%s) is not a mesh object" % (o.Name, o.TypeId))
        m = o.Mesh
        print("mesh           :", o.Name)
        print("facets / points / edges:", m.CountFacets, "/", m.CountPoints, "/", m.CountEdges)
        print("solid (closed) :", m.isSolid())
        print("non-manifold   :", m.hasNonManifolds())
        print("self-intersect :", m.hasSelfIntersections())
        print("inconsistent orientation:", m.hasNonUniformOrientedFacets())
        print("invalid points :", m.hasInvalidPoints())
        print("area           : %.4f mm2" % m.Area)
        print("volume         : %.4f mm3%s" % (m.Volume, "" if m.isSolid() else "  (not closed; unreliable)"))
        print("bbox           :", _bbox_str(m))
    """).format(name=object_name, doc=document))


@_tool()
def mesh_to_shape(
    object_name: str,
    tolerance: float = 0.1,
    solid: bool = True,
    name: str = "",
    document: str = "",
) -> str:
    """Convert a mesh into a Part::Feature shape (faceted, one face per triangle).

    Args:
        object_name: Mesh::Feature object name.
        tolerance: Sewing tolerance in mm.
        solid: Also make a solid from the closed shell.
        name: Name for the new shape object.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        o = _obj(doc, {name!r})
        if "Mesh" not in o.PropertiesList:
            raise _Fail("%s (%s) is not a mesh object" % (o.Name, o.TypeId))
        sh = _Part.Shape()
        sh.makeShapeFromMesh(o.Mesh.Topology, {tol!r})
        made = "shell"
        if {solid!r}:
            try:
                sol = _Part.makeSolid(sh)
                if sol.isValid() and sol.Volume > 0:
                    sh = sol
                    made = "solid"
            except Exception as e:
                print("WARNING: could not make a solid:", e)
        po = doc.addObject("Part::Feature", {sname!r} or (o.Name + "_Shape"))
        po.Shape = sh
        doc.recompute()
        print("created", po.Name, "as a", made, "from", o.Name)
        print("valid:", sh.isValid(), " faces:", len(sh.Faces), " volume: %.4f mm3" % sh.Volume)
        print("bbox:", _bbox_str(sh))
    """).format(name=object_name, tol=tolerance, solid=solid, sname=name, doc=document))


@_tool(destructive=True)
def mesh_fix(object_name: str, document: str = "") -> str:
    """Run the standard mesh repairs in place and report what changed.

    Removes duplicate points and facets, fixes degenerate facets and
    indices, harmonizes normals, removes non-manifolds and fixes
    self-intersections.

    Args:
        object_name: Mesh::Feature object name.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        o = _obj(doc, {name!r})
        if "Mesh" not in o.PropertiesList:
            raise _Fail("%s (%s) is not a mesh object" % (o.Name, o.TypeId))
        m = o.Mesh.copy()
        def stats(mm):
            return {{"facets": mm.CountFacets, "points": mm.CountPoints, "solid": mm.isSolid(),
                    "non_manifold": mm.hasNonManifolds(), "self_intersections": mm.hasSelfIntersections(),
                    "orientation_ok": not mm.hasNonUniformOrientedFacets()}}
        before = stats(m)
        ran, failed = [], []
        for step, args in (("removeDuplicatedPoints", ()), ("removeDuplicatedFacets", ()),
                           ("fixDegenerations", (0.0,)), ("fixIndices", ()),
                           ("harmonizeNormals", ()), ("removeNonManifolds", ()),
                           ("fixSelfIntersections", ())):
            try:
                getattr(m, step)(*args)
                ran.append(step)
            except TypeError:
                try:
                    getattr(m, step)()
                    ran.append(step)
                except Exception as e:
                    failed.append("%s: %s" % (step, e))
            except Exception as e:
                failed.append("%s: %s" % (step, e))
        o.Mesh = m
        doc.recompute()
        after = stats(m)
        print("ran:", ran)
        if failed:
            print("failed:", failed)
        print("before:", before)
        print("after :", after)
    """).format(name=object_name, doc=document))


# ── Measurement and verification ───────────────────────────────────


@_tool(read_only=True, idempotent=True)
def measure_element(
    object_name: str,
    sub_element: str = "",
    document: str = "",
) -> str:
    """Measure an object or one of its sub-elements: size, centre of mass, inertia.

    Args:
        object_name: Object name.
        sub_element: Optional "FaceN", "EdgeN" or "VertexN" of that object.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        o = _obj(doc, {name!r})
        s = _shape_of(o)
        if s is None:
            raise _Fail("%s (%s) has no shape" % (o.Name, o.TypeId))
        sub = {sub!r}.strip()
        if sub:
            try:
                s = s.getElement(sub)
            except Exception as e:
                raise _Fail("%s has no %s: %s" % (o.Name, sub, e))
        print("%s%s: %s" % (o.Name, (":" + sub) if sub else "", s.ShapeType))
        print("  bbox:", _bbox_str(s))
        if s.ShapeType == "Vertex":
            print("  point:", s.Point)
        if s.ShapeType == "Edge":
            print("  length: %.4f mm  curve: %s" % (s.Length, type(s.Curve).__name__))
            print("  start:", s.Vertexes[0].Point, " end:", s.Vertexes[-1].Point)
            if "Circle" in type(s.Curve).__name__:
                print("  centre:", s.Curve.Center, " radius: %.4f" % s.Curve.Radius)
        if s.ShapeType == "Face":
            print("  area: %.4f mm2  surface: %s" % (s.Area, type(s.Surface).__name__))
            if "Plane" in type(s.Surface).__name__:
                print("  normal:", s.Surface.Axis, " through:", s.Surface.Position)
        if s.ShapeType in ("Solid", "Compound", "CompSolid", "Shell"):
            print("  solids %d  faces %d  edges %d" % (len(s.Solids), len(s.Faces), len(s.Edges)))
            print("  area: %.4f mm2  volume: %.4f mm3" % (s.Area, s.Volume))
        try:
            print("  centre of mass:", s.CenterOfGravity if hasattr(s, "CenterOfGravity") else s.CenterOfMass)
        except Exception:
            pass
        if s.Solids:
            try:
                pp = s.Solids[0].PrincipalProperties if len(s.Solids) == 1 else None
                mi = (s.Solids[0] if len(s.Solids) == 1 else s).MatrixOfInertia
                print("  inertia diagonal: (%.4f, %.4f, %.4f)" % (mi.A11, mi.A22, mi.A33))
                if pp:
                    print("  principal moments:", tuple(round(v, 4) for v in pp["Moments"]))
            except Exception:
                pass
    """).format(name=object_name, sub=sub_element, doc=document))


@_tool(read_only=True, idempotent=True)
def point_inside(
    object_name: str,
    x: float,
    y: float,
    z: float,
    tolerance: float = 1e-6,
    document: str = "",
) -> str:
    """Test whether a world point lies inside an object's solid.

    Use it to prove a pocket really cut (point inside the pocket is now
    outside) or a boss really grew.

    Args:
        object_name: Object name.
        x: World X in mm.
        y: World Y in mm.
        z: World Z in mm.
        tolerance: Tolerance in mm; points on the surface count as inside.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        o = _obj(doc, {name!r})
        s = _shape_of(o)
        if s is None or not s.Solids:
            raise _Fail("%s has no solid to test against" % o.Name)
        p = App.Vector({x!r}, {y!r}, {z!r})
        inside = any(sol.isInside(p, {tol!r}, True) for sol in s.Solids)
        print("(%g, %g, %g) is %s %s" % ({x!r}, {y!r}, {z!r}, "INSIDE" if inside else "OUTSIDE", o.Name))
        print("distance to surface: %.6f mm" % s.distToShape(_Part.Vertex(p))[0])
    """).format(name=object_name, x=x, y=y, z=z, tol=tolerance, doc=document))


@_tool()
def measure_distance(
    element_a: str,
    element_b: str,
    keep: bool = True,
    document: str = "",
) -> str:
    """Measure the distance between two elements with a Measure object the user can see.

    Creates a Measure::MeasureDistance in the document, so the result also
    appears in the 3D view and the tree. With keep=False it is removed after
    reading.

    Args:
        element_a: "Object:FaceN", "Object:EdgeN", "Object:VertexN" or "Object".
        element_b: Same form.
        keep: Leave the measurement object in the document.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        oa, sa = _sub_ref(doc, {a!r}, "object")
        ob, sb = _sub_ref(doc, {b!r}, "object")
        m = doc.addObject("Measure::MeasureDistance", "Measure_%s_%s" % (oa.Name, ob.Name))
        m.Element1 = (oa, sa[0])
        m.Element2 = (ob, sb[0])
        doc.recompute()
        bad = [t for t in m.State if t in ("Invalid", "Error")]
        if bad:
            doc.removeObject(m.Name)
            raise _Fail("FreeCAD could not measure %s to %s (%s); check the element names" % (
                {a!r}, {b!r}, bad))
        print("distance %s -> %s: %.6f mm (%.6f in)" % ({a!r}, {b!r}, m.Distance, m.Distance / 25.4))
        print("  dX %.6f  dY %.6f  dZ %.6f" % (m.DistanceX, m.DistanceY, m.DistanceZ))
        print("  from", m.Position1, "to", m.Position2)
        if {keep!r}:
            print("measurement object:", m.Name)
        else:
            doc.removeObject(m.Name)
    """).format(a=element_a, b=element_b, keep=keep, doc=document))


# ── Document helpers ───────────────────────────────────────────────


@_tool(read_only=True, idempotent=True)
def find_objects(
    type_contains: str = "",
    label_contains: str = "",
    in_body: str = "",
    document: str = "",
) -> str:
    """Find objects by type, label or containing Body.

    Args:
        type_contains: Text the TypeId must contain, e.g. "Sketch", "PartDesign::", "Link".
        label_contains: Text the Label must contain (case-insensitive).
        in_body: Only features inside this Body.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        bd = _body(doc, {body!r}) if {body!r} else None
        hits = []
        for o in doc.Objects:
            if {tc!r} and {tc!r} not in o.TypeId:
                continue
            if {lc!r} and {lc!r}.lower() not in o.Label.lower():
                continue
            parent = o.getParentGeoFeatureGroup()
            if bd is not None and parent != bd:
                continue
            hits.append((o, parent))
        for o, parent in hits:
            print("  %-24s %-32s label %-24s in %s" % (o.Name, o.TypeId, o.Label, parent.Name if parent else "-"))
        print(len(hits), "object(s)")
    """).format(tc=type_contains, lc=label_contains, body=in_body, doc=document))


@_tool(read_only=True, idempotent=True)
def dependencies(object_name: str, document: str = "") -> str:
    """Show what an object depends on and what depends on it.

    Args:
        object_name: Object name.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        o = _obj(doc, {name!r})
        parent = o.getParentGeoFeatureGroup()
        print(o.Name, "(" + o.TypeId + ")", "in", parent.Name if parent else "no container")
        print("depends on (OutList):", [d.Name for d in o.OutList])
        print("used by (InList)    :", [d.Name for d in o.InList])
        print("recursive: %d upstream, %d downstream" % (len(o.OutListRecursive), len(o.InListRecursive)))
        if o.ExpressionEngine:
            print("expressions:", list(o.ExpressionEngine))
    """).format(name=object_name, doc=document))


@_tool(read_only=True, idempotent=True)
def list_object_types(module_filter: str = "", document: str = "") -> str:
    """List the object types a document can create, grouped by module.

    Args:
        module_filter: Only types whose name contains this text, e.g. "PartDesign", "Measure".
        document: Document name. Empty string for the active document (a scratch
            document is used and closed if none is open).
    """
    return _call(textwrap.dedent("""\
        doc = App.getDocument({doc!r}) if {doc!r} else App.ActiveDocument
        temp = doc is None
        if temp:
            doc = App.newDocument("TypeProbe")
        try:
            types = sorted(doc.supportedTypes())
        finally:
            if temp:
                App.closeDocument(doc.Name)
        groups = {{}}
        for t in types:
            if {flt!r} and {flt!r}.lower() not in t.lower():
                continue
            groups.setdefault(t.split("::")[0], []).append(t)
        for mod in sorted(groups):
            print("%s (%d):" % (mod, len(groups[mod])))
            print("  " + "  ".join(t.split("::", 1)[1] for t in groups[mod]))
        print(sum(len(v) for v in groups.values()), "type(s) in", len(groups), "module(s); %d total" % len(types))
    """).format(flt=module_filter, doc=document))


@_tool(read_only=True, idempotent=True)
def get_property_info(object_name: str, property_name: str, document: str = "") -> str:
    """Describe one property: type, group, documentation, enum values, value and expression.

    Args:
        object_name: Object name.
        property_name: Property name.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        o = _obj(doc, {name!r})
        p = {prop!r}
        if p not in o.PropertiesList:
            near = [q for q in o.PropertiesList if p.lower() in q.lower()]
            raise _Fail("%s has no property %r%s" % (o.Name, p, ("; similar: %s" % near) if near else ""))
        print("%s.%s" % (o.Name, p))
        print("  type  :", o.getTypeIdOfProperty(p))
        print("  group :", o.getGroupOfProperty(p))
        print("  doc   :", o.getDocumentationOfProperty(p))
        print("  editor:", o.getEditorMode(p) or "normal")
        try:
            print("  enum  :", o.getEnumerationsOfProperty(p))
        except Exception:
            pass
        try:
            print("  value :", repr(getattr(o, p)))
        except Exception as e:
            print("  value : <error %s>" % e)
        expr = dict(o.ExpressionEngine).get(p)
        if expr:
            print("  expression:", expr)
    """).format(name=object_name, prop=property_name, doc=document))


@_tool()
def add_property(
    object_name: str,
    property_type: str,
    property_name: str,
    group: str = "Custom",
    documentation: str = "",
    document: str = "",
) -> str:
    """Add a custom property to an object (for parameters or metadata).

    Args:
        object_name: Object name.
        property_type: e.g. "App::PropertyLength", "App::PropertyString", "App::PropertyFloat",
            "App::PropertyBool", "App::PropertyInteger", "App::PropertyAngle".
        property_name: Name of the new property.
        group: Property group shown in the editor.
        documentation: Tooltip text.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        o = _obj(doc, {name!r})
        if {prop!r} in o.PropertiesList:
            raise _Fail("%s already has a property %r" % (o.Name, {prop!r}))
        try:
            o.addProperty({ptype!r}, {prop!r}, {group!r}, {docs!r})
        except Exception as e:
            raise _Fail("cannot add %s: %s" % ({ptype!r}, e))
        doc.recompute()
        print("added", o.Name + "." + {prop!r}, "(" + {ptype!r} + ") in group", {group!r})
        print("value:", repr(getattr(o, {prop!r})))
    """).format(name=object_name, ptype=property_type, prop=property_name, group=group,
                docs=documentation, doc=document))


@_tool(idempotent=True)
def touch(object_name: str, recompute_now: bool = True, document: str = "") -> str:
    """Mark an object as changed so the next recompute rebuilds it and its dependents.

    Args:
        object_name: Object name.
        recompute_now: Recompute immediately and report the state afterwards.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        o = _obj(doc, {name!r})
        o.touch()
        print("touched", o.Name, "; downstream:", [d.Name for d in o.InListRecursive][:20])
        if {rc!r}:
            doc.recompute()
            print("recomputed;", _state_str(o))
        else:
            print("state:", list(o.State))
    """).format(name=object_name, rc=recompute_now, doc=document))


# ── Assembly joints ────────────────────────────────────────────────

_JOINT_KINDS = ("Fixed", "Revolute", "Cylindrical", "Slider", "Ball", "Distance",
                "Parallel", "Perpendicular", "Angle", "RackPinion", "Screw", "Gears", "Belt")

# Shared by the assembly tools: executed inside FreeCAD after the prelude.
_ASM_HELPERS = textwrap.dedent("""\
    import JointObject, UtilsAssembly

    def _asm(doc, name):
        a = _obj(doc, name, "assembly")
        if a.TypeId != "Assembly::AssemblyObject":
            raise _Fail("%s is a %s, not an Assembly::AssemblyObject (create_assembly makes one)" % (a.Name, a.TypeId))
        return a

    def _is_asm_part(o):
        # The assembly's Group also lists joints and groups; a part is a
        # placeable thing: a link, a container or a solid.
        if hasattr(o, "JointType") or hasattr(o, "ObjectToGround"):
            return False
        if o.TypeId in ("Assembly::JointGroup", "Assembly::BomGroup", "Assembly::ViewGroup"):
            return False
        return o.TypeId in ("App::Link", "App::Part", "Assembly::AssemblyObject", "PartDesign::Body") \\
            or ("Placement" in o.PropertiesList and hasattr(o, "Shape"))

    def _asm_parts(asm):
        return [o for o in asm.Group if _is_asm_part(o)]

    def _asm_status(asm):
        jg = UtilsAssembly.getJointGroup(asm)
        grounded = [o.ObjectToGround.Name for o in jg.Group if hasattr(o, "ObjectToGround") and o.ObjectToGround]
        joints = [o for o in jg.Group if hasattr(o, "JointType")]
        parts = _asm_parts(asm)
        print("assembly %s: %d part(s), %d joint(s), grounded: %s" % (
            asm.Name, len(parts), len(joints), grounded or "NONE -- the solver has nothing to hold still"))
        return parts, joints, grounded

    def _asm_ref(asm, text):
        cname, _, elem = text.strip().partition(":")
        if not cname:
            raise _Fail("element must be 'Component:Element', e.g. 'L_Bracket:Face6'")
        comp0 = _obj(asm.Document, cname, "component")
        sub = (cname + "." + elem) if elem else cname
        comp, new_sub = UtilsAssembly.getComponentReference(asm, asm, sub)
        if comp is None:
            raise _Fail("%s is not a component of %s (its direct children are %s)" % (
                cname, asm.Name, [o.Name for o in _asm_parts(asm)]))
        if new_sub:
            if comp.getSubObject(new_sub) is None:
                raise _Fail("%s has no element %r" % (comp.Name, new_sub))
            # The connector needs the element twice: the element itself and
            # the "vertex" slot. Repeating the element puts the connector at
            # its centre (face centre, edge midpoint or circle centre).
            return [comp, [new_sub, new_sub]]
        return [comp, ["", ""]]

""")


@_tool(idempotent=True)
def assembly_ground(assembly: str, component: str, document: str = "") -> str:
    """Ground a component of an Assembly so the solver holds it still.

    An assembly with no grounded part has nothing to solve against: joints
    then move both parts arbitrarily. Ground the base part first.

    Args:
        assembly: Assembly::AssemblyObject name.
        component: A direct child of the assembly (usually an App::Link).
        document: Document name. Empty string for the active document.
    """
    return _call(_ASM_HELPERS + textwrap.dedent("""\
        doc = _doc({doc!r})
        asm = _asm(doc, {asm!r})
        comp = _obj(doc, {comp!r}, "component")
        if comp not in _asm_parts(asm):
            raise _Fail("%s is not a direct child of %s" % (comp.Name, asm.Name))
        jg = UtilsAssembly.getJointGroup(asm)
        if any(getattr(o, "ObjectToGround", None) == comp for o in jg.Group):
            print(comp.Name, "is already grounded")
        else:
            g = jg.newObject("App::FeaturePython", "GroundedJoint")
            JointObject.GroundedJoint(g, comp)
            JointObject.ViewProviderGroundedJoint(g.ViewObject)
            doc.recompute()
            print("grounded", comp.Name, "with", g.Name)
        _asm_status(asm)
    """).format(doc=document, asm=assembly, comp=component))


@_tool()
def assembly_add_joint(
    assembly: str,
    kind: str,
    element_a: str,
    element_b: str,
    distance: float = 0.0,
    offset: float = 0.0,
    rotation: float = 0.0,
    name: str = "",
    document: str = "",
) -> str:
    """Join two components of an Assembly with a solver joint, then solve.

    Elements are "Component:Element", e.g. "L_Bracket:Face6",
    "L_Shaft:Edge3" (a circular edge gives its axis) or "L_Pin:Vertex2".
    The joint connector sits at the element's centre with its Z along the
    face normal or edge axis. Both components' placements are reported
    after solving; check that the right one moved.

    Args:
        assembly: Assembly::AssemblyObject name.
        kind: Fixed, Revolute, Cylindrical, Slider, Ball, Distance, Parallel,
            Perpendicular, Angle, RackPinion, Screw, Gears or Belt.
        element_a: First connector, usually on the already-placed part.
        element_b: Second connector, on the part that should move.
        distance: Distance value for Distance / RackPinion / Screw / Gears / Belt.
        offset: Shift of the second connector along its normal, in mm.
        rotation: Rotation of the second connector about its normal, in degrees.
        name: Label for the joint.
        document: Document name. Empty string for the active document.
    """
    return _call(_ASM_HELPERS + textwrap.dedent("""\
        doc = _doc({doc!r})
        asm = _asm(doc, {asm!r})
        kinds = {{k.lower(): k for k in JointObject.JointTypes}}
        kind = kinds.get({kind!r}.replace(" ", "").lower())
        if kind is None:
            raise _Fail("kind must be one of %s" % list(JointObject.JointTypes))
        ra = _asm_ref(asm, {a!r})
        rb = _asm_ref(asm, {b!r})
        if ra[0] == rb[0]:
            raise _Fail("both elements are on %s; a joint needs two different components" % ra[0].Name)
        before = {{o.Name: App.Placement(o.Placement) for o in _asm_parts(asm)}}
        jg = UtilsAssembly.getJointGroup(asm)
        j = jg.newObject("App::FeaturePython", "Joint")
        j.Label = {name!r} or kind
        JointObject.Joint(j, JointObject.JointTypes.index(kind))
        JointObject.ViewProviderJoint(j.ViewObject)
        if {off!r} or {rot!r}:
            j.Offset2 = App.Placement(App.Vector(0, 0, {off!r}), App.Rotation(App.Vector(0, 0, 1), {rot!r}))
        if {dist!r} and "Distance" in j.PropertiesList:
            j.Distance = {dist!r}
        j.Proxy.setJointConnectors(j, [ra, rb])
        doc.recompute()
        code = asm.solve(False)
        doc.recompute()
        print("created", j.Name, "(" + kind + ")", "label", j.Label)
        print("  connector A:", ra[0].Name, ra[1][0] or "(origin)", "at", _vstr(j.Placement1.Base))
        print("  connector B:", rb[0].Name, rb[1][0] or "(origin)", "at", _vstr(j.Placement2.Base))
        print("solve:", code, "(0 = solved)" if code == 0 else "<-- SOLVER FAILED; the joint may be over-constrained or unreachable")
        for o in _asm_parts(asm):
            moved = (o.Placement.Base - before[o.Name].Base).Length > 1e-6 or \\
                    abs(o.Placement.Rotation.Angle - before[o.Name].Rotation.Angle) > 1e-6
            s = _shape_of(o)
            print("  %-20s at %s%s%s" % (o.Name, _vstr(o.Placement.Base), "  MOVED" if moved else "",
                                        ("  bbox " + _bbox_str(s)) if s is not None else ""))
        _asm_status(asm)
    """).format(doc=document, asm=assembly, kind=kind, a=element_a, b=element_b,
                dist=distance, off=offset, rot=rotation, name=name))


@_tool(idempotent=True)
def assembly_solve(assembly: str, document: str = "") -> str:
    """Run the assembly solver and report every component's placement.

    Args:
        assembly: Assembly::AssemblyObject name.
        document: Document name. Empty string for the active document.
    """
    return _call(_ASM_HELPERS + textwrap.dedent("""\
        doc = _doc({doc!r})
        asm = _asm(doc, {asm!r})
        code = asm.solve(False)
        doc.recompute()
        print("solve:", code, "(0 = solved)" if code == 0 else "<-- SOLVER FAILED")
        parts, joints, grounded = _asm_status(asm)
        for o in parts:
            print("  %-20s at %s  rot %.2f deg about %s" % (
                o.Name, _vstr(o.Placement.Base), o.Placement.Rotation.Angle * 57.29578, _vstr(o.Placement.Rotation.Axis)))
        for j in joints:
            print("  joint %-16s %-12s %s <-> %s" % (
                j.Label, j.JointType,
                (j.Reference1[0].Name + ":" + j.Reference1[1][0]) if j.Reference1 else "?",
                (j.Reference2[0].Name + ":" + j.Reference2[1][0]) if j.Reference2 else "?"))
    """).format(doc=document, asm=assembly))


@_tool(read_only=True, idempotent=True)
def assembly_bom(assembly: str, document: str = "") -> str:
    """Bill of materials: every part instanced in the assembly with quantities.

    Link arrays count as their element count; nested assemblies and App::Part
    containers are walked.

    Args:
        assembly: Assembly::AssemblyObject or App::Part name.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        asm = _obj(doc, {asm!r}, "assembly")
        rows = {{}}
        def walk(container, mult):
            for o in getattr(container, "Group", []):
                if o.TypeId in ("Assembly::JointGroup", "Assembly::BomGroup", "Assembly::ViewGroup"):
                    continue
                if hasattr(o, "JointType") or hasattr(o, "ObjectToGround"):
                    continue
                if o.TypeId == "App::Link":
                    src = o.LinkedObject
                    n = (o.ElementCount or 1) * mult
                    if src is not None and src.TypeId in ("App::Part", "Assembly::AssemblyObject"):
                        walk(src, n)
                        continue
                    key = (src.Label if src else "?", src.Name if src else "?",
                           src.Document.Name if src and src.Document != doc else "")
                    rows[key] = rows.get(key, 0) + n
                elif o.TypeId in ("App::Part", "Assembly::AssemblyObject"):
                    walk(o, mult)
                else:
                    key = (o.Label, o.Name, "")
                    rows[key] = rows.get(key, 0) + mult
        walk(asm, 1)
        print("  %-4s %-28s %-24s %s" % ("qty", "part", "source object", "document"))
        for (label, name, extdoc), qty in sorted(rows.items(), key=lambda kv: (-kv[1], kv[0][0])):
            print("  %-4d %-28s %-24s %s" % (qty, label, name, extdoc or "(this)"))
        print(sum(rows.values()), "instance(s) of", len(rows), "unique part(s)")
    """).format(doc=document, asm=assembly))


# ── CAM ────────────────────────────────────────────────────────────

_CAM_HELPERS = textwrap.dedent("""\
    def _job(doc, name):
        j = _obj(doc, name, "CAM job")
        if not (hasattr(j, "Operations") and hasattr(j, "Stock") and hasattr(j, "Tools")):
            raise _Fail("%s (%s) is not a CAM Job" % (j.Name, j.TypeId))
        return j

    def _job_summary(job):
        ops = list(job.Operations.Group)
        print("job %s (label %s): live in GUI: %s, integrity: %s" % (
            job.Name, job.Label, job.ViewObject.Proxy is not None,
            job.Proxy.integrityCheck(job) if hasattr(job.Proxy, "integrityCheck") else "?"))
        print("  model:", [(c.Name, getattr(c, "Objects", [None])[0].Name if getattr(c, "Objects", None) else "?") for c in job.Model.Group])
        st = job.Stock
        print("  stock:", st.Name, st.StockType, _bbox_str(st.Shape) if st.Shape and not st.Shape.isNull() else "(no shape)")
        print("  tools:", [(t.Name, t.ToolNumber, t.Tool.Label, str(t.Tool.Diameter)) for t in job.Tools.Group])
        print("  operations:", [(o.Name, round(o.Path.Length, 1) if hasattr(o, "Path") else "-") for o in ops])
        print("  post processor:", job.PostProcessor or "(none)", " args:", job.PostProcessorArgs or "(none)")

    def _op_summary(op):
        p = op.Path
        bad = [t for t in op.State if t in ("Invalid", "Error")]
        print("  path: %.1f mm, %d commands%s" % (p.Length, len(p.Commands), ("  STATE: " + ",".join(bad)) if bad else ""))
        if p.Length > 0:
            print("  toolpath bbox:", _bbox_str(p))
        else:
            print("  WARNING: the toolpath is EMPTY -- nothing will be cut. Check Base faces, depths and tool diameter.")

""")


@_tool()
def cam_create_job(
    models: str,
    stock: str = "from_base",
    stock_margins: str = "1,1,1",
    stock_size: str = "",
    post_processor: str = "grbl",
    name: str = "",
    document: str = "",
) -> str:
    """Create a CAM Job through the GUI layer, so it is live in the tree.

    A job built with the App-layer Create() looks complete from Python but
    is inert in the GUI: nothing nests under it and Post Process cannot
    drive it. This uses the GUI path with the task panel suppressed.

    Args:
        models: Comma-separated objects to machine (Bodies or shapes).
        stock: "from_base" (model bounding box plus margins) or "box".
        stock_margins: For from_base: "x,y,z" or "xneg,xpos,yneg,ypos,zneg,zpos" in mm.
        stock_size: For box: "length,width,height" in mm, placed at the origin.
        post_processor: e.g. "grbl", "linuxcnc", "mach3_mach4", "fanuc".
        name: Label for the job.
        document: Document name. Empty string for the active document.
    """
    return _call(_CAM_HELPERS + textwrap.dedent("""\
        import Path.Main.Gui.Job as JG
        import Path.Main.Stock as Stock
        doc = _doc({doc!r})
        App.setActiveDocument(doc.Name)
        objs = [_obj(doc, n) for n in _names({models!r})]
        if not objs:
            raise _Fail("no model objects given")
        for o in objs:
            if _shape_of(o) is None:
                raise _Fail("%s has no shape to machine" % o.Name)
        job = JG.Create(objs, None, openTaskPanel=False)
        if job is None:
            raise _Fail("job creation failed; see the FreeCAD errors above")
        if {name!r}:
            job.Label = {name!r}
        if {stock!r} == "from_base":
            m = [float(x) for x in {margins!r}.split(",") if x.strip()]
            if len(m) == 3:
                m = [m[0], m[0], m[1], m[1], m[2], m[2]]
            if len(m) != 6:
                raise _Fail("stock_margins must be 'x,y,z' or six values")
            st = job.Stock
            st.ExtXneg, st.ExtXpos, st.ExtYneg, st.ExtYpos, st.ExtZneg, st.ExtZpos = m
        elif {stock!r} == "box":
            dims = [float(x) for x in {size!r}.split(",") if x.strip()]
            if len(dims) != 3:
                raise _Fail("stock_size must be 'length,width,height' for a box stock")
            old = job.Stock
            job.Stock = Stock.CreateBox(job, extent=App.Vector(*dims))
            doc.removeObject(old.Name)
        else:
            raise _Fail("stock must be from_base or box")
        posts = job.getEnumerationsOfProperty("PostProcessor")
        if {post!r} and {post!r} not in posts:
            raise _Fail("post_processor must be one of %s" % [p for p in posts if p])
        job.PostProcessor = {post!r}
        doc.recompute()
        _job_summary(job)
        print("next: cam_add_tool to size the default tool, then cam_add_operation")
    """).format(doc=document, models=models, stock=stock, margins=stock_margins,
                size=stock_size, post=post_processor, name=name))


@_tool()
def cam_add_tool(
    job: str,
    diameter: float,
    shape: str = "endmill",
    flutes: int = 2,
    spindle_speed: float = 12000.0,
    horizontal_feed: float = 800.0,
    vertical_feed: float = 300.0,
    tool_number: int = 0,
    label: str = "",
    retune_default: bool = False,
    document: str = "",
) -> str:
    """Add a tool (ToolBit + ToolController) to a CAM job, or retune its default one.

    Every job is created with its own 5 mm endmill controller. With
    retune_default=True that one is resized in place instead of adding a
    second; this is the right choice for a one-tool job, because deleting a
    controller leaves its ToolBit body orphaned in the tree. A ToolBit is
    never shared between jobs.

    Args:
        job: CAM Job name.
        diameter: Tool diameter in mm.
        shape: endmill, ballend, bullnose, chamfer, drill, v-bit, slittingsaw,
            dovetail, radius, reamer, tap, taperedballnose, thread-mill or probe.
        flutes: Number of flutes.
        spindle_speed: RPM.
        horizontal_feed: Cutting feed in mm/min.
        vertical_feed: Plunge feed in mm/min.
        tool_number: T number. 0 picks the next free one.
        label: Label for the tool. Defaults to "<diameter>mm <shape>".
        retune_default: Modify the job's default controller instead of adding one.
        document: Document name. Empty string for the active document.
    """
    return _call(_CAM_HELPERS + textwrap.dedent("""\
        import Path.Tool.Bit as TB
        import Path.Tool.Controller as TC
        doc = _doc({doc!r})
        job = _job(doc, {job!r})
        label = {label!r} or ("%gmm %s" % ({dia!r}, {shape!r}))
        if {retune!r}:
            if not job.Tools.Group:
                raise _Fail("%s has no tool controller to retune" % job.Name)
            tc = job.Tools.Group[0]
            bit = tc.Tool
            how = "retuned default"
        else:
            try:
                bit = TB.ToolBit.from_shape_id({shape!r}).attach_to_doc(doc)
            except Exception as e:
                raise _Fail("cannot create a %r tool bit: %s" % ({shape!r}, e))
            number = {num!r} or (max([t.ToolNumber for t in job.Tools.Group] + [0]) + 1)
            tc = TC.Create("TC: " + label, tool=bit, toolNumber=number)
            job.Proxy.addToolController(tc)
            how = "added"
        bit.Diameter = "%g mm" % {dia!r}
        if "Flutes" in bit.PropertiesList:
            bit.Flutes = {flutes!r}
        bit.Label = label
        tc.Label = "TC: " + label
        tc.SpindleSpeed = float({rpm!r})
        tc.HorizFeed = "%g mm/min" % {hf!r}
        tc.VertFeed = "%g mm/min" % {vf!r}
        doc.recompute()
        print(how, tc.Name, "T%d" % tc.ToolNumber, "->", bit.Name, "(" + bit.Label + ")",
              "diameter", bit.Diameter, "flutes", getattr(bit, "Flutes", "-"))
        print("  spindle", tc.SpindleSpeed, "rpm  feed", tc.HorizFeed, " plunge", tc.VertFeed)
        print("  job tools:", [(t.Name, t.ToolNumber, str(t.Tool.Diameter)) for t in job.Tools.Group])
    """).format(doc=document, job=job, dia=diameter, shape=shape, flutes=flutes, rpm=spindle_speed,
                hf=horizontal_feed, vf=vertical_feed, num=tool_number, label=label,
                retune=retune_default))


@_tool(read_only=True, idempotent=True)
def cam_list_tools(job: str, document: str = "") -> str:
    """List a CAM job's tool controllers with their bits, feeds and speeds.

    Args:
        job: CAM Job name.
        document: Document name. Empty string for the active document.
    """
    return _call(_CAM_HELPERS + textwrap.dedent("""\
        doc = _doc({doc!r})
        job = _job(doc, {job!r})
        print("  %-3s %-24s %-24s %-10s %-7s %-14s %-14s %s" % ("T", "controller", "bit", "diameter", "flutes", "feed", "plunge", "rpm"))
        for t in job.Tools.Group:
            b = t.Tool
            print("  %-3d %-24s %-24s %-10s %-7s %-14s %-14s %g" % (
                t.ToolNumber, t.Name, b.Label, b.Diameter, getattr(b, "Flutes", "-"),
                t.HorizFeed, t.VertFeed, t.SpindleSpeed))
        print(len(job.Tools.Group), "tool controller(s)")
    """).format(doc=document, job=job))


@_tool()
def cam_add_operation(
    job: str,
    kind: str = "profile",
    base: str = "",
    tool_controller: str = "",
    start_depth: Optional[float] = None,
    final_depth: Optional[float] = None,
    step_down: float = 0.0,
    side: str = "",
    direction: str = "",
    name: str = "",
    document: str = "",
) -> str:
    """Add a machining operation to a CAM job and report its toolpath.

    The toolpath length and bounding box are reported; an empty toolpath is
    flagged, because an operation that cuts nothing otherwise looks fine.

    Args:
        job: CAM Job name.
        kind: profile, pocket, drilling, adaptive, engrave, facing, helix,
            slot, deburr, vcarve, surface or pocketshape.
        base: Comma-separated "Object:FaceN" (or "Object:EdgeN") references on
            the job's model objects. Empty machines the whole model (profile).
        tool_controller: Tool controller name. Default: the job's first.
        start_depth: Z to start cutting at, in mm. Default: stock top.
        final_depth: Z to finish at, in mm. Default: model bottom.
        step_down: Depth per pass in mm. Default: tool diameter.
        side: profile only: "Outside" or "Inside".
        direction: "CW" or "CCW" where the operation has one.
        name: Name for the operation.
        document: Document name. Empty string for the active document.
    """
    return _call(_CAM_HELPERS + textwrap.dedent("""\
        import importlib
        import Path.Op.Gui.Base as PathOpGui
        doc = _doc({doc!r})
        App.setActiveDocument(doc.Name)
        job = _job(doc, {job!r})
        kinds = {{"profile": "Profile", "pocket": "Pocket", "drilling": "Drilling", "adaptive": "Adaptive",
                 "engrave": "Engrave", "facing": "MillFace", "helix": "Helix", "slot": "Slot",
                 "deburr": "Deburr", "vcarve": "Vcarve", "surface": "Surface", "pocketshape": "PocketShape"}}
        if {kind!r} not in kinds:
            raise _Fail("kind must be one of %s" % sorted(kinds))
        mod = importlib.import_module("Path.Op.Gui." + kinds[{kind!r}])
        res = mod.Command.res
        tc = _obj(doc, {tc!r}, "tool controller") if {tc!r} else (job.Tools.Group[0] if job.Tools.Group else None)
        if tc is None:
            raise _Fail("the job has no tool controller; run cam_add_tool first")
        bases = []
        for ref in _names({base!r}):
            oname, _, sub = ref.replace(".", ":").partition(":")
            o = _obj(doc, oname)
            target = None
            for c in job.Model.Group:
                if c == o or o in (getattr(c, "Objects", None) or []):
                    target = c
                    break
            if target is None:
                raise _Fail("%s is not in the job's model (which holds %s)" % (
                    o.Name, [(c.Name, (getattr(c, "Objects", None) or [None])[0].Name if getattr(c, "Objects", None) else "?") for c in job.Model.Group]))
            if sub:
                try:
                    found = target.Shape.getElement(sub)
                except Exception:
                    found = None
                if found is None:
                    raise _Fail("%s has no element %s (it has %d faces, %d edges)" % (
                        target.Name, sub, len(target.Shape.Faces), len(target.Shape.Edges)))
            for i, (t, subs) in enumerate(bases):
                if t == target:
                    bases[i] = (t, subs + ([sub] if sub else []))
                    break
            else:
                bases.append((target, [sub] if sub else []))
        # With more than one tool controller FreeCAD opens a "Tool Controller
        # Selection" dialog while creating the operation. The dialog guard
        # would answer it with Cancel and the creation would fail, so answer
        # it here with the controller that was asked for.
        import Path.Op.Base as OpBase
        saved = OpBase.PathUtils.findToolController
        OpBase.PathUtils.findToolController = lambda *a, **k: tc
        try:
            op = res.objFactory({name!r} or (kinds[{kind!r}] + "_" + job.Label), obj=None, parentJob=job)
        finally:
            OpBase.PathUtils.findToolController = saved
        op.ViewObject.Proxy = PathOpGui.ViewProvider(op.ViewObject, res)
        op.ToolController = tc
        if bases and "Base" in op.PropertiesList:
            op.Base = bases
        if {sd!r} is not None:
            op.setExpression("StartDepth", None)
            op.StartDepth = {sd!r}
        if {fd!r} is not None:
            op.setExpression("FinalDepth", None)
            op.FinalDepth = {fd!r}
        if {step!r} > 0 and "StepDown" in op.PropertiesList:
            op.setExpression("StepDown", None)
            op.StepDown = {step!r}
        for prop, val in (("Side", {side!r}), ("Direction", {dirn!r})):
            if val:
                if prop not in op.PropertiesList:
                    raise _Fail("%s has no %s setting" % (kinds[{kind!r}], prop))
                allowed = op.getEnumerationsOfProperty(prop)
                if val not in allowed:
                    raise _Fail("%s must be one of %s" % (prop.lower(), allowed))
                setattr(op, prop, val)
        doc.recompute()
        print("created", op.Name, "(" + kinds[{kind!r}] + ") in", job.Name, "with", tc.Name)
        print("  base:", [(t.Name, s) for t, s in bases] or "whole model")
        print("  depths: start", op.StartDepth, " final", op.FinalDepth,
              " step", getattr(op, "StepDown", "-"), " side", getattr(op, "Side", "-"), " direction", getattr(op, "Direction", "-"))
        _op_summary(op)
    """).format(doc=document, job=job, kind=kind, base=base, tc=tool_controller, sd=start_depth,
                fd=final_depth, step=step_down, side=side, dirn=direction, name=name))


@_tool()
def cam_add_dressup(operation: str, kind: str = "dogbone", name: str = "", document: str = "") -> str:
    """Wrap an operation in a dress-up: dogbone corners, holding tags or a boundary.

    Model corners square and let the dogbone dress-up add the relief: a
    dogbone modelled into the part does not survive the toolpath offset.

    Args:
        operation: Operation name.
        kind: "dogbone", "tags" or "boundary".
        name: Name for the dress-up object.
        document: Document name. Empty string for the active document.
    """
    return _call(_CAM_HELPERS + textwrap.dedent("""\
        import importlib
        doc = _doc({doc!r})
        App.setActiveDocument(doc.Name)
        op = _obj(doc, {op!r}, "operation")
        if not hasattr(op, "Path"):
            raise _Fail("%s (%s) is not a CAM operation" % (op.Name, op.TypeId))
        mods = {{"dogbone": ("Path.Dressup.Gui.DogboneII", "DressupDogbone"),
                "tags": ("Path.Dressup.Gui.Tags", "DressupTag"),
                "boundary": ("Path.Dressup.Gui.Boundary", "DressupPathBoundary")}}
        if {kind!r} not in mods:
            raise _Fail("kind must be dogbone, tags or boundary")
        modname, default = mods[{kind!r}]
        before = op.Path.Length
        m = importlib.import_module(modname)
        dr = m.Create(op, {name!r} or default)
        doc.recompute()
        print("created", dr.Name, "(" + {kind!r} + ") around", op.Name)
        print("  path length: %.1f -> %.1f mm" % (before, dr.Path.Length))
        _op_summary(dr)
        job = next((o for o in dr.InListRecursive if hasattr(o, "Operations") and hasattr(o, "Stock")), None)
        if job is not None:
            print("  job operations now:", [o.Name for o in job.Operations.Group])
    """).format(doc=document, op=operation, kind=kind, name=name))


@_tool(idempotent=True)
def cam_post(job: str, file_path: str, post_processor: str = "", document: str = "") -> str:
    """Post-process a CAM job to G-code, write the file and verify it.

    The Python export() returns the G-code instead of writing it, and the
    job's output-file setting is ignored, so this writes the file itself.
    The G-code editor is suppressed for the duration and the job's own
    arguments are restored afterwards, so the user's Post Process button
    still shows the editor.

    Args:
        job: CAM Job name.
        file_path: Destination .nc / .gcode path.
        post_processor: Override the job's post processor (e.g. "grbl").
        document: Document name. Empty string for the active document.
    """
    return _call(_CAM_HELPERS + textwrap.dedent("""\
        import re
        import Path.Post.Processor as PP
        doc = _doc({doc!r})
        job = _job(doc, {job!r})
        if {post!r}:
            posts = job.getEnumerationsOfProperty("PostProcessor")
            if {post!r} not in posts:
                raise _Fail("post_processor must be one of %s" % [p for p in posts if p])
            job.PostProcessor = {post!r}
        if not job.PostProcessor:
            raise _Fail("the job has no post processor; pass post_processor")
        if not job.Operations.Group:
            raise _Fail("the job has no operations")
        saved = job.PostProcessorArgs
        if "--no-show-editor" not in saved:
            job.PostProcessorArgs = (saved + " --no-show-editor").strip()
        try:
            sections = PP.PostProcessorFactory.get_post_processor(job, job.PostProcessor).export()
        finally:
            job.PostProcessorArgs = saved
        if not sections:
            raise _Fail("the post processor returned nothing")
        gcode = "\\n".join(s[1] if isinstance(s, (tuple, list)) else str(s) for s in sections)
        path = {path!r}
        with open(path, "w") as fh:
            fh.write(gcode)
        if not _os.path.exists(path) or _os.path.getsize(path) == 0:
            raise _Fail("post reported success but %s is missing or empty" % path)
        lines = [ln for ln in gcode.splitlines() if ln.strip()]
        zs = [float(m) for m in re.findall(r"(?<![A-Za-z])Z(-?\\d+\\.?\\d*)", gcode)]
        xs = [float(m) for m in re.findall(r"(?<![A-Za-z])X(-?\\d+\\.?\\d*)", gcode)]
        ys = [float(m) for m in re.findall(r"(?<![A-Za-z])Y(-?\\d+\\.?\\d*)", gcode)]
        fs = sorted(set(float(m) for m in re.findall(r"(?<![A-Za-z])F(\\d+\\.?\\d*)", gcode)))
        print("wrote", path, _os.path.getsize(path), "bytes,", len(lines), "lines, post", job.PostProcessor,
              "sections", len(sections))
        print("  units: %s" % ("mm (G21)" if "G21" in gcode else ("INCH (G20)" if "G20" in gcode else "not declared")))
        if zs:
            print("  Z range: %.3f .. %.3f" % (min(zs), max(zs)))
        if xs and ys:
            print("  XY extents: X %.3f..%.3f  Y %.3f..%.3f" % (min(xs), max(xs), min(ys), max(ys)))
        print("  feeds:", fs or "none")
        if zs and min(zs) >= 0:
            print("  WARNING: no move goes below Z=0; check final depth")
    """).format(doc=document, job=job, path=file_path, post=post_processor))


@_tool(read_only=True, idempotent=True)
def cam_audit(document: str = "") -> str:
    """Audit every CAM job in a document for the silent failures.

    Reports jobs that are inert in the GUI, empty toolpaths, operations
    whose base faces decayed to "?FaceN", tool bits shared between jobs,
    and orphan operations outside any job.

    Args:
        document: Document name. Empty string for the active document.
    """
    return _call(_CAM_HELPERS + textwrap.dedent("""\
        doc = _doc({doc!r})
        jobs = [o for o in doc.Objects if hasattr(o, "Operations") and hasattr(o, "Stock") and hasattr(o, "Tools")]
        problems = 0
        in_job = set()
        bit_jobs = {{}}
        for job in jobs:
            print("job", job.Name, "(" + job.Label + ")")
            if job.ViewObject is None or job.ViewObject.Proxy is None:
                print("  PROBLEM: no ViewProvider -- built at the App layer, inert in the GUI")
                problems += 1
            ok = job.Proxy.integrityCheck(job) if hasattr(job.Proxy, "integrityCheck") else None
            if ok is False:
                print("  PROBLEM: integrityCheck failed")
                problems += 1
            for t in job.Tools.Group:
                bit_jobs.setdefault(t.Tool.Name, set()).add(job.Name)
            def walk(ops):
                for op in ops:
                    in_job.add(op.Name)
                    length = op.Path.Length if hasattr(op, "Path") else 0
                    flags = []
                    if length == 0:
                        flags.append("EMPTY toolpath")
                    base = getattr(op, "Base", None)
                    if isinstance(base, (list, tuple)):
                        for t, subs in base:
                            for s in subs:
                                if s.startswith("?"):
                                    flags.append("base %s:%s decayed" % (t.Name, s))
                    bad = [t for t in op.State if t in ("Invalid", "Error")]
                    if bad:
                        flags.append("state " + ",".join(bad))
                    print("  op %-24s %8.1f mm %s" % (op.Name, length, ("  PROBLEM: " + "; ".join(flags)) if flags else ""))
                    # A dress-up wraps its operation in Base; audit that too.
                    if base is not None and not isinstance(base, (list, tuple)) and hasattr(base, "Path"):
                        walk([base])
            walk(job.Operations.Group)
            problems += sum(1 for op in job.Operations.Group if hasattr(op, "Path") and op.Path.Length == 0)
        for bit, js in bit_jobs.items():
            if len(js) > 1:
                print("PROBLEM: tool bit %s is shared by jobs %s -- the tree can claim it only once" % (bit, sorted(js)))
                problems += 1
        def is_op(o):
            # Jobs, tool controllers and stock are Path features too; an
            # operation is one with a Path and a ToolController of its own.
            return (o.TypeId == "Path::FeaturePython" and hasattr(o, "Path")
                    and "ToolController" in o.PropertiesList and not hasattr(o, "Operations"))
        orphans = [o.Name for o in doc.Objects if is_op(o) and o.Name not in in_job
                   and not any(hasattr(p, "Path") for p in o.InList)]
        if orphans:
            print("PROBLEM: orphan operations outside any job:", orphans)
            problems += len(orphans)
        print("%d job(s), %d problem(s)" % (len(jobs), problems))
    """).format(doc=document))


# ── TechDraw ───────────────────────────────────────────────────────

_VIEW_DIRECTIONS = {
    "top": ((0, 0, 1), (1, 0, 0)), "bottom": ((0, 0, -1), (1, 0, 0)),
    "front": ((0, -1, 0), (1, 0, 0)), "rear": ((0, 1, 0), (-1, 0, 0)),
    "right": ((1, 0, 0), (0, 1, 0)), "left": ((-1, 0, 0), (0, -1, 0)),
    "iso": ((1, -1, 1), (1, 1, 0)),
}


@_tool()
def drawing_create_page(template: str = "A4_Landscape", name: str = "Page", document: str = "") -> str:
    """Create a TechDraw page from a template.

    Args:
        template: Template name matched against FreeCAD's TechDraw templates
            (e.g. "A4_Landscape", "A3_Landscape", "Letter") or a path to an SVG.
        name: Name for the page.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        tdir = _os.path.join(App.getResourceDir(), "Mod", "TechDraw", "Templates")
        t = {tpl!r}
        if _os.path.isabs(t):
            path = t
        else:
            files = sorted(f for f in _os.listdir(tdir) if f.lower().endswith(".svg"))
            hits = [f for f in files if f.lower() == t.lower() + ".svg"] or \\
                   [f for f in files if t.lower() in f.lower()]
            if not hits:
                raise _Fail("no template matches %r; available: %s" % (t, files))
            path = _os.path.join(tdir, hits[0])
        if not _os.path.exists(path):
            raise _Fail("template file not found: %s" % path)
        page = doc.addObject("TechDraw::DrawPage", {name!r})
        tpl = doc.addObject("TechDraw::DrawSVGTemplate", page.Name + "Template")
        tpl.Template = path
        page.Template = tpl
        doc.recompute()
        print("created page", page.Name, "with template", _os.path.basename(path))
        print("  size: %.0f x %.0f mm" % (tpl.Width, tpl.Height))
    """).format(doc=document, tpl=template, name=name))


@_tool()
def drawing_add_view(
    page: str,
    sources: str,
    direction: str = "iso",
    scale: float = 1.0,
    x: float = 100.0,
    y: float = 100.0,
    name: str = "View",
    document: str = "",
) -> str:
    """Add a projected view of objects to a TechDraw page.

    Args:
        page: TechDraw page name.
        sources: Comma-separated objects to draw.
        direction: top, bottom, front, rear, right, left, iso, or "x,y,z".
        scale: Drawing scale (1.0 = full size).
        x: Position on the page in mm from the left.
        y: Position on the page in mm from the bottom.
        name: Name for the view.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        page = _obj(doc, {page!r}, "page")
        if page.TypeId != "TechDraw::DrawPage":
            raise _Fail("%s is a %s, not a TechDraw page" % (page.Name, page.TypeId))
        srcs = [_obj(doc, n) for n in _names({srcs!r})]
        dirs = {dirs!r}
        d = {dirn!r}.strip().lower()
        if d in dirs:
            vdir, xdir = App.Vector(*dirs[d][0]), App.Vector(*dirs[d][1])
        else:
            try:
                vdir = App.Vector(*[float(v) for v in d.split(",")])
            except Exception:
                raise _Fail("direction must be one of %s or 'x,y,z'" % sorted(dirs))
            up = App.Vector(0, 0, 1) if abs(vdir.normalize().dot(App.Vector(0, 0, 1))) < 0.99 else App.Vector(0, 1, 0)
            xdir = up.cross(vdir)
        v = doc.addObject("TechDraw::DrawViewPart", {name!r})
        page.addView(v)
        v.Source = srcs
        v.Direction = vdir.normalize()
        v.XDirection = xdir.normalize()
        v.ScaleType = "Custom"
        v.Scale = {scale!r}
        v.X = {x!r}
        v.Y = {y!r}
        doc.recompute()
        print("added view", v.Name, "of", [s.Name for s in srcs], "to", page.Name)
        print("  direction", _vstr(v.Direction), " scale", v.Scale, " at (%g, %g)" % (v.X, v.Y))
        print(" ", _state_str(v))
    """).format(doc=document, page=page, srcs=sources, dirn=direction, scale=scale, x=x, y=y,
                name=name, dirs=_VIEW_DIRECTIONS))


@_tool()
def drawing_add_section(
    page: str,
    base_view: str,
    direction: str = "Down",
    origin: str = "0,0,0",
    normal: str = "",
    x: float = 200.0,
    y: float = 100.0,
    name: str = "Section",
    document: str = "",
) -> str:
    """Add a section view cut through a base view's objects.

    Args:
        page: TechDraw page name.
        base_view: The DrawViewPart to section.
        direction: Up, Down, Left, Right or Aligned (arrow direction on the base view).
        origin: "x,y,z" point in model coordinates the cut plane passes through.
        normal: "x,y,z" viewing direction of the section in model coordinates.
            Default follows direction (Down=0,-1,0  Up=0,1,0  Right=1,0,0  Left=-1,0,0).
        x: Position on the page in mm.
        y: Position on the page in mm.
        name: Name for the section view.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        page = _obj(doc, {page!r}, "page")
        base = _obj(doc, {base!r}, "base view")
        if base.TypeId != "TechDraw::DrawViewPart":
            raise _Fail("%s is a %s, not a DrawViewPart" % (base.Name, base.TypeId))
        defaults = {{"down": (0, -1, 0), "up": (0, 1, 0), "right": (1, 0, 0), "left": (-1, 0, 0), "aligned": (0, -1, 0)}}
        d = {dirn!r}.strip().lower()
        if d not in defaults:
            raise _Fail("direction must be Up, Down, Left, Right or Aligned")
        n = App.Vector(*[float(v) for v in {normal!r}.split(",")]) if {normal!r} else App.Vector(*defaults[d])
        o = App.Vector(*[float(v) for v in {origin!r}.split(",")])
        s = doc.addObject("TechDraw::DrawViewSection", {name!r})
        page.addView(s)
        s.Source = base.Source
        s.BaseView = base
        s.SectionDirection = d.capitalize()
        s.SectionNormal = n.normalize()
        s.SectionOrigin = o
        s.ScaleType = "Custom"
        s.Scale = base.Scale
        s.X = {x!r}
        s.Y = {y!r}
        doc.recompute()
        print("added section", s.Name, "of", base.Name, "through", _vstr(o), "looking", _vstr(s.SectionNormal))
        print(" ", _state_str(s))
    """).format(doc=document, page=page, base=base_view, dirn=direction, origin=origin,
                normal=normal, x=x, y=y, name=name))


@_tool()
def drawing_add_dimension(
    page: str,
    view: str,
    kind: str = "Distance",
    references: str = "Edge0,Edge1",
    x: float = 0.0,
    y: float = 0.0,
    document: str = "",
) -> str:
    """Add a dimension to a drawing view and report its measured value.

    Args:
        page: TechDraw page name.
        view: View the dimension belongs to.
        kind: Distance, DistanceX, DistanceY, Radius, Diameter, Angle or Area.
        references: Comma-separated 2D references in the view, e.g. "Edge0,Edge1",
            "Vertex0,Vertex3" or "Edge2" for a radius.
        x: Label offset X on the page in mm.
        y: Label offset Y in mm.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        page = _obj(doc, {page!r}, "page")
        v = _obj(doc, {view!r}, "view")
        dim = doc.addObject("TechDraw::DrawViewDimension", "Dim")
        page.addView(dim)
        kinds = dim.getEnumerationsOfProperty("Type")
        if {kind!r} not in kinds:
            doc.removeObject(dim.Name)
            raise _Fail("kind must be one of %s" % kinds)
        dim.Type = {kind!r}
        dim.References2D = [(v, r) for r in _names({refs!r})]
        dim.MeasureType = "Projected"
        dim.X = {x!r}
        dim.Y = {y!r}
        doc.recompute()
        bad = [t for t in dim.State if t in ("Invalid", "Error")]
        try:
            val = dim.getRawValue()
        except Exception:
            val = None
        print("added", dim.Name, {kind!r}, "on", v.Name, "refs", _names({refs!r}))
        print("  value:", val, " format:", getattr(dim, "FormatSpec", ""), ("  STATE: " + ",".join(bad)) if bad else "")
        if val == 0 or bad:
            print("  WARNING: a zero value or an error means the references do not exist in this"
                  " view or are the same element; TechDraw edge numbering is per view")
    """).format(doc=document, page=page, view=view, kind=kind, refs=references, x=x, y=y))


@_tool(idempotent=True)
def drawing_export(page: str, file_path: str, document: str = "") -> str:
    """Export a TechDraw page to SVG, PDF or DXF, verifying the file.

    Args:
        page: TechDraw page name.
        file_path: Destination path; the extension picks the format.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import TechDraw, TechDrawGui
        doc = _doc({doc!r})
        page = _obj(doc, {page!r}, "page")
        if page.TypeId != "TechDraw::DrawPage":
            raise _Fail("%s is a %s, not a TechDraw page" % (page.Name, page.TypeId))
        path = {path!r}
        ext = _os.path.splitext(path)[1].lower().lstrip(".")
        doc.recompute()
        Gui.updateGui()
        if _os.path.exists(path):
            _os.remove(path)
        if ext == "svg":
            TechDrawGui.exportPageAsSvg(page, path)
        elif ext == "pdf":
            TechDrawGui.exportPageAsPdf(page, path)
        elif ext == "dxf":
            TechDraw.writeDXFPage(page, path)
        else:
            raise _Fail("extension must be svg, pdf or dxf")
        if not _os.path.exists(path) or _os.path.getsize(path) == 0:
            raise _Fail("export reported success but %s is missing or empty" % path)
        print("exported", page.Name, "to", path, _os.path.getsize(path), "bytes;",
              len(page.Views), "view(s)")
    """).format(doc=document, page=page, path=file_path))


# ── View control ───────────────────────────────────────────────────

_VIEW_CODE = textwrap.dedent("""\
    def _view3d():
        gd = Gui.ActiveDocument
        if gd is None:
            raise _Fail("no active document in the GUI")
        v = gd.ActiveView
        if v is None or not hasattr(v, "viewIsometric"):
            views = gd.mdiViewsOfType("Gui::View3DInventor")
            if not views:
                raise _Fail("the active document has no 3D view")
            v = views[0]
        return v

    def _apply_view(v, direction, fit):
        names = {"iso": "viewIsometric", "isometric": "viewIsometric", "front": "viewFront",
                 "top": "viewTop", "right": "viewRight", "left": "viewLeft", "rear": "viewRear",
                 "back": "viewRear", "bottom": "viewBottom", "dimetric": "viewDimetric",
                 "trimetric": "viewTrimetric"}
        d = direction.strip().lower()
        if d:
            if d in names:
                getattr(v, names[d])()
            else:
                try:
                    v.setViewDirection(tuple(float(x) for x in d.split(",")))
                except Exception:
                    raise _Fail("direction must be one of %s or 'x,y,z'" % sorted(names))
        if fit:
            v.fitAll()
        Gui.updateGui()

""")


@_tool(idempotent=True)
def set_view(direction: str = "iso", fit: bool = True) -> str:
    """Point the 3D view along a standard direction and optionally fit all.

    Args:
        direction: iso, front, top, right, left, rear, bottom, dimetric,
            trimetric, or a viewing direction "x,y,z".
        fit: Fit the view to everything visible afterwards.
    """
    return _call(_VIEW_CODE + textwrap.dedent("""\
        v = _view3d()
        _apply_view(v, {dirn!r}, {fit!r})
        print("view:", {dirn!r} or "(unchanged)", " camera:", v.getCameraType(),
              " looking along", _vstr(App.Vector(*v.getViewDirection())))
    """).format(dirn=direction, fit=fit))


@_tool(idempotent=True)
def set_camera(
    position: str = "",
    target: str = "0,0,0",
    orthographic: bool = True,
    fit: bool = False,
) -> str:
    """Place the camera at a position looking at a target, orthographic or perspective.

    Args:
        position: "x,y,z" camera position in mm. Empty keeps the position
            and only changes the projection type.
        target: "x,y,z" point to look at.
        orthographic: True for orthographic, False for perspective.
        fit: Fit the view to everything visible afterwards (overrides the distance).
    """
    return _call(_VIEW_CODE + textwrap.dedent("""\
        from pivy import coin
        v = _view3d()
        if v.getCameraType() != ("Orthographic" if {ortho!r} else "Perspective"):
            # Switching projection replaces the camera node and re-derives its
            # position; let that settle before placing it.
            v.setCameraType("Orthographic" if {ortho!r} else "Perspective")
            Gui.updateGui()
        if {pos!r}:
            p = App.Vector(*[float(x) for x in {pos!r}.split(",")])
            t = App.Vector(*[float(x) for x in {tgt!r}.split(",")])
            if (t - p).Length < 1e-9:
                raise _Fail("position and target coincide")
            up = coin.SbVec3f(0, 0, 1) if abs((t - p).normalize().dot(App.Vector(0, 0, 1))) < 0.99 else coin.SbVec3f(0, 1, 0)
            # The viewer may still be settling a previous fit or projection
            # change when the camera is placed, and that moves it again.
            # Place it, let the viewer process events, and place it once more.
            for _ in range(2):
                cam = v.getCameraNode()
                cam.position.setValue(p.x, p.y, p.z)
                cam.pointAt(coin.SbVec3f(t.x, t.y, t.z), up)
                cam.focalDistance.setValue((t - p).Length)
                Gui.updateGui()
            actual = App.Vector(*v.getCameraNode().position.getValue().getValue())
            print("requested position", _vstr(p), " target", _vstr(t))
            if (actual - p).Length > 1e-3:
                print("WARNING: the viewer moved the camera after placement")
        if {fit!r}:
            v.fitAll()
        Gui.updateGui()
        cam = v.getCameraNode()
        print("camera:", v.getCameraType(), " at", _vstr(App.Vector(*cam.position.getValue().getValue())),
              " looking along", _vstr(App.Vector(*v.getViewDirection())))
    """).format(pos=position, tgt=target, ortho=orthographic, fit=fit))


@_tool(idempotent=True)
def set_selection(
    object_name: str,
    sub_elements: str = "",
    clear: bool = True,
    document: str = "",
) -> str:
    """Select an object, or sub-elements of it, in the GUI.

    Args:
        object_name: Object to select.
        sub_elements: Comma-separated "Face3,Edge7". Empty selects the whole object.
        clear: Clear the current selection first.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        doc = _doc({doc!r})
        o = _obj(doc, {name!r})
        if {clear!r}:
            Gui.Selection.clearSelection()
        subs = _names({subs!r})
        if subs:
            s = _shape_of(o)
            for sub in subs:
                if s is not None:
                    try:
                        found = s.getElement(sub)
                    except Exception:
                        found = None
                    if found is None:
                        raise _Fail("%s has no element %s" % (o.Name, sub))
                Gui.Selection.addSelection(doc.Name, o.Name, sub)
        else:
            Gui.Selection.addSelection(doc.Name, o.Name)
        sel = Gui.Selection.getSelectionEx()
        print("selected:", [(x.ObjectName, list(x.SubElementNames)) for x in sel])
    """).format(doc=document, name=object_name, subs=sub_elements, clear=clear))


@_tool(idempotent=True)
def clear_selection() -> str:
    """Clear the GUI selection."""
    return _call("Gui.Selection.clearSelection()\nprint('selection cleared')\n")


# ── Draft helpers ──────────────────────────────────────────────────


@_tool()
def draft_clone(object_name: str, name: str = "", document: str = "") -> str:
    """Make a Draft Clone of an object (what a CAM job's Model holds).

    Args:
        object_name: Object to clone.
        name: Label for the clone.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import Draft
        doc = _doc({doc!r})
        App.setActiveDocument(doc.Name)
        o = _obj(doc, {name!r})
        c = Draft.make_clone(o)
        if {label!r}:
            c.Label = {label!r}
        doc.recompute()
        s = _shape_of(c)
        print("created", c.Name, "(" + c.TypeId + ") cloning", o.Name)
        if s is not None:
            print("  bbox:", _bbox_str(s), " volume: %.4f" % s.Volume)
    """).format(doc=document, name=object_name, label=name))


@_tool()
def draft_array(
    object_name: str,
    kind: str = "ortho",
    count_x: int = 2,
    count_y: int = 1,
    count_z: int = 1,
    spacing_x: float = 10.0,
    spacing_y: float = 10.0,
    spacing_z: float = 10.0,
    polar_count: int = 4,
    polar_angle: float = 360.0,
    center: str = "0,0,0",
    use_link: bool = True,
    name: str = "",
    document: str = "",
) -> str:
    """Make a Draft orthogonal or polar array of an object.

    Args:
        object_name: Object to array.
        kind: "ortho" or "polar".
        count_x: Ortho: instances along X.
        count_y: Ortho: instances along Y.
        count_z: Ortho: instances along Z.
        spacing_x: Ortho: spacing along X in mm.
        spacing_y: Ortho: spacing along Y in mm.
        spacing_z: Ortho: spacing along Z in mm.
        polar_count: Polar: number of instances.
        polar_angle: Polar: total angle in degrees.
        center: Polar: "x,y,z" centre of rotation.
        use_link: Make a link array (lighter) instead of a shape array.
        name: Label for the array.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import Draft
        doc = _doc({doc!r})
        App.setActiveDocument(doc.Name)
        o = _obj(doc, {name!r})
        if {kind!r} == "ortho":
            a = Draft.make_ortho_array(o, App.Vector({sx!r}, 0, 0), App.Vector(0, {sy!r}, 0), App.Vector(0, 0, {sz!r}),
                                       {nx!r}, {ny!r}, {nz!r}, use_link={link!r})
            expected = {nx!r} * {ny!r} * {nz!r}
        elif {kind!r} == "polar":
            c = App.Vector(*[float(x) for x in {center!r}.split(",")])
            a = Draft.make_polar_array(o, {pn!r}, {pa!r}, c, use_link={link!r})
            expected = {pn!r}
        else:
            raise _Fail("kind must be ortho or polar")
        if {label!r}:
            a.Label = {label!r}
        doc.recompute()
        s = _shape_of(a)
        print("created", a.Name, "(" + a.TypeId + ")", {kind!r}, "array of", o.Name, "x", expected)
        if s is not None:
            print("  solids:", len(s.Solids), " bbox:", _bbox_str(s))
    """).format(doc=document, name=object_name, kind=kind, nx=count_x, ny=count_y, nz=count_z,
                sx=spacing_x, sy=spacing_y, sz=spacing_z, pn=polar_count, pa=polar_angle,
                center=center, link=use_link, label=name))


@_tool()
def draft_wire(points: str, closed: bool = False, name: str = "", document: str = "") -> str:
    """Make a Draft Wire (polyline) from 3D points, e.g. a CAM boundary or spine.

    Args:
        points: JSON list of [x, y, z] (or [x, y]) points in mm.
        closed: Close the wire into a loop (a closed wire makes a face).
        name: Label for the wire.
        document: Document name. Empty string for the active document.
    """
    return _call(textwrap.dedent("""\
        import json, Draft
        doc = _doc({doc!r})
        App.setActiveDocument(doc.Name)
        pts = [App.Vector(*(list(p) + [0] * (3 - len(p)))) for p in json.loads({pts!r})]
        if len(pts) < 2:
            raise _Fail("need at least two points")
        w = Draft.make_wire(pts, closed={closed!r})
        w.Closed = {closed!r}
        if {label!r}:
            w.Label = {label!r}
        doc.recompute()
        s = w.Shape
        print("created", w.Name, "(" + w.TypeId + ") with", len(pts), "points; closed:", w.Closed)
        print("  edges:", len(s.Edges), " length: %.4f mm" % s.Length, " faces:", len(s.Faces))
        if s.Wires:
            print("  wire closed:", s.Wires[0].isClosed())
    """).format(doc=document, pts=points, closed=closed, label=name))


# ── Resources ──────────────────────────────────────────────────────
#
# Read-only views of FreeCAD state that a client can fetch without a tool
# call. Each one is the text of the matching inspection tool.


@mcp.resource("freecad://documents")
def resource_documents() -> str:
    """Open documents: name, label, file and object count."""
    return list_documents()


@mcp.resource("freecad://document/{name}/objects")
def resource_document_objects(name: str) -> str:
    """Objects of one document with type, visibility and bounding box."""
    return get_document_info(document=name)


@mcp.resource("freecad://types")
def resource_types() -> str:
    """Every object type FreeCAD can create, grouped by module."""
    return list_object_types()


@mcp.resource("freecad://selection")
def resource_selection() -> str:
    """The current GUI selection."""
    return get_selection()


# ── Prompts ────────────────────────────────────────────────────────


@mcp.prompt()
def build_parametric_part(part_name: str = "Part", requirements: str = "") -> str:
    """Build a spreadsheet-driven PartDesign part, verifying each step."""
    return textwrap.dedent("""\
        Build "{name}" in FreeCAD as a parametric PartDesign part.
        Requirements: {req}

        Work in this order, and read every tool result before the next call:
        1. Confirm orientation with the user first: which plane is the main
           face, which axis is up, where the origin sits.
        2. create_document, then spreadsheet_set one aliased cell per design
           input (lengths with units, e.g. "19.05 mm"). Every number the
           user might change goes here, not into the geometry.
        3. transaction("begin"), add_object("PartDesign::Body", ...).
        4. create_sketch on an origin plane of that Body; draw with
           sketch_add_rectangle / sketch_add_circle / sketch_add_polyline;
           add named dimensions with sketch_constrain; check sketch_info says
           "fully constrained: True" and "all wires closed: True".
        5. partdesign_feature (pad), then pockets, holes, patterns and
           dress-ups. Each result prints the Body volume before and after;
           a zero delta means the feature cut or added nothing -- fix it
           before continuing.
        6. set_expression to bind every Length, Radius and named sketch
           constraint to its spreadsheet alias, e.g.
           set_expression("Profile", "Constraints.width", "Params.width").
        7. Prove it is parametric: spreadsheet_set a different value,
           recompute, measure, then set it back.
        8. get_errors, then transaction("commit"), save_document, and
           capture_screenshot(view="iso", fit=True) for the user.
    """).format(name=part_name, req=requirements or "(ask the user)")


@mcp.prompt()
def verify_assembly(assembly: str = "") -> str:
    """Check an assembly for interference, gaps, grounding and visibility traps."""
    return textwrap.dedent("""\
        Verify the assembly {asm} in the active FreeCAD document.

        1. get_document_info(type_filter="App::Link") and assembly_bom to see
           what is instanced, and from which Bodies.
        2. assembly_solve: the solve code must be 0 and at least one part must
           be grounded; an assembly with no joints is parts parked by hand.
        3. check_interference("*"): every reported pair is a real overlap of
           solids. measure(a, b) on parts that should mate: distance must be
           0, intersection must be 0.
        4. set_visibility("*", True) then capture_screenshot(view="iso",
           fit=True). If the image is blank, the Body Tips are hidden: the
           tool prints why.
        5. get_errors must say "document is clean".
        Report findings as a list: what is wrong, which objects, and the
        measured numbers; do not change the model unless asked.
    """).format(asm=assembly or "(the active assembly)")


@mcp.prompt()
def set_up_cam_job(model: str = "", post_processor: str = "grbl", output_file: str = "") -> str:
    """Create a CAM job, tools, operations and posted G-code, with checks."""
    return textwrap.dedent("""\
        Set up a CAM job for {model} in the active FreeCAD document.

        1. measure(model) to know the stock size, and ask the user for the
           tool diameter, material and machine if not given.
        2. cam_create_job(models, stock="from_base", post_processor="{post}").
           The result must say "live in GUI: True" and "integrity: True".
        3. cam_add_tool(job, diameter, retune_default=True) for a one-tool
           job; add more tools only when the job needs them.
        4. cam_add_operation for each cut (profile outside, pockets, holes),
           giving explicit depths. Every operation prints its toolpath
           length; an EMPTY toolpath is a failure to fix, not a warning to
           ignore. Model corners square and add cam_add_dressup(op,
           "dogbone") for inside corners instead of modelling dogbones.
        5. cam_audit: zero problems.
        6. cam_post(job, "{out}"): check the units line (G21), the Z range
           against the stock, and the XY extents against the machine.
        7. Leave the job's PostProcessorArgs as you found them so the user's
           Post Process button still shows the editor.
    """).format(model=model or "(the model)", post=post_processor,
                out=output_file or "/path/to/output.nc")


# ── Entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
