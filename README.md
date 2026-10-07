# TrailCurrent FreeCAD Tools

A FreeCAD workbench that collects the tools we use day to day at TrailCurrent —
branding printed parts, dropping in connector housings, handing a body straight
to a slicer or to Blender, and driving FreeCAD itself from an AI agent over MCP.

It started life as a logo debosser and has grown a good deal since.

| | |
|---|---|
| **Branding** | Deboss the TrailCurrent logo as a multi-level relief, with or without part text |
| **QR codes** | Emboss or deboss a scannable QR code onto any flat face |
| **Connectors** | Place Deutsch DTM 2, 4 and 12-position housings as real geometry |
| **Slicer handoff** | Send the selected body to Bambu Studio at print-grade tessellation |
| **Blender handoff** | Send the selected body to Blender for rendering or animation |
| **MCP server** | Expose FreeCAD to Claude Code and other MCP clients — 93 tools |

MIT licensed. FreeCAD 0.21 or newer; tested on 1.0 and 1.1.

---

## MCP — driving FreeCAD from an AI agent

`mcp/` contains an MCP server that connects to an XML-RPC server running inside
FreeCAD and exposes the application as tools an agent can call. It is how we
build parametric models, assemblies, CAM jobs and drawings conversationally
rather than by clicking.

Start the RPC server from inside FreeCAD — switch to this workbench and press
**MCP Server** — then point your client at the server:

```bash
claude mcp add freecad -- uv run --with mcp python3 \
  ~/.local/share/FreeCAD/v1-1/Mod/TrailCurrentFreeCADTools/mcp/freecad_mcp_server.py
```

`uv` pulls the `mcp` package on demand, so there is nothing to install first.

All 93 tools, grouped by what they touch:

```
documents   list_documents  create_document  open_document  save_document  close_document
            get_document_info  find_objects  dependencies  list_object_types  transaction  undo  redo
objects     add_object  remove_object  get_object_properties  set_object_property  get_property_info
            add_property  touch  set_placement  set_visibility  set_appearance  get_appearance
            get_selection  set_selection  clear_selection  recompute  get_errors
sketches    create_sketch  sketch_add_polyline  sketch_add_circle  sketch_add_rectangle  sketch_add_arc
            sketch_add_slot  sketch_add_polygon  sketch_constrain  sketch_delete_geometry
            sketch_list_geometry  sketch_info
partdesign  partdesign_feature  partdesign_revolve  partdesign_hole  partdesign_loft  partdesign_pipe
            partdesign_helix  partdesign_primitive  partdesign_pattern  partdesign_dressup  set_tip
            create_datum  create_shapebinder
assembly    create_assembly  add_link  assembly_ground  assembly_add_joint  assembly_solve  assembly_bom
parametric  spreadsheet_set  spreadsheet_get  set_expression  clear_expression
inspection  measure  measure_element  measure_distance  point_inside  check_interference
            capture_screenshot  set_view  set_camera  fit_view
mesh        shape_to_mesh  mesh_info  mesh_to_shape  mesh_fix
cam         cam_create_job  cam_add_tool  cam_list_tools  cam_add_operation  cam_add_dressup
            cam_post  cam_audit
drawing     drawing_create_page  drawing_add_view  drawing_add_section  drawing_add_dimension
            drawing_export
draft       draft_clone  draft_array  draft_wire
io          import_file  export_object  export_dxf
escape      execute_code
```

Every tool carries MCP annotations (read-only, destructive, idempotent), so a
client can tell `measure` from `remove_object` without reading the docstring.
The server also serves four resources (`freecad://documents`,
`freecad://document/{name}/objects`, `freecad://types`, `freecad://selection`)
and three prompts (`build_parametric_part`, `verify_assembly`,
`set_up_cam_job`) that sequence the tools in the intended order.

**Everything runs on FreeCAD's GUI thread.** A long call freezes the
application, and a modal dialog opened by a tool will deadlock every call after
it. Keep individual calls short. `execute_code` takes a `timeout` (60 s by
default); a call that runs past it is abandoned by the caller but keeps running
in FreeCAD.

Every result carries what FreeCAD itself said during the call. Recompute
failures and deprecation notices go to the Report view, not stdout, so they are
read back from there and returned as `[FreeCAD warnings]` and
`[FreeCAD errors]`. A `[dialogs auto-dismissed]` block means a modal prompt was
answered with its default button on your behalf.

To check the server against a running FreeCAD, run the live smoke test. It
exercises every tool against a scratch document and closes it on exit:

```bash
uv run --with mcp python3 mcp/tests/smoke_test.py            # test the tools
uv run --with mcp python3 mcp/tests/smoke_test.py --reload-rpc  # also re-read rpc_server.py in FreeCAD first
```

If you are driving this with Claude Code, the companion
[`freecad` skill](https://github.com/trailcurrentoss/TrailCurrentClaudeSkills)
encodes the workflow and the failure modes worth knowing about before you hit
them.

---

## Branding

### Logo deboss

Select a flat face, click the toolbar button, and the plugin cuts the logo into
the surface as four layers at different depths:

| Layer | Depth |
|---|---|
| Circle background | deepest — the floor |
| Mountain silhouette | mid |
| Trail path | shallow |
| Lightning bolt | shallowest |

The result is a staircase relief where the bolt reads as most raised. **Deboss
Logo + Text** does the same and adds a part name alongside it.

### QR codes

Generate a QR from any URL or text and emboss or deboss it onto a flat face:

- Raised or recessed
- 5–200 mm
- X/Y placement offsets
- Error correction L/M/Q/H and a configurable quiet zone

The live preview reports module count, QR version and module size, so you can
confirm the code is coarse enough to survive printing before you commit to it.
The `qrcode` package is bundled — nothing to pip install.

### Re-editing

Logo and QR objects keep their parameters. **Double-click** the object in the
tree, or select it and press the toolbar button, to reopen the panel with every
value pre-filled.

---

## Connector housings

**Place DTM2P / DTM4P / DTM12P Housing** drops a Deutsch DTM connector housing
into the document as real geometry, so an enclosure can be designed around the
actual part rather than a guess at its envelope.

---

## Handoffs

### Send to Bambu Studio

Tessellates the selected body at print-grade resolution, writes the mesh to
`~/TrailCurrentPrints`, and opens it in Bambu Studio. The command finds Bambu
Studio wherever it lives — a distribution package, `/opt`, a Flatpak, or an
AppImage under `~/Applications` — and reuses a running instance rather than
starting a second one.

### Send to Blender

Exports the selected body and opens it in Blender, for renders and assembly
animations. Pairs with the
[`blender` skill](https://github.com/trailcurrentoss/TrailCurrentClaudeSkills)
if you are scripting that side.

---

## Quick start

1. [Install the plugin](docs/installation.md)
2. Restart FreeCAD
3. Switch to the **TrailCurrent FreeCAD Tools** workbench in the workbench dropdown
4. Select a flat face on a solid body
5. Pick a tool from the toolbar and adjust the parameters in the side panel

## Documentation

- [Installation Guide](docs/installation.md) — Addon Manager or manual install
- [Usage Guide](docs/usage.md) — step-by-step walkthrough
- [Settings Reference](docs/settings.md) — every parameter explained
- [MCP Backlog](docs/backlog.md) — known defects and planned tools for the MCP server

## File structure

```
├── package.xml               # Addon Manager metadata
├── InitGui.py / Init.py      # Workbench registration
├── mcp/                      # MCP server + in-FreeCAD XML-RPC server
│   ├── freecad_mcp_server.py #   the MCP client-facing server
│   ├── rpc_server.py         #   runs inside FreeCAD
│   ├── rpc_command.py        #   toolbar command to start/stop it
│   └── tests/smoke_test.py   #   live test of every tool against a running FreeCAD
├── logo_*.py                 # Logo deboss: command, geometry, boolean ops
├── logotext_*.py             # Logo + text variant
├── qr_command.py / qr_emboss.py
├── dtm2p_*.py / dtm4p_*.py / dtm12p_*.py    # Deutsch DTM housings
├── bambu_command.py          # Send to Bambu Studio
├── blender_command.py        # Send to Blender
├── blender_studio_template.py
├── qrcode/                   # Bundled qrcode package
├── install.sh                # Manual install helper (Linux)
├── resources/icons/
└── docs/
```

## License

MIT. See [LICENSE](LICENSE).

## Links

- [Issue tracker](https://github.com/trailcurrentoss/TrailCurrentFreeCADTools/issues)
- [TrailCurrent Claude skills](https://github.com/trailcurrentoss/TrailCurrentClaudeSkills)
- [TrailCurrent](https://github.com/trailcurrentoss)
