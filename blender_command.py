"""FreeCAD command and task-panel UI for the Send to Blender tool."""

import os
import shutil
import subprocess

import FreeCAD
import FreeCADGui

from PySide import QtWidgets, QtCore

from launch_env import popen_kwargs

_plugin_dir = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_blender():
    """Locate the Blender executable on the system.

    Returns the path string or None if Blender cannot be found.
    """
    path = shutil.which("blender")
    if path:
        return path
    for candidate in ["/snap/bin/blender", "/usr/bin/blender",
                      "/usr/local/bin/blender",
                      "/opt/blender/blender"]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _get_exportable_object():
    """Return the selected object suitable for STL export, or None.

    Accepts any object with a non-null .Shape or a .Mesh attribute.
    If a feature inside a PartDesign::Body is selected, walks up to the
    Body so the full shape is exported.
    """
    sel = FreeCADGui.Selection.getSelectionEx()
    if len(sel) != 1:
        return None
    obj = sel[0].Object
    # Walk up to Body if we selected a feature inside one
    if hasattr(obj, "getParentGeoFeatureGroup"):
        parent = obj.getParentGeoFeatureGroup()
        if (parent is not None
                and hasattr(parent, "TypeId")
                and parent.TypeId == "PartDesign::Body"):
            obj = parent
    if hasattr(obj, "Shape") and obj.Shape and not obj.Shape.isNull():
        return obj
    if hasattr(obj, "Mesh"):
        return obj
    return None


def _export_stl(obj, filepath, mesh_tolerance=0.1):
    """Export a FreeCAD object to an STL file.

    Args:
        obj: FreeCAD object with .Shape or .Mesh
        filepath: Output .stl path
        mesh_tolerance: Linear deflection for tessellation (mm).
                        Lower = finer mesh, larger file.
    """
    if hasattr(obj, "Shape") and obj.Shape and not obj.Shape.isNull():
        import MeshPart
        mesh_data = MeshPart.meshFromShape(
            Shape=obj.Shape,
            LinearDeflection=mesh_tolerance,
            AngularDeflection=0.5,
        )
        mesh_data.write(filepath)
    elif hasattr(obj, "Mesh"):
        obj.Mesh.write(filepath)
    else:
        raise RuntimeError("Object has no exportable Shape or Mesh.")


def _shell_quote(s):
    """Quote a string for safe use in a shell command."""
    if s and all(c.isalnum() or c in "-_./=:" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def _default_export_dir():
    """Return a sensible default directory for the file dialog."""
    doc = FreeCAD.ActiveDocument
    if doc and doc.FileName:
        return os.path.dirname(doc.FileName)
    return os.path.expanduser("~")


# ---------------------------------------------------------------------------
# Task Panel
# ---------------------------------------------------------------------------

class SendToBlenderTaskPanel:
    """PySide task panel with export and scene configuration options."""

    def __init__(self, obj, blender_path):
        self.obj = obj
        self.blender_path = blender_path
        self.form = self._build_ui()

    def _build_ui(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(widget)
        layout.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.AllNonFixedFieldsGrow)

        # Header
        header = QtWidgets.QLabel("<b>Send to Blender</b>")
        header.setAlignment(QtCore.Qt.AlignCenter)
        layout.addRow(header)

        # Object info
        self.obj_label = QtWidgets.QLabel(self.obj.Label)
        layout.addRow("Object:", self.obj_label)

        sep1 = QtWidgets.QFrame()
        sep1.setFrameShape(QtWidgets.QFrame.HLine)
        layout.addRow(sep1)

        # --- Export Settings ---
        export_header = QtWidgets.QLabel("<i>Export Settings</i>")
        export_header.setAlignment(QtCore.Qt.AlignCenter)
        layout.addRow(export_header)

        self.tolerance_spin = QtWidgets.QDoubleSpinBox()
        self.tolerance_spin.setRange(0.01, 1.0)
        self.tolerance_spin.setValue(0.1)
        self.tolerance_spin.setSingleStep(0.05)
        self.tolerance_spin.setDecimals(2)
        self.tolerance_spin.setSuffix(" mm")
        self.tolerance_spin.setToolTip(
            "STL mesh tessellation tolerance.\n"
            "Lower values produce smoother meshes\n"
            "but larger file sizes.")
        layout.addRow("Mesh Tolerance:", self.tolerance_spin)

        sep2 = QtWidgets.QFrame()
        sep2.setFrameShape(QtWidgets.QFrame.HLine)
        layout.addRow(sep2)

        # --- Scene Settings ---
        scene_header = QtWidgets.QLabel("<i>Blender Scene</i>")
        scene_header.setAlignment(QtCore.Qt.AlignCenter)
        layout.addRow(scene_header)

        self.material_combo = QtWidgets.QComboBox()
        self.material_combo.addItems([
            "Match FreeCAD color",
            "White plastic",
            "Dark grey plastic",
            "Brushed aluminum",
            "Raw (no material)",
        ])
        self.material_combo.setToolTip(
            "Material applied to the model in Blender.")
        layout.addRow("Material:", self.material_combo)

        self.resolution_combo = QtWidgets.QComboBox()
        self.resolution_combo.addItems([
            "1920 x 1080 (Full HD)",
            "2560 x 1440 (QHD)",
            "3840 x 2160 (4K UHD)",
            "1080 x 1080 (Square)",
        ])
        self.resolution_combo.setToolTip("Render resolution preset.")
        layout.addRow("Resolution:", self.resolution_combo)

        self.samples_spin = QtWidgets.QSpinBox()
        self.samples_spin.setRange(32, 4096)
        self.samples_spin.setValue(256)
        self.samples_spin.setSingleStep(64)
        self.samples_spin.setToolTip(
            "Cycles render samples.\n"
            "Higher values reduce noise but increase render time.")
        layout.addRow("Render Samples:", self.samples_spin)

        self.focal_spin = QtWidgets.QDoubleSpinBox()
        self.focal_spin.setRange(24.0, 200.0)
        self.focal_spin.setValue(85.0)
        self.focal_spin.setSingleStep(5.0)
        self.focal_spin.setDecimals(0)
        self.focal_spin.setSuffix(" mm")
        self.focal_spin.setToolTip(
            "Camera focal length.\n"
            "85 mm is typical for product photography.\n"
            "50 mm for wider shots, 135 mm for tighter crops.")
        layout.addRow("Focal Length:", self.focal_spin)

        sep3 = QtWidgets.QFrame()
        sep3.setFrameShape(QtWidgets.QFrame.HLine)
        layout.addRow(sep3)

        info = QtWidgets.QLabel(
            '<span style="color: #666; font-size: 11px;">'
            'You will be asked where to save the project files.<br>'
            'A launcher script will be created to open the scene<br>'
            'in Blender. Press F12 in Blender to render.</span>')
        info.setAlignment(QtCore.Qt.AlignCenter)
        info.setWordWrap(True)
        layout.addRow(info)

        return widget

    # -- Task panel interface -----------------------------------------------

    def _build_blender_cmd(self, stl_path, script_path):
        """Build the full Blender command line as a list of tokens."""
        res_map = {
            0: (1920, 1080),
            1: (2560, 1440),
            2: (3840, 2160),
            3: (1080, 1080),
        }
        res_x, res_y = res_map.get(
            self.resolution_combo.currentIndex(), (1920, 1080))

        material_keys = [
            "match_freecad", "white_plastic", "dark_grey_plastic",
            "brushed_aluminum", "raw",
        ]
        material = material_keys[self.material_combo.currentIndex()]

        freecad_color = "0.8,0.8,0.8"
        if hasattr(self.obj, "ViewObject"):
            vo = self.obj.ViewObject
            if hasattr(vo, "ShapeColor"):
                c = vo.ShapeColor
                freecad_color = "{:.3f},{:.3f},{:.3f}".format(
                    c[0], c[1], c[2])

        focal = self.focal_spin.value()
        samples = self.samples_spin.value()

        return [
            self.blender_path,
            "--python", script_path,
            "--",
            "--stl", stl_path,
            "--material", material,
            "--freecad-color", freecad_color,
            "--resolution", str(res_x), str(res_y),
            "--samples", str(samples),
            "--focal-length", str(focal),
        ]

    def accept(self):
        """Ask for a save folder, export STL, write launcher, show command."""
        # Ask the user where to save the project files
        save_dir = QtWidgets.QFileDialog.getExistingDirectory(
            None,
            "Choose folder for Blender project files",
            _default_export_dir(),
        )
        if not save_dir:
            return False  # user cancelled the folder dialog

        stl_path = os.path.join(save_dir, self.obj.Label + ".stl")
        tolerance = self.tolerance_spin.value()

        try:
            _export_stl(self.obj, stl_path, tolerance)
        except Exception as e:
            FreeCAD.Console.PrintError(
                "STL export failed: {}\n".format(e))
            QtWidgets.QMessageBox.critical(
                None, "Export Error",
                "Failed to export STL:\n\n{}".format(e))
            return False

        # Copy the Blender studio script alongside the STL
        src_script = os.path.join(_plugin_dir, "blender_studio_template.py")
        dst_script = os.path.join(save_dir, "blender_studio_template.py")
        shutil.copy2(src_script, dst_script)

        # Build the full command
        cmd = self._build_blender_cmd(stl_path, dst_script)

        # Write a launcher shell script
        launcher_path = os.path.join(save_dir, "open_in_blender.sh")
        with open(launcher_path, "w") as f:
            f.write("#!/bin/bash\n")
            quoted = " ".join(_shell_quote(c) for c in cmd)
            f.write(quoted + "\n")
        os.chmod(launcher_path, 0o755)

        FreeCADGui.Control.closeDialog()

        # Try to launch Blender directly
        launched = False
        try:
            # Clean environment: see launch_env.py.
            subprocess.Popen(cmd, **popen_kwargs())
            launched = True
        except (PermissionError, OSError) as e:
            FreeCAD.Console.PrintWarning(
                "Direct Blender launch failed ({}), showing command.\n"
                .format(e))

        if launched:
            FreeCAD.Console.PrintMessage(
                "Sent '{}' to Blender (STL: {})\n".format(
                    self.obj.Label, stl_path))
        else:
            # Show fallback dialog with the launcher command
            shell_cmd = "bash " + _shell_quote(launcher_path)
            _show_launch_dialog(shell_cmd, save_dir)

        return True

    def reject(self):
        """Called when the user presses Cancel."""
        FreeCADGui.Control.closeDialog()
        return True

    def getStandardButtons(self):
        return (
            QtWidgets.QDialogButtonBox.Ok
            | QtWidgets.QDialogButtonBox.Cancel
        )


def _show_launch_dialog(shell_cmd, export_dir):
    """Show a dialog with a copyable command when auto-launch fails."""
    dlg = QtWidgets.QDialog(None)
    dlg.setWindowTitle("Send to Blender")
    dlg.setMinimumWidth(500)
    layout = QtWidgets.QVBoxLayout(dlg)

    layout.addWidget(QtWidgets.QLabel(
        "<b>Project files saved successfully.</b><br><br>"
        "FreeCAD cannot launch Blender directly (snap sandbox).<br>"
        "Run this command in a terminal to open the scene:"))

    cmd_edit = QtWidgets.QLineEdit(shell_cmd)
    cmd_edit.setReadOnly(True)
    cmd_edit.selectAll()
    layout.addWidget(cmd_edit)

    dir_label = QtWidgets.QLabel(
        '<span style="color: #666; font-size: 11px;">'
        'Files saved in: {}</span>'.format(export_dir))
    dir_label.setWordWrap(True)
    layout.addWidget(dir_label)

    btn_layout = QtWidgets.QHBoxLayout()

    copy_btn = QtWidgets.QPushButton("Copy to Clipboard")
    def _copy():
        QtWidgets.QApplication.clipboard().setText(shell_cmd)
        copy_btn.setText("Copied!")
    copy_btn.clicked.connect(_copy)
    btn_layout.addWidget(copy_btn)

    close_btn = QtWidgets.QPushButton("OK")
    close_btn.clicked.connect(dlg.accept)
    btn_layout.addWidget(close_btn)

    layout.addLayout(btn_layout)
    dlg.exec_()


# ---------------------------------------------------------------------------
# FreeCAD Command
# ---------------------------------------------------------------------------

class SendToBlenderCommand:
    """FreeCAD command that exports the selection and opens it in Blender."""

    def GetResources(self):
        return {
            "Pixmap": os.path.join(
                _plugin_dir, "resources", "icons", "SendToBlender.svg"),
            "MenuText": "Send to Blender",
            "ToolTip": (
                "Export the selected body as STL and open it in Blender\n"
                "with a product photography studio scene (Cycles renderer,\n"
                "3-point lighting, white cyclorama backdrop)."
            ),
        }

    def IsActive(self):
        """Active when a single exportable object is selected."""
        return _get_exportable_object() is not None

    def Activated(self):
        """Show the task panel with export and scene options."""
        obj = _get_exportable_object()
        if obj is None:
            return

        blender_path = _find_blender()
        if blender_path is None:
            QtWidgets.QMessageBox.warning(
                None, "Send to Blender",
                "Blender was not found on this system.\n\n"
                "Install Blender and ensure it is in your PATH,\n"
                "or install via:\n"
                "  sudo snap install blender --classic")
            return

        panel = SendToBlenderTaskPanel(obj, blender_path)
        FreeCADGui.Control.showDialog(panel)


# Register the command with FreeCAD
FreeCADGui.addCommand("TrailCurrent_SendToBlender", SendToBlenderCommand())
