#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live smoke test for the FreeCAD MCP server.

Drives the XML-RPC server running inside FreeCAD through the MCP tool
functions themselves, exercising every tool against a scratch document and
asserting on files written, object counts and volumes. Run it before every
version bump.

FreeCAD must be open with the MCP RPC server started (TrailCurrent FreeCAD
Tools workbench > MCP Server). The scratch document is closed on exit.

Usage:
  uv run --with mcp python3 mcp/tests/smoke_test.py [--reload-rpc] [--keep]

  --reload-rpc  Re-read mcp/rpc_server.py inside the running FreeCAD before
                testing, so edits to the in-FreeCAD side can be checked
                without restarting FreeCAD.
  --keep        Leave the scratch document open for inspection.
"""

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
MCP_DIR = os.path.dirname(HERE)
ADDON_DIR = os.path.dirname(MCP_DIR)

# The addon's mcp/ directory must not shadow the mcp SDK.
for _p in ("", HERE, MCP_DIR, ADDON_DIR):
    while _p in sys.path:
        sys.path.remove(_p)


def load_server():
    spec = importlib.util.spec_from_file_location(
        "freecad_mcp_server", os.path.join(MCP_DIR, "freecad_mcp_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Re-executes rpc_server.py inside the module FreeCAD already imported,
# keeping the live server, queue and timers, and re-pointing the timers and
# the registered RPC methods at the new function objects.
RELOAD_RPC = r'''
import sys
mod = None
for name, m in list(sys.modules.items()):
    f = (getattr(m, "__file__", "") or "").replace("\\", "/")
    if f.endswith("/mcp/rpc_server.py"):
        mod = m
        break
if mod is None:
    raise RuntimeError("rpc_server module not found in sys.modules")
keep = {k: mod.__dict__[k] for k in ("_server", "_server_thread", "_exec_queue",
        "_timer", "_watchdog", "_namespace", "_dialog_log") if k in mod.__dict__}
code = compile(open(mod.__file__).read(), mod.__file__, "exec")
timer, watchdog = keep.get("_timer"), keep.get("_watchdog")
old_pq, old_wd = mod._process_queue, mod._watchdog_tick
for t, old in ((timer, old_pq), (watchdog, old_wd)):
    if t is not None:
        try:
            t.timeout.disconnect()
        except Exception:
            t.timeout.disconnect(old)
try:
    exec(code, mod.__dict__)
finally:
    mod.__dict__.update(keep)
    if timer is not None:
        timer.timeout.connect(mod._process_queue)
    if watchdog is not None:
        watchdog.timeout.connect(mod._watchdog_tick)
mod._init_namespace()
srv = keep.get("_server")
if srv is not None:
    for fn in ("execute", "ping", "screenshot"):
        srv.register_function(getattr(mod, fn), fn)
if hasattr(mod, "_calibrate_report_levels") and not mod._level_by_colour:
    mod._calibrate_report_levels()
print("reloaded", mod.__file__)
'''


class Smoke:
    def __init__(self, t, out_dir):
        self.t = t
        self.out = out_dir
        self.results = []
        self.doc = None

    # -- harness -------------------------------------------------------

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok), detail))
        print(("PASS  " if ok else "FAIL  ") + name)
        if not ok and detail:
            print("      " + detail.strip().replace("\n", "\n      "))

    def step(self, name, fn):
        try:
            fn()
        except Exception:
            self.check(name, False, traceback.format_exc())

    def q(self, code):
        """Run code in FreeCAD; return its text output."""
        return self.t.execute_code(code)

    def qj(self, code):
        """Run code that prints one JSON value; return it parsed."""
        out = self.q(code)
        line = out.strip().splitlines()[0] if out.strip() else ""
        return json.loads(line)

    def path(self, name):
        return os.path.join(self.out, name)

    # -- tests ---------------------------------------------------------

    def run(self):
        t = self.t
        r = t.list_documents()
        if r.startswith("ERROR"):
            print(r)
            sys.exit(2)

        r = t.create_document("MCPSmoke")
        m = re.search(r"Created document: (\S+)", r)
        self.check("create_document", bool(m), r)
        if not m:
            sys.exit(2)
        self.doc = D = m.group(1)

        # --- objects and properties ---
        r = t.add_object("Part::Box", "Box", document=D)
        self.check("add_object Part::Box", "Created Box" in r, r)
        r = t.set_object_property("Box", "Length", "20", document=D)
        self.check("set_object_property", "Set Box.Length = 20" in r, r)
        r = t.set_object_property("Box", "NoSuchProp", "1", document=D)
        self.check("set_object_property unknown property is an ERROR line",
                   r.startswith("ERROR:") and "Traceback" not in r, r)
        r = t.get_object_properties("Box", document=D)
        self.check("get_object_properties", '"Length"' in r, r)
        r = t.set_placement("Box", x=100, document=D)
        self.check("set_placement reports bbox", "X 100.0000..120.0000" in r, r)

        # --- item 1: export every format ---
        for ext in ("step", "stp", "iges", "brep", "stl", "obj", "ply", "3mf",
                    "amf", "off", "wrl", "x3d", "glb", "gltf"):
            p = self.path("box." + ext)
            r = t.export_object("Box", p, document=D)
            ok = os.path.exists(p) and os.path.getsize(p) > 0 and "exported" in r
            self.check("export_object ." + ext, ok, r)
        r = t.export_object("Box", self.path("box.xyz"), document=D)
        self.check("export_object unknown extension is an ERROR line",
                   r.startswith("ERROR:") and "unsupported extension" in r, r)

        # --- item 2: import ---
        r = t.import_file(self.path("box.stl"), document=D)
        self.check("import_file .stl", "Mesh::Feature" in r and "added 1" in r, r)
        svg = os.path.join(ADDON_DIR, "resources", "icons", "MCPServer.svg")
        r = t.import_file(svg, document=D)
        self.check("import_file .svg", "added" in r and "added 0" not in r
                   and "ERROR" not in r, r)
        r = t.import_file(self.path("box.step"), document=D)
        self.check("import_file .step", "Part::Feature" in r and "added 1" in r, r)
        r = t.import_file(self.path("does_not_exist.step"), document=D)
        self.check("import_file missing file is an ERROR line", r.startswith("ERROR:"), r)
        # Keep the scratch document to the objects the later checks expect.
        self.q("d = App.getDocument(%r)\n"
               "for o in list(d.Objects):\n"
               "    if o.Name != 'Box':\n"
               "        d.removeObject(o.Name)\n"
               "d.recompute()" % D)

        # --- items 3, 5: two bodies, pad, symmetric pocket ---
        t.add_object("PartDesign::Body", "B1", document=D)
        t.add_object("PartDesign::Body", "B2", document=D)
        r = t.create_sketch("S1", "XY", body="B1", document=D)
        self.check("create_sketch in B1", "created sketch S1" in r, r)
        r = t.sketch_add_circle("S1", 0, 0, 5, document=D)
        self.check("sketch_add_circle", "geometry 0" in r, r)
        r = t.partdesign_feature("B1", "S1", "pad", 10, document=D)
        m = re.search(r"delta \+([\d.]+)", r)
        self.check("partdesign_feature pad volume", m and abs(float(m.group(1)) - 785.398) < 0.5, r)
        self.check("partdesign_feature reports Type and SideType",
                   "Type: Length" in r and "SideType: One side" in r, r)

        r = t.create_sketch("S2", "XY", body="B2", document=D)
        own = self.qj(textwrap_json(
            "d = App.getDocument(%r); sk = d.getObject('S2'); b2 = d.getObject('B2')\n"
            "sup = sk.AttachmentSupport[0][0]\n"
            "print(json.dumps({'own': sup in b2.Origin.OriginFeatures, 'name': sup.Name,"
            " 'parent': sk.getParentGeoFeatureGroup().Name}))" % D))
        self.check("create_sketch in B2 attaches to B2's own origin (item 3)",
                   own.get("own") and own.get("parent") == "B2", json.dumps(own))
        r = t.sketch_add_polyline("S2", "[[20,20],[30,20],[30,30],[20,30]]", document=D)
        self.check("sketch_add_polyline closed", "open vertices (should be 0 for a closed profile): 0" in r, r)
        r = t.sketch_info("S2", document=D)
        self.check("sketch_info", "all wires closed: True" in r, r)
        r = t.partdesign_feature("B2", "S2", "pad", 5, document=D)
        m = re.search(r"delta \+([\d.]+)", r)
        self.check("pad in second body", m and abs(float(m.group(1)) - 500) < 0.01, r)

        # --- item 4: pattern on the second body, Tip moves, no orphan ---
        r = t.partdesign_pattern("B2", "pad_S2", "linear", 3, 40, "X", document=D)
        m = re.search(r"delta \+([\d.]+)", r)
        self.check("partdesign_pattern on second body grows volume (item 4)",
                   m and abs(float(m.group(1)) - 1000) < 0.01 and "Tip: LinearPattern" in r, r)
        orphans = self.qj(textwrap_json(
            "d = App.getDocument(%r)\n"
            "print(json.dumps([o.Name for o in d.Objects if o.TypeId.startswith('PartDesign::')"
            " and o.TypeId != 'PartDesign::Body' and o.getParentGeoFeatureGroup() is None]))" % D))
        self.check("no orphan PartDesign features", orphans == [], json.dumps(orphans))
        r = t.partdesign_pattern("B2", "pad_S1", "linear", 3, 40, "X", document=D)
        self.check("partdesign_pattern refuses a feature from another body",
                   r.startswith("ERROR:") and "not a feature of" in r, r)
        orphans = self.qj(textwrap_json(
            "d = App.getDocument(%r)\n"
            "print(json.dumps([o.Name for o in d.Objects if o.TypeId.startswith('PartDesign::')"
            " and o.TypeId != 'PartDesign::Body' and o.getParentGeoFeatureGroup() is None]))" % D))
        self.check("refusal leaves no orphan", orphans == [], json.dumps(orphans))

        # --- item 5: symmetric pocket on the pad's own plane removes material ---
        t.create_sketch("S3", "XY", body="B1", document=D)
        t.sketch_add_circle("S3", 0, 0, 2, document=D)
        r = t.partdesign_feature("B1", "S3", "pocket", 4, side_type="Symmetric", document=D)
        m = re.search(r"delta -([\d.]+)", r)
        self.check("pocket SideType=Symmetric removes volume (item 5)",
                   m and abs(float(m.group(1)) - 25.133) < 0.1 and "SideType: Symmetric" in r, r)
        r = t.partdesign_feature("B1", "S3", "pocket", 4, type="Bogus", document=D)
        self.check("partdesign_feature rejects a bad type", r.startswith("ERROR:"), r)
        r = t.partdesign_feature("B1", "S3", "pocket", 4, type="ThroughAll", document=D)
        self.check("partdesign_feature accepts ThroughAll", "Type: ThroughAll" in r, r)
        t.remove_object("pocket_S3001", document=D)

        # --- item 6: wildcard interference skips body internals ---
        r = t.check_interference("*", document=D)
        self.check("check_interference('*') reports no self-interference (item 6)",
                   "0 real interference(s)" in r and "pad_S1" not in r, r)
        r = t.check_interference("B1,B2,Box", document=D)
        self.check("check_interference explicit", "0 real interference(s)" in r, r)

        # --- item 7: spreadsheet listing ---
        r = t.spreadsheet_set("Params", "B1", "3 mm", alias="thickness", document=D)
        self.check("spreadsheet_set", "resolves to: 3" in r, r)
        t.spreadsheet_set("Params", "A1", "thickness", document=D)
        r = t.spreadsheet_get("Params", document=D)
        rows = [ln for ln in r.splitlines() if "thickness" in ln and "B1" in ln]
        self.check("spreadsheet_get lists one aliased row (item 7)",
                   len(rows) == 1 and "1 aliased cell(s)" in r, r)
        r = t.spreadsheet_get("Params", "thickness", document=D)
        self.check("spreadsheet_get by alias", "thickness = 3" in r, r)
        r = t.set_expression("pad_S1", "Length", "Params.thickness", document=D)
        self.check("set_expression", "bound pad_S1.Length" in r and "ERROR" not in r, r)
        r = t.clear_expression("pad_S1", "Length", document=D)
        self.check("clear_expression", "cleared" in r, r)
        t.set_object_property("pad_S1", "Length", "10", document=D)

        # --- item 8: guards ---
        r = t.measure("Params", document=D)
        self.check("measure on a spreadsheet is an ERROR line (item 8)",
                   r.startswith("ERROR:") and "Traceback" not in r, r)
        r = t.add_object("PartDesign::Pad", "Orphan", document=D)
        self.check("add_object refuses a PartDesign feature without a body (item 8)",
                   r.startswith("ERROR:") and "body=" in r, r)
        r = t.add_object("Sketcher::SketchObject", "S4", body="B2", document=D)
        self.check("add_object into a body", "Created S4" in r and "in B2" in r, r)
        t.remove_object("S4", document=D)
        r = t.measure("B1", "B2", document=D)
        self.check("measure two objects", "distance to B2" in r and "intersection" in r, r)

        # --- item 9: one traceback ---
        r = t.execute_code('raise ValueError("x")')
        self.check("execute_code error is a single traceback (item 9)",
                   r.count("Traceback (most recent call last)") == 1
                   and "During handling" not in r and "ValueError: x" in r, r)
        r = t.execute_code("1 + 1")
        self.check("execute_code expression result", r.strip() == "2", r)
        r = t.execute_code("x = (")
        self.check("execute_code syntax error is a single traceback",
                   r.count("Traceback") == 1 and "SyntaxError" in r, r)

        # --- item 10: console capture ---
        r = t.execute_code("p = App.getDocument(%r).getObject('pad_S1')\n"
                           "p.Midplane = True\nApp.getDocument(%r).recompute()\n"
                           "p.SideType = 'One side'\nApp.getDocument(%r).recompute()" % (D, D, D))
        self.check("deprecation warning reaches the caller (item 10)",
                   "[FreeCAD warnings]" in r and "deprecated" in r, r)
        r = t.execute_code("FreeCAD.Console.PrintError('smoke error line\\n')")
        self.check("PrintError reaches the caller as an error", "[FreeCAD errors]" in r
                   and "smoke error line" in r, r)

        # --- item 11: timeout ---
        t0 = time.time()
        r = t.execute_code("import time\ntime.sleep(2)", timeout=1)
        self.check("execute_code honours timeout (item 11)",
                   ("timed out" in r or "no reply" in r) and time.time() - t0 < 5, r)
        time.sleep(1.5)  # let the sleeping call finish before the next one queues

        # --- item 12: undo / redo ---
        t.transaction("begin", "smoke cylinder", document=D)
        t.add_object("Part::Cylinder", "Cyl", document=D)
        t.transaction("commit", document=D)
        r = t.undo(document=D)
        has = self.qj("print(json.dumps(App.getDocument(%r).getObject('Cyl') is not None))" % D)
        self.check("undo removes the transaction's object (item 12)",
                   "undid: smoke cylinder" in r and has is False, r)
        r = t.redo(document=D)
        has = self.qj("print(json.dumps(App.getDocument(%r).getObject('Cyl') is not None))" % D)
        self.check("redo restores it", "redid: smoke cylinder" in r and has is True, r)
        t.remove_object("Cyl", document=D)

        # --- item 13: cheap document info ---
        r = t.get_document_info(document=D)
        self.check("get_document_info omits volume by default (item 13)",
                   '"volume"' not in r and '"bounds"' in r, r[:400])
        r = t.get_document_info(document=D, include_shape_stats=True, type_filter="PartDesign::Body")
        info = json.loads(r)
        self.check("get_document_info filter + stats",
                   info["listed"] == 2 and all("volume" in o["shape"] for o in info["objects"]), r[:400])
        r = t.get_document_info(document=D, label_contains="b1")
        self.check("get_document_info label filter", json.loads(r)["listed"] == 1, r[:400])

        # --- assembly, links, placement, visibility ---
        r = t.create_assembly("Asm", document=D)
        self.check("create_assembly", "created Asm" in r, r)
        r = t.add_link("Asm", "B1", document=D)
        self.check("add_link", "linked B1 into Asm as L_B1" in r, r)
        r = t.add_link("Asm", "B2", name="L2", count=3, document=D)
        self.check("add_link array (item 25)", "(array of 3)" in r, r)
        r = t.set_placement("L_B1", x=200, document=D)
        self.check("set_placement on a link", "X 195.0000..205.0000" in r, r)
        r = t.measure("L_B1", document=D)
        self.check("measure resolves a link", "origin (195.0000" in r, r)
        r = t.set_visibility("*", True, document=D)
        self.check("set_visibility", "visible now:" in r, r)
        r = t.recompute(document=D)
        self.check("recompute", "Recomputed" in r, r)
        r = t.get_errors(document=D)
        self.check("get_errors", "document is clean" in r, r)
        r = t.get_selection()
        self.check("get_selection", "selected" in r or "[" in r, r)
        r = t.fit_view()
        self.check("fit_view", "View fitted" in r, r)
        p = self.path("view.png")
        r = t.capture_screenshot(p, 640, 480)
        self.check("capture_screenshot", os.path.exists(p) and os.path.getsize(p) > 0, r)
        p = self.path("profile.dxf")
        r = t.export_dxf("S2", p, document=D)
        self.check("export_dxf", "4 LINE entities" in r and os.path.getsize(p) > 0, r)
        r = t.remove_object("Box", document=D)
        self.check("remove_object", "Removed: Box" in r, r)

        self.phase3()
        self.phase4()

        # --- save ---
        p = self.path("smoke.FCStd")
        r = t.save_document(p, document=D)
        self.check("save_document", os.path.exists(p) and "Saved as" in r, r)
        r = t.list_documents()
        self.check("list_documents", D in r, r)

    def phase3(self):
        t = self.t
        D = self.doc

        # --- items 14, 15: constraints and geometry ---
        r = t.sketch_list_geometry("S2", document=D)
        self.check("sketch_list_geometry", r.count("LineSegment") == 4 and "Coincident" in r, r)
        r = t.sketch_constrain("S2", "Horizontal", 0, document=D)
        self.check("sketch_constrain Horizontal", "added Horizontal" in r, r)
        r = t.sketch_constrain("S2", "Distance", 0, value=10, name="w", document=D)
        self.check("sketch_constrain named Distance (item 14)", "named w" in r and "Constraints.w" in r, r)
        r = t.sketch_constrain("S2", "Distance", 0, value=12, document=D)
        self.check("conflicting constraint is removed again", r.startswith("ERROR:") and "removed" in r, r)
        r = t.set_expression("S2", "Constraints.w", "Params.thickness", document=D)
        info = t.sketch_list_geometry("S2", document=D)
        self.check("sketch dimension driven by spreadsheet alias (item 14)",
                   "ERROR" not in r and "= 3 mm  name=w" in info, r + "\n" + info)
        t.clear_expression("S2", "Constraints.w", document=D)
        self.q("sk = App.getDocument(%r).getObject('S2'); sk.setDatum('w', App.Units.Quantity('10 mm'))\n"
               "App.getDocument(%r).recompute()" % (D, D))

        t.create_sketch("S5", "XY", body="B2", document=D)
        r = t.sketch_add_rectangle("S5", 50, 0, 8, 6, name_prefix="tab", document=D)
        self.check("sketch_add_rectangle fully constrains (item 15)",
                   "fully constrained: True" in r and "tab_width" in r, r)
        t.create_sketch("S6", "XY", body="B2", document=D)
        r = t.sketch_add_arc("S6", 0, 0, 5, 0, 90, document=D)
        self.check("sketch_add_arc", "is geometry 0" in r, r)
        r = t.sketch_add_slot("S6", 70, 0, 80, 0, 2, document=D)
        self.check("sketch_add_slot", "solve: 0" in r and "arcs 1, 3" in r, r)
        r = t.sketch_add_polygon("S6", 90, 0, 3, sides=6, document=D)
        self.check("sketch_add_polygon", "solve: 0" in r and "sides 5..10" in r, r)
        r = t.sketch_delete_geometry("S6", "[0]", document=D)
        self.check("sketch_delete_geometry", "geometry now: 11" in r, r)
        t.remove_object("S6", document=D)

        # --- item 16: datums ---
        r = t.create_datum("B1", "plane", "XY", offset=10, document=D)
        self.check("create_datum plane reports world placement (item 16)",
                   "world position: (0.0000, 0.0000, 10.0000)" in r, r)
        r = t.create_datum("B1", "plane", "XZ", offset=5, name="XZoff", document=D)
        self.check("create_datum XZ offset lands at -Y", "world position: (0.0000, -5.0000, 0.0000)" in r, r)
        r = t.create_datum("B1", "line", "X", document=D)
        self.check("create_datum line follows the X axis", "line direction: (1.0000, 0.0000, 0.0000)" in r, r)
        r = t.create_sketch("S7", body="B1", support="DatumPlane", document=D)
        self.check("create_sketch on a datum", "support: DatumPlane" in r
                   and "world placement: (0.0000, 0.0000, 10.0000)" in r, r)

        # --- item 17: feature set ---
        r = t.partdesign_primitive("B2", "box", '{"Length": 5, "Width": 5, "Height": 5}', x=60, document=D)
        self.check("partdesign_primitive box (item 17)", "delta +125.0000" in r, r)
        r = t.partdesign_primitive("B2", "box", '{"Lenght": 5}', document=D)
        self.check("partdesign_primitive rejects unknown property", r.startswith("ERROR:") and "Length" in r, r)
        t.create_sketch("HS", "XY", body="B1", offset=10, document=D)
        t.sketch_add_circle("HS", 3, 0, 1, document=D)
        r = t.partdesign_hole("B1", "HS", 1.0, depth=3, document=D)
        m = re.search(r"delta -([\d.]+)", r)
        # pi * 0.5^2 * 3 for the bore plus the 118-degree drill-point cone
        self.check("partdesign_hole removes a cylinder", m and abs(float(m.group(1)) - 2.435) < 0.05, r)
        t.add_object("PartDesign::Body", "B3", document=D)
        t.create_sketch("S8", "XZ", body="B3", document=D)
        t.sketch_add_rectangle("S8", 2, 0, 2, 5, document=D)
        r = t.partdesign_revolve("B3", "S8", "revolution", "V_Axis", 360, document=D)
        m = re.search(r"delta \+([\d.]+)", r)
        self.check("partdesign_revolve ring volume", m and abs(float(m.group(1)) - 188.496) < 0.5, r)
        t.create_sketch("LF1", "XY", body="B3", offset=20, document=D)
        t.sketch_add_circle("LF1", 0, 0, 3, document=D)
        t.create_sketch("LF2", "XY", body="B3", offset=30, document=D)
        t.sketch_add_circle("LF2", 0, 0, 1, document=D)
        r = t.partdesign_loft("B3", "LF1,LF2", document=D)
        self.check("partdesign_loft", "delta +" in r and "valid: True" in r, r)
        t.create_sketch("SP", "XZ", body="B3", document=D)
        t.sketch_add_polyline("SP", "[[10,0],[10,-15]]", closed=False, document=D)
        t.create_sketch("PP", "XY", body="B3", document=D)
        t.sketch_add_circle("PP", 10, 0, 1, document=D)
        r = t.partdesign_pipe("B3", "PP", "SP", document=D)
        m = re.search(r"delta \+([\d.]+)", r)
        self.check("partdesign_pipe sweeps a tube", m and abs(float(m.group(1)) - 47.124) < 0.5, r)
        t.create_sketch("HX", "XZ", body="B3", document=D)
        t.sketch_add_circle("HX", 6, 0, 0.5, document=D)
        r = t.partdesign_helix("B3", "HX", pitch=2, height=6, document=D)
        self.check("partdesign_helix", "delta +" in r and "valid: True" in r, r)

        # --- item 18: dress-ups ---
        r = t.partdesign_dressup("B2", "fillet", 0.5, "parallel:Z & above:-1", document=D)
        self.check("partdesign_dressup fillet by selector (item 18)",
                   "delta -" in r and "valid: True" in r and "Edge" in r, r)
        r = t.partdesign_dressup("B2", "chamfer", 0.3, "Face1", document=D)
        self.check("partdesign_dressup chamfer edges of a face", "valid: True" in r and "edge(s)" in r, r)
        r = t.partdesign_dressup("B2", "fillet", 0.5, "longer:1000", document=D)
        self.check("partdesign_dressup empty selector is an ERROR line", r.startswith("ERROR:"), r)
        r = t.set_tip("B2", "pad_S2", document=D)
        self.check("set_tip", "Tip of B2 is now pad_S2" in r, r)
        t.set_tip("B2", "Chamfer", document=D)

        # --- item 19: binder ---
        r = t.create_shapebinder("B3", "B1", "Face1", document=D)
        self.check("create_shapebinder (item 19)", "faces 1" in r and "valid: True" in r, r)

        # --- item 20: appearance ---
        r = t.set_appearance("B1", color="#ff0000", transparency=20, document=D)
        self.check("set_appearance", "painted 1" in r, r)
        r = t.get_appearance("B1", document=D)
        self.check("get_appearance round-trips colour (item 20)", "#ff0000" in r and "transparency 20" in r, r)

        # --- item 21: mesh ---
        r = t.shape_to_mesh("B1", deflection=0.05, document=D)
        self.check("shape_to_mesh (item 21)", "created B1_Mesh" in r and "solid True" in r, r)
        r = t.mesh_info("B1_Mesh", document=D)
        self.check("mesh_info", "solid (closed) : True" in r, r)
        r = t.mesh_fix("B1_Mesh", document=D)
        self.check("mesh_fix", "ran:" in r and "harmonizeNormals" in r, r)
        r = t.mesh_to_shape("B1_Mesh", document=D)
        self.check("mesh_to_shape", "as a solid" in r and "valid: True" in r, r)
        t.remove_object("B1_Mesh_Shape", document=D)
        t.remove_object("B1_Mesh", document=D)

        # --- item 22: measurement ---
        r = t.measure_element("B1", "Face1", document=D)
        self.check("measure_element face (item 22)", "area:" in r and "surface: Cylinder" in r, r)
        r = t.measure_element("B1", document=D)
        self.check("measure_element solid inertia", "centre of mass" in r and "inertia diagonal" in r, r)
        r = t.point_inside("B1", 0, 0, 5, document=D)
        self.check("point_inside true", "is INSIDE" in r, r)
        r = t.point_inside("B1", 0, 0, 1, document=D)
        self.check("point_inside false (inside the pocket)", "is OUTSIDE" in r, r)
        r = t.measure_distance("B1:Vertex1", "B2:Vertex1", document=D)
        self.check("measure_distance", "distance B1:Vertex1 -> B2:Vertex1:" in r and "measurement object" in r, r)

        # --- item 23: document helpers ---
        r = t.find_objects(type_contains="PartDesign::Body", document=D)
        self.check("find_objects (item 23)", "3 object(s)" in r, r)
        r = t.find_objects(in_body="B3", type_contains="Sketch", document=D)
        self.check("find_objects in_body", "6 object(s)" in r, r)
        r = t.dependencies("pad_S1", document=D)
        self.check("dependencies", "'S1'" in r and "used by" in r, r)
        r = t.list_object_types("PartDesign", document=D)
        self.check("list_object_types", "PartDesign (" in r and "Pad" in r, r[:300])
        r = t.get_property_info("pad_S1", "Type", document=D)
        self.check("get_property_info", "enum  :" in r and "UpToFirst" in r, r)
        r = t.add_property("Params", "App::PropertyString", "Note", document=D)
        self.check("add_property", "added Params.Note" in r, r)
        r = t.touch("pad_S1", document=D)
        self.check("touch", "touched pad_S1" in r and "valid: True" in r, r)

    def phase4(self):
        t = self.t
        D = self.doc

        t.add_object("PartDesign::Body", "B4", document=D)
        t.partdesign_primitive("B4", "box", '{"Length": 10, "Width": 10, "Height": 10}', document=D)

        # --- view control (item 31), before TechDraw steals the active view ---
        r = t.set_view("front")
        self.check("set_view (item 31)", "view: front" in r and "looking along" in r, r)
        r = t.set_camera("100,100,100", "0,0,0", orthographic=False)
        m = re.search(r"looking along \((-?[\d.]+), (-?[\d.]+), (-?[\d.]+)\)", r)
        d = [float(v) for v in m.groups()] if m else [0, 0, 0]
        self.check("set_camera", "camera: Perspective" in r
                   and all(abs(c + 0.577) < 0.05 for c in d), r)
        t.set_camera(orthographic=True, fit=True)
        r = t.set_selection("B4", "Face6", document=D)
        self.check("set_selection", "('B4', ['Face6'])" in r, r)
        r = t.clear_selection()
        self.check("clear_selection", "selection cleared" in r, r)
        p = self.path("iso.png")
        r = t.capture_screenshot(p, 640, 480, view="iso", fit=True)
        self.check("capture_screenshot with view and fit", os.path.exists(p) and os.path.getsize(p) > 0, r)

        # --- item 24: assembly joints ---
        r = t.create_assembly("Asm2", document=D)
        self.check("create_assembly is an Assembly::AssemblyObject", "Assembly::AssemblyObject" in r, r)
        t.add_link("Asm2", "B1", name="LA", document=D)
        t.add_link("Asm2", "B4", name="LB", document=D)
        t.set_placement("LB", x=100, document=D)
        r = t.assembly_ground("Asm2", "LA", document=D)
        self.check("assembly_ground (item 24)", "grounded LA" in r and "grounded: ['LA']" in r, r)
        r = t.assembly_add_joint("Asm2", "Fixed", "LA:Face3", "LB:Face5", name="seat", document=D)
        self.check("assembly_add_joint moves the free part onto the grounded one",
                   "solve: 0" in r and "LB" in r and "MOVED" in r and "Z 10.0000..20.0000" in r, r)
        r = t.assembly_add_joint("Asm2", "Fixed", "LA:Face3", "LA:Face2", document=D)
        self.check("assembly_add_joint refuses one component", r.startswith("ERROR:"), r)
        r = t.assembly_solve("Asm2", document=D)
        self.check("assembly_solve", "solve: 0" in r and "joint seat" in r, r)
        r = t.assembly_bom("Asm2", document=D)
        self.check("assembly_bom", "2 instance(s) of 2 unique part(s)" in r, r)
        r = t.assembly_bom("Asm", document=D)
        self.check("assembly_bom counts link arrays (item 25)", "4 instance(s)" in r, r)

        # --- item 44: parts from another document, nested sub-assemblies ---
        r = t.create_document("MCPParts")
        self.parts_doc = P = re.search(r"Created document: (\S+)", r).group(1)
        t.add_object("PartDesign::Body", "Pin", document=P)
        t.partdesign_primitive("Pin", "cylinder", '{"Radius": 2, "Height": 20}', document=P)
        r = t.add_link("Asm2", "Pin", name="L_Pin", source_document=P, document=D)
        self.check("add_link refuses an external link before saving",
                   r.startswith("ERROR:") and "saved" in r, r)
        t.save_document(self.path("parts.FCStd"), document=P)
        t.save_document(self.path("smoke.FCStd"), document=D)
        r = t.add_link("Asm2", "Pin", name="L_Pin", source_document=P, document=D)
        self.check("add_link from another document (item 44)", "external, from" in r and "parts.FCStd" in r, r)
        r = t.assembly_add_joint("Asm2", "Fixed", "LB:Face6", "L_Pin:Face2", name="pin_seat", document=D)
        self.check("joint onto an external part", "solve: 0" in r and "L_Pin" in r and "MOVED" in r, r)
        r = t.create_assembly("Sub", parent="Asm2", document=D)
        self.check("create_assembly nested", "inside Asm2" in r, r)
        t.add_link("Sub", "B1", name="L_Cap", document=D)
        r = t.assembly_add_joint("Asm2", "Fixed", "L_Pin:Face3", "Sub.L_Cap:Face2", name="cap", document=D)
        self.check("joint reaching into a nested assembly", "solve: 0" in r and "Sub " in r and "MOVED" in r, r)
        r = t.assembly_bom("Asm2", document=D)
        self.check("assembly_bom names the external document",
                   P in r and "4 instance(s) of 3 unique part(s)" in r, r)

        # --- items 45-48: found on real assemblies ---
        r = t.check_interference("*", document=D)
        self.check("check_interference('*') skips masters of links (item 45)",
                   "'B1'" not in r and "'B4'" not in r and "elapsed:" in r, r)
        r = t.get_appearance("*", document=D)
        self.check("get_appearance('*') skips origin features (item 46)",
                   "X_Axis" not in r and "XY_Plane" not in r and "B1 " in r, r)
        r = t.get_document_info(document=D, limit=3)
        info = json.loads(r)
        self.check("get_document_info limit and by_type (item 47)",
                   info["listed"] == 3 and info["truncated"] is True
                   and info["by_type"].get("PartDesign::Body", 0) >= 4
                   and info["matched"] == info["object_count"], r[:400])
        t.create_assembly("Board", document=D)
        t.add_link("Board", "B1", name="Bd_a", document=D)
        t.add_link("Board", "B2", name="Bd_b", document=D)
        t.add_link("Asm2", "Board", name="L_Board", document=D)
        r = t.assembly_bom("Asm2", document=D)
        self.check("assembly_bom counts a linked container as one part (item 48)",
                   "(container, 2 objects)" in r and "5 instance(s) of 4 unique part(s)" in r, r)
        r = t.assembly_bom("Asm2", depth=2, document=D)
        self.check("assembly_bom depth=2 descends", "container" not in r and "6 instance(s)" in r, r)

        # --- items 26-29: CAM ---
        r = t.cam_create_job("B4", stock="from_base", stock_margins="1,1,1", post_processor="grbl",
                             name="JobB4", document=D)
        self.check("cam_create_job is live in the GUI (item 26)",
                   "live in GUI: True" in r and "integrity: True" in r, r)
        m = re.search(r"job (\S+) \(label JobB4\)", r)
        job = m.group(1) if m else "Job"
        r = t.cam_add_tool(job, 3.175, retune_default=True, document=D)
        self.check("cam_add_tool retunes the default (item 27)", "retuned default" in r and "3.175 mm" in r, r)
        r = t.cam_add_tool(job, 6.0, shape="ballend", label="6mm ball", document=D)
        self.check("cam_add_tool adds a second tool", "added" in r and "T2" in r, r)
        r = t.cam_list_tools(job, document=D)
        self.check("cam_list_tools", "2 tool controller(s)" in r, r)
        r = t.cam_add_operation(job, "profile", side="Outside", step_down=5, name="ProfileB4", document=D)
        self.check("cam_add_operation profile has a toolpath (item 28)",
                   "created ProfileB4" in r and "EMPTY" not in r and "path:" in r, r)
        r = t.cam_add_operation(job, "facing", base="B4:Face6", name="FaceB4", document=D)
        self.check("cam_add_operation facing on a face", "created FaceB4" in r and "EMPTY" not in r, r)
        r = t.cam_add_operation(job, "profile", base="B4:Face99", document=D)
        self.check("cam_add_operation rejects a missing face", r.startswith("ERROR:"), r)
        base_len = float(re.search(r"path: ([\d.]+) mm", t.cam_operation_info("ProfileB4", document=D)).group(1))
        r = t.cam_add_operation(job, "profile", side="Outside", step_down=5,
                                properties='{"OffsetExtra": "1 mm"}', name="ProfileOffset", document=D)
        m = re.search(r"path: ([\d.]+) mm", r)
        self.check("cam_add_operation properties lengthen the path (item 43)",
                   m and float(m.group(1)) > base_len and "applied: [('OffsetExtra'" in r, r)
        r = t.cam_add_operation(job, "profile", properties='{"Stepovr": 1}', document=D)
        self.check("cam_add_operation rejects an unknown property", r.startswith("ERROR:") and "tunable" in r, r)
        r = t.cam_operation_info("ProfileOffset", document=D)
        self.check("cam_operation_info", "[Profile]" in r and "OffsetExtra" in r and "choices" in r, r)
        r = t.cam_set_operation("ProfileOffset", '{"Side": "Inside", "UseComp": false}', document=D)
        self.check("cam_set_operation", "path length:" in r and "('Side', 'Inside')" in r, r)
        r = t.cam_set_operation("ProfileOffset", '{"Direction": "Sideways"}', document=D)
        self.check("cam_set_operation rejects a bad enum", r.startswith("ERROR:") and "'CW'" in r, r)
        r = t.cam_set_operation("ProfileOffset", '{"Side": "Inside"}', document=D)
        self.check("cam_set_operation explains a hidden property",
                   r.startswith("ERROR:") and "hidden" in r and "UseComp" in r, r)
        t.remove_object("ProfileOffset", document=D)
        r = t.cam_add_dressup("ProfileB4", "dogbone", document=D)
        self.check("cam_add_dressup dogbone", "created DressupDogbone" in r and "EMPTY" not in r, r)
        r = t.cam_add_dressup("DressupDogbone", "ramp", name="B4_Ramp", properties='{"Angle": 85}', document=D)
        self.check("cam_add_dressup ramp (no Create() upstream)",
                   "created RampEntryDressup" in r and "EMPTY" not in r and "('Angle', '85" in r, r)
        ops_line = next((ln for ln in r.splitlines() if "job operations now" in ln), "")
        self.check("cam_add_dressup ramp drops the wrapped op from the job (posts once)",
                   "RampEntryDressup" in ops_line and "'DressupDogbone'" not in ops_line, r)
        r = t.cam_add_dressup("RampEntryDressup", "ramp", properties='{"Method": "Zigzag"}', document=D)
        self.check("cam_add_dressup rejects a bad enum", r.startswith("ERROR:") and "Helix" in r, r)
        r = t.cam_add_dressup("ProfileB4", "dogbone", name="B4_Bones", document=D)
        self.check("cam_add_dressup custom name is the Label; Name keeps 'Dressup' so CAM finds the tool",
                   "created DressupDogbone" in r and "label B4_Bones" in r and "tool: " in r, r)
        r = t.cam_add_dressup("RampEntryDressup", "dogbone", name="Ramp_Bones", document=D)
        self.check("cam_add_dressup resolves the tool through a stack of dress-ups", "tool: " in r, r)
        r = t.remove_object(r.split("created ")[1].split()[0], document=D)
        self.check("remove_object on a dress-up puts its operation back in the job",
                   "RampEntryDressup is back in" in r, r)
        p = self.path("b4.nc")
        r = t.cam_post(job, p, document=D)
        self.check("cam_post writes verified G-code (item 29)",
                   os.path.exists(p) and "units: mm (G21)" in r and "Z range" in r, r)
        r = t.cam_audit(document=D)
        self.check("cam_audit clean", "1 job(s), 0 problem(s)" in r, r)

        # --- item 30: TechDraw ---
        r = t.drawing_create_page("A4_Landscape", document=D)
        self.check("drawing_create_page (item 30)", "created page Page" in r and "297 x 210" in r, r)
        r = t.drawing_add_view("Page", "B4", "top", 1.0, 80, 120, name="TopView", document=D)
        self.check("drawing_add_view", "added view TopView" in r and "valid: True" in r, r)
        r = t.drawing_add_section("Page", "TopView", "Down", "5,5,5", x=200, y=120, document=D)
        self.check("drawing_add_section", "added section Section" in r and "valid: True" in r, r)
        r = t.drawing_list_geometry("TopView", document=D)
        self.check("drawing_list_geometry (item 42)", "4 visible edge(s)" in r and "Vertex3" in r, r)
        verts = {int(m.group(1)): (float(m.group(2)), float(m.group(3)))
                 for m in re.finditer(r"Vertex(\d+)\s+\((-?[\d.]+), (-?[\d.]+)\)", r)}
        top = sorted((i for i, (vx, vy) in verts.items() if vy > 0), key=lambda i: verts[i][0])
        refs = "Vertex%d,Vertex%d" % (top[0], top[-1]) if len(top) >= 2 else "Vertex0,Vertex2"
        r = t.drawing_add_dimension("Page", "TopView", "DistanceX", refs, document=D)
        m = re.search(r"value: ([\d.]+)", r)
        self.check("drawing_add_dimension 2D vertices from the listing",
                   m and abs(float(m.group(1)) - 10.0) < 0.01 and "measured Projected" in r, r)
        r = t.drawing_add_dimension("Page", "TopView", "Distance", "B4:Edge1", document=D)
        m = re.search(r"value: ([\d.]+)", r)
        self.check("drawing_add_dimension 3D reference",
                   m and abs(float(m.group(1)) - 10.0) < 0.01 and "measured True" in r, r)
        r = t.drawing_add_dimension("Page", "TopView", "DistanceX", "Edge40", document=D)
        self.check("drawing_add_dimension rejects an unknown 2D edge",
                   r.startswith("ERROR:") and "drawing_list_geometry" in r, r)
        for ext in ("svg", "pdf", "dxf"):
            p = self.path("page." + ext)
            r = t.drawing_export("Page", p, document=D)
            self.check("drawing_export ." + ext, os.path.exists(p) and os.path.getsize(p) > 0 and "exported" in r, r)

        # --- item 32: Draft ---
        r = t.draft_clone("B4", document=D)
        self.check("draft_clone (item 32)", "created Clone" in r and "volume: 1000" in r, r)
        r = t.draft_array("B4", "ortho", count_x=2, count_y=2, spacing_x=20, spacing_y=20, document=D)
        self.check("draft_array ortho", "solids: 4" in r, r)
        r = t.draft_wire("[[0,0,0],[10,0,0],[10,10,0]]", closed=True, document=D)
        self.check("draft_wire", "edges: 3" in r and "wire closed: True" in r, r)

    def close(self):
        for name in (self.doc, getattr(self, "parts_doc", None)):
            if name:
                print(self.t.close_document(name))


def textwrap_json(code):
    return "import json\n" + code


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--reload-rpc", action="store_true",
                    help="re-read rpc_server.py inside the running FreeCAD first")
    ap.add_argument("--keep", action="store_true",
                    help="leave the scratch document open")
    ap.add_argument("--out", default=None,
                    help="directory for exported files (default: a temp dir)")
    args = ap.parse_args()

    t = load_server()
    if args.reload_rpc:
        print(t.execute_code(RELOAD_RPC))

    out = args.out or tempfile.mkdtemp(prefix="freecad_mcp_smoke_")
    os.makedirs(out, exist_ok=True)
    print("output directory:", out)

    s = Smoke(t, out)
    try:
        s.run()
    finally:
        if not args.keep:
            s.close()

    failed = [r for r in s.results if not r[1]]
    print("\n{} checks, {} failed".format(len(s.results), len(failed)))
    for name, _, detail in failed:
        print("  FAIL", name)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
