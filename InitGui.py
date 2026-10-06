# -*- coding: utf-8 -*-
import os
import sys
import inspect
import FreeCAD
import FreeCADGui

try:
    # Derive our plugin directory from the code object's filename.
    # FreeCAD does not set __file__ when it exec()s InitGui.py, but the
    # code object always knows its source path.
    _plugin_dir = os.path.dirname(os.path.abspath(
        inspect.currentframe().f_code.co_filename
    ))
    # FreeCAD exec()s this file with separate globals/locals dicts.
    # Class bodies can only see globals, so we must store it there.
    globals()['_plugin_dir'] = _plugin_dir

    if _plugin_dir not in sys.path:
        sys.path.insert(0, _plugin_dir)

    FreeCAD.Console.PrintMessage(
        "TrailCurrent FreeCAD Tools: loading from {}\n".format(_plugin_dir)
    )

    class TrailCurrentFreeCADToolsWorkbench(FreeCADGui.Workbench):
        MenuText = "TrailCurrent FreeCAD Tools"
        ToolTip = ("TrailCurrent FreeCAD Tools: deboss the TrailCurrent logo, emboss QR codes, "
                   "place DTM connector housings, send bodies to Blender or Bambu Studio, "
                   "and drive FreeCAD from an AI agent over MCP")
        Icon = os.path.join(_plugin_dir, "resources", "icons", "TrailCurrentLogo.svg")

        def GetClassName(self):
            return "Gui::PythonWorkbench"

        def Initialize(self):
            FreeCAD.Console.PrintMessage("TrailCurrent FreeCAD Tools: Initialize()\n")
            import logo_command
            import logotext_command
            import qr_command
            import blender_command
            import bambu_command
            import dtm2p_command
            import dtm4p_command
            import dtm12p_command
            from mcp import rpc_command  # noqa: F401
            cmd_list = ["TrailCurrent_DebossLogo",
                        "TrailCurrent_DebossLogoText",
                        "TrailCurrent_QRCodeEmboss",
                        "TrailCurrent_DTM2P",
                        "TrailCurrent_DTM4P",
                        "TrailCurrent_DTM12P",
                        "TrailCurrent_SendToBlender",
                        "TrailCurrent_SendToBambuStudio",
                        "TrailCurrent_MCPServer"]
            self.appendToolbar("TrailCurrent FreeCAD Tools", cmd_list)
            self.appendMenu("TrailCurrent", cmd_list)

        def Activated(self):
            FreeCAD.Console.PrintMessage("TrailCurrent FreeCAD Tools: Activated\n")

        def Deactivated(self):
            pass

    FreeCADGui.addWorkbench(TrailCurrentFreeCADToolsWorkbench)
    FreeCAD.Console.PrintMessage("TrailCurrent FreeCAD Tools: workbench registered OK\n")
except Exception as e:
    FreeCAD.Console.PrintError("TrailCurrent FreeCAD Tools FAILED: {}\n".format(e))
