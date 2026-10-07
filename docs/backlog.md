# MCP server backlog

Ordered work list for `mcp/`. Phases are in dependency order and each phase
can ship on its own. Every item names the file to change and the check that
proves it done. Checks are meant to be run against a live FreeCAD 1.1 instance
through the MCP itself, the same way the defects were found.

Bump `<version>` in `package.xml` on every release: minor for a phase, patch
for a fix. The Addon Manager ignores anything that is not newer.

---

## Phase 1 — Silent failures

Tools that report success while doing nothing, or that only work on the first
Body in a document.

- [x] **1. `export_object` writes nothing for mesh and glTF formats.**
  `mcp/freecad_mcp_server.py` (`export_object`). `Part.export` only handles
  STEP, IGES and BREP; `.stl` returns silently with no file and `.obj`, `.glb`,
  `.3mf` raise "Unknown extension". Route by extension: STEP/IGES/BREP through
  `ImportGui.export` with objects so colours survive; STL/OBJ/PLY/3MF through
  `Mesh.export` with a `deflection` parameter; GLB/GLTF through
  `ImportGui.export`. Resolve `App::Link` to a positioned shape the way
  `measure` does. Verify the file exists and is non-empty before reporting.
  *Check:* export a box to every extension and confirm each file lands.

- [x] **2. `import_file` cannot import STL, OBJ, SVG or DXF.**
  `Import.insert` fails with "no supported file format" for anything but
  STEP/IGES/BREP. Dispatch on extension to `Mesh.insert`, `importSVG.insert`,
  `importDXF.insert` and `Import.insert`. Report the names of objects added.
  *Check:* import the STL from item 1 and an SVG from `resources/icons`.

- [x] **3. `create_sketch` attaches a second Body's sketch to the first Body's
  origin.** The document-level `XY_Plane` lookup finds the first body's plane;
  the second body's is `XY_Plane001`. When a body is given, take the plane from
  that body's `Origin.OriginFeatures` first; fall back to the document-level
  name only when there is no body.
  *Check:* two bodies, sketch in the second, support is in the second body's
  origin.

- [x] **4. `partdesign_pattern` crashes on any Body after the first and leaves
  an orphan.** `bd.getObject("X_Axis")` is `None` for later bodies and the
  error is raised after `addObject`, leaving a dangling pattern. It also never
  moves `Tip`, so the pattern drops out of the result shape even on the first
  body. Resolve axes and planes from `bd.Origin.OriginFeatures` by role,
  validate before `addObject`, and set `bd.Tip` to the new pattern.
  *Check:* pattern a pad in the second body; no orphan, body volume grows.

- [x] **5. `partdesign_feature` sets the deprecated `Midplane` property.**
  FreeCAD 1.1 maps it to `SideType`, logs a deprecation warning on every
  recompute, and `Midplane=False` is what makes a pocket cut nothing. Set
  `SideType` instead and expose the full `Type` enum (`Length`, `ThroughAll`,
  `UpToFirst`, `UpToFace`, `TwoLengths`, `UpToShape`) plus `Length2`. Report
  `SideType` and `Type` back.
  *Check:* a pocket with `SideType="Symmetric"` removes volume from a pad on
  the same plane.

- [x] **6. `check_interference("*")` reports a Body interfering with its own
  features.** The wildcard includes every Pad and Pattern inside a Body. Skip
  objects whose `getParentGeoFeatureGroup()` is a Body, and skip Origin
  children.
  *Check:* one Body with a pad and a pattern reports zero interferences.

- [x] **7. `spreadsheet_get` with no cell lists properties, not aliases.** It
  prints `Label`, `Visibility`, `ExpressionEngine` and `cells`, and computes a
  `props` list it never uses. Walk the used cells and print cell, alias,
  content and resolved value.
  *Check:* a sheet with one alias prints one row.

- [x] **8. Guard `measure` and `add_object` against misuse.** `measure` raises
  a traceback on any object without a Shape (a Spreadsheet, for instance);
  return a clear message instead. `add_object` creates PartDesign features
  outside any Body even though its docstring advertises `PartDesign::Pad`;
  add an optional `body` argument, use `body.addObject` for PartDesign types,
  and refuse a PartDesign feature with no body.

---

## Phase 2 — Make failures visible

RPC-layer changes in `mcp/rpc_server.py` and the formatting side of
`mcp/freecad_mcp_server.py`.

- [x] **9. Every traceback carries a bogus chained `SyntaxError`.**
  `_process_queue` tries `eval` first and runs `exec` inside the `except`, so
  every real exception arrives under "During handling of the above exception"
  beneath a `SyntaxError` at line 1. Decide eval versus exec with `ast.parse`
  before running anything, and run `exec` outside the handler.
  *Check:* `raise ValueError("x")` through `execute_code` returns one
  traceback.

- [x] **10. FreeCAD Console output is not captured.** `PrintWarning` and
  `PrintError` go to the Report view, not `sys.stderr`, so recompute failures
  and deprecation warnings never reach the caller. Install a Console observer
  for the duration of each execution and return `console_warnings` and
  `console_errors` keys; render them in `_format`.
  *Check:* a pocket with `Midplane` set shows the deprecation warning in the
  tool result.

- [x] **11. Expose `timeout` on `execute_code`.** The RPC `execute(code,
  timeout=60)` already accepts one; the MCP tool does not pass it. Set a
  matching socket timeout on the XML-RPC proxy so the client does not outlive
  the server's own limit. Document the default and the GUI-thread cost.

- [x] **12. Add `undo` and `redo` tools** beside `transaction`, reporting
  `UndoNames` and `RedoNames` afterwards.

- [x] **13. Make `get_document_info` cheap by default.** It computes Volume and
  Area for every shape, which is seconds on the GUI thread for a large
  assembly. Add `include_shape_stats=False` (bounding box stays, Volume and
  Area become opt-in) plus `type_filter` and `label_contains`.

---

## Phase 3 — Cover the modelling surface

New tools in `mcp/freecad_mcp_server.py`. Each one follows the existing
pattern: add to the body, set `Tip`, report validity and bounding box.

- [x] **14. Sketch constraints.** `sketch_constrain(sketch, kind, geo_a, pos_a,
  geo_b, pos_b, value, name)` covering Coincident, PointOnObject, Horizontal,
  Vertical, Parallel, Perpendicular, Tangent, Equal, Symmetric, Distance,
  DistanceX, DistanceY, Radius, Diameter, Angle and Block. A named constraint
  is what `set_expression` needs via `Constraints.name`, so return the index
  and name. Today nothing creates a dimensional constraint, so no sketch built
  through the tools can be driven from a spreadsheet.
  *Check:* a rectangle bound to a spreadsheet alias resizes on recompute and
  `FullyConstrained` turns true.

- [x] **15. Sketch geometry.** `sketch_add_rectangle` (optionally centred, with
  constraints applied), `sketch_add_arc`, `sketch_add_slot`,
  `sketch_add_polygon`, `sketch_delete_geometry`, and `sketch_list_geometry`
  printing indices, types and endpoints so the constraint tool has something
  to reference.

- [x] **16. Datums.** `create_datum(body, kind="plane|line|point|cs",
  base_plane, offset, rotation)` that echoes the resulting world placement,
  since the offset sign on `XZ_Plane` and `YZ_Plane` is the whole trap.
  `create_sketch` gains a `support` argument accepting a datum name.

- [x] **17. PartDesign feature set.** Revolution and Groove (axis, angle), Hole
  (diameter, depth, countersink, thread), Loft and Pipe (profile list, spine),
  Helix, and the additive and subtractive primitives.

- [x] **18. Dress-ups.** `partdesign_dressup(body, kind="fillet|chamfer|draft|
  thickness", edges_or_faces, size)`. Sub-element names are fragile, so also
  accept selectors such as "all edges parallel to Z" or "edges of face N
  longer than X" and print which edges were chosen. Add `set_tip(body,
  feature)`.

- [x] **19. ShapeBinder.** `create_shapebinder(body, source, sub_elements,
  kind="SubShapeBinder")` so one body can reference another's geometry
  parametrically instead of duplicating coordinates.

- [x] **20. Appearance.** `set_appearance(object_names, color, transparency,
  display_mode)` and `get_appearance`; Materials where present. The Blender
  handoff depends on colours being set.

- [x] **21. Mesh.** `shape_to_mesh(object, deflection, angular_deflection,
  name)`, `mesh_info` (facets, manifold, self-intersections, volume),
  `mesh_to_shape`, `mesh_fix`.

- [x] **22. Measurement and verification.** `measure` gains sub-element support
  (face, edge, vertex), centre of mass and inertia (shipped as the separate
  `measure_element` tool so `measure` keeps its two-object signature). Add `point_inside(object,
  x, y, z)` for the `isInside` check, and `measure_distance(a, sub_a, b,
  sub_b)` using the 1.x `Measure` module so results also appear in the GUI.

- [x] **23. Document-level helpers.** `find_objects(type, label_contains,
  in_body)`, `dependencies(object)` returning InList and OutList,
  `list_object_types(module_filter)` from `supportedTypes` (over 350 types
  across 20 modules on 1.1), `get_property_info(object, property)` with type
  and enum values, `add_property`, and `touch`.

---

## Phase 4 — Assemblies, CAM, drawings, view

- [x] **24. Assembly joints.** `assembly_ground(assembly, link)`,
  `assembly_add_joint(assembly, kind, link_a, element_a, link_b, element_b,
  offset, rotation)` for Fixed, Revolute, Cylindrical, Slider, Ball, Distance,
  Parallel, Perpendicular and Angle, `assembly_solve`, and `assembly_bom`.
  Report joint count and whether anything is grounded. Today `create_assembly`
  makes only the empty container.

- [x] **25. Link arrays.** `add_link` gains `count` and `show_elements` so a
  link array replaces N separate links.

- [x] **26. CAM job creation.** `cam_create_job(models, stock_kind,
  stock_margins, post_processor)` built through the Gui-layer path so the job
  is live in the tree, not the inert App-layer `Create()`. Report
  `integrityCheck` and the tree contents. The README currently promises CAM
  and no CAM tool exists.

- [x] **27. CAM tools and controllers.** `cam_add_tool(job, bit_path_or_spec,
  spindle, feeds)` that never shares a ToolBit between jobs, and
  `cam_list_tools(job)`.

- [x] **28. CAM operations.** `cam_add_operation(job, kind="profile|pocket|
  drilling|adaptive|engrave|facing", base, tool_controller, depths, side,
  direction, step_down)` and `cam_add_dressup(op, kind="dogbone|tags|
  boundary")`, since modelled dogbones do not survive the toolpath offset.
  Each returns toolpath length and bbox so an empty operation is caught.

- [x] **29. CAM post and audit.** `cam_post(job, path)` that writes what
  `export()` returns and verifies the file. `cam_audit(document)` that walks
  the tree and reports orphan operations, shared tools, and operations whose
  base decayed to `?FaceN`.

- [x] **30. TechDraw.** `drawing_create_page(template)`,
  `drawing_add_view(page, source, direction, scale)`, `drawing_add_section`,
  `drawing_add_dimension`, `drawing_export(page, path)` to SVG, PDF or DXF
  via `TechDraw.writeDXFPage`.

- [x] **31. View control.** `set_view(direction="iso|front|top|right|...",
  fit=True)`, `set_camera(position, target, orthographic)`,
  `set_display_mode` (shipped as the `display_mode` argument of `set_appearance`). `capture_screenshot` gains `fit` and `view` arguments.
  Add `set_selection(object, sub_elements, clear=True)` and
  `clear_selection`.

- [x] **32. Draft helpers** for the CAM path: `draft_clone`, `draft_array`,
  and 2D wire creation, since `Job.Create` fills `job.Model` with Draft
  clones.

---

## Phase 5 — Advertise it

- [x] **33. Tool annotations.** Pass `annotations=ToolAnnotations(
  read_only_hint=True)` on every inspection tool, `destructive_hint=True` on
  `remove_object`, `close_document`, `transaction("abort")` and `undo`, and
  `idempotent_hint` where it applies. Keep the mcp 1.x import fallback
  working.

- [x] **34. Resources.** Register `freecad://documents`,
  `freecad://document/{name}/objects`, `freecad://types` and
  `freecad://selection` so clients can read state without a tool call.

- [x] **35. Prompts.** Register "build a parametric part", "verify an
  assembly" and "set up a CAM job" prompts that sequence the tools in the
  intended order.

- [x] **36. Rewrite the server `instructions` string.** Add the tool groups,
  the GUI-thread freeze rule, the `dialogs` key semantics, the timeout, the
  full list of pre-imported modules (including `importSVG`, which the RPC
  server already imports), and a one-line inventory of FreeCAD modules
  reachable only through `execute_code` (FEM, Surface, Points, OpenSCAD,
  Inspection, Robot).

- [x] **37. Fix the `execute_code` docstring examples.** They demonstrate a
  `Part::Cut` while `partdesign_feature` says never to use Part booleans.
  Show a PartDesign body instead.

- [x] **38. Update README and `package.xml`** with the new tool count and
  grouping. Remove the CAM claim until Phase 4 ships.

- [x] **39. Update the companion skill** to drop the "no sketch-constraint
  tool" and "set `SideType` afterwards" workarounds once items 5 and 14 land,
  and to point at the CAM tools once Phase 4 lands.

---

## Phase 6 — Discoverability of references

Found while using the Phase 4 tools: both take references the caller has no
way to look up first.

- [x] **42. `drawing_add_dimension` needs edge numbers the caller cannot see.**
  TechDraw numbers edges and vertices per view, after the hidden-line
  worker finishes, so a dimension placed right after `drawing_add_view`
  silently measures 0. Add `drawing_list_geometry(view)` printing every
  Edge and Vertex index with its projected coordinates, length and curve
  type; make `drawing_add_dimension` wait for the view geometry, accept 3D
  references ("Object:EdgeN", measured True) as well as 2D ones, and
  explain a zero value.
  *Check:* a DistanceX between two listed vertices reports the box width.

- [x] **43. `cam_add_operation` exposes only side, direction and depths.**
  Each operation kind has its own settings (StepOver, UseComp, OffsetExtra,
  Pattern, ZigZagAngle, HelixAngle, PeckDepth, RetractHeight, ...). Add a
  `properties` JSON argument applied with enum and quantity validation,
  `cam_set_operation(operation, properties)` to tune afterwards reporting
  the toolpath change, and `cam_operation_info(operation)` listing every
  tunable property with its value, enum choices and expression.
  *Check:* a profile created with `{"OffsetExtra": "1 mm"}` has a longer
  toolpath than one without.

---

## Cross-cutting

- [x] **40. Live smoke test.** Add `mcp/tests/` with a script that drives the
  running RPC server over XML-RPC, exercises every tool against a scratch
  document, and asserts on files existing, object counts and volumes. It must
  close its scratch document on exit. Run it before every version bump.

- [x] **41. Shared helpers.** Factor out `_doc(document)`, `_shape_of(obj)`
  and `_origin_feature(body, role)` so the three copies of the Link-shape
  code and the two origin lookups cannot drift again.
