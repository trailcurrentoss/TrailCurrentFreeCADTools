"""FreeCAD command and task-panel UI for the Send to Bambu Studio tool.

Tessellates the selected Body/Part with configurable deflection settings,
writes a high-fidelity 3MF (or STL fallback), and launches Bambu Studio
with the file as a positional argument. Handles native, AppImage, and
Flatpak Bambu Studio installations across Linux, macOS, and Windows.
"""

import os
import platform
import shutil
import subprocess

import FreeCAD
import FreeCADGui

from PySide import QtWidgets, QtCore

from launch_env import popen_kwargs

_plugin_dir = os.path.dirname(os.path.abspath(__file__))

# Flatpak sandbox cannot read /tmp — stage under $HOME.
_STAGE_DIR = os.path.expanduser("~/TrailCurrentPrints")

# One-shot flag file so we only nag the user about Single Instance mode once.
_HINT_FLAG = os.path.expanduser(
    "~/.config/TrailCurrent/bambu_single_instance_hint_shown")


# ---------------------------------------------------------------------------
# Bambu Studio launcher discovery
# ---------------------------------------------------------------------------

def _find_bambu_studio():
    """Locate Bambu Studio on the system.

    Returns a tuple (kind, launcher) where kind is one of:
      "native"  — launcher is a list [exe, ...] to prepend to [filepath]
      "flatpak" — launcher is the app id; call must use --file-forwarding
      None if Bambu Studio cannot be found.
    """
    system = platform.system()

    # 1. PATH lookup (works for deb installs and manually-staged AppImages)
    for name in ("bambu-studio", "BambuStudio", "bambustudio"):
        found = shutil.which(name)
        if found:
            return ("native", [found])

    # 2. Platform-specific well-known install locations
    if system == "Linux":
        for candidate in [
            "/usr/bin/bambu-studio",
            "/usr/local/bin/bambu-studio",
            "/opt/bambu-studio/bambu-studio",
            os.path.expanduser("~/Applications/BambuStudio.AppImage"),
            os.path.expanduser("~/Applications/Bambu_Studio.AppImage"),
            os.path.expanduser("~/bin/bambu-studio"),
        ]:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return ("native", [candidate])
        # 3. Flatpak
        if _flatpak_has_bambu():
            return ("flatpak", "com.bambulab.BambuStudio")
    elif system == "Darwin":
        for candidate in [
            "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
            os.path.expanduser(
                "~/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"),
        ]:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return ("native", [candidate])
    elif system == "Windows":
        for candidate in [
            r"C:\Program Files\Bambu Studio\bambu-studio.exe",
            r"C:\Program Files (x86)\Bambu Studio\bambu-studio.exe",
        ]:
            if os.path.isfile(candidate):
                return ("native", [candidate])

    return None


def _flatpak_has_bambu():
    """Return True if the Bambu Studio Flatpak is installed."""
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return False
    try:
        out = subprocess.run(
            [flatpak, "list", "--app", "--columns=application"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "com.bambulab.BambuStudio" in out.stdout


def _bambu_studio_running():
    """Best-effort check for whether a Bambu Studio window is already open."""
    system = platform.system()
    try:
        if system == "Windows":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq bambu-studio.exe"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            return "bambu-studio.exe" in out.stdout.lower()
        pgrep = shutil.which("pgrep")
        if pgrep is None:
            return False
        out = subprocess.run(
            [pgrep, "-f", "-i", "bambu[-_]?studio"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        return out.returncode == 0 and out.stdout.strip() != ""
    except (OSError, subprocess.TimeoutExpired):
        return False


def _maybe_show_single_instance_hint(parent=None):
    """Show a one-time explainer about Bambu's Single Instance setting."""
    if os.path.exists(_HINT_FLAG):
        return
    dlg = QtWidgets.QMessageBox(parent)
    dlg.setIcon(QtWidgets.QMessageBox.Information)
    dlg.setWindowTitle("Send to Bambu Studio — one-time tip")
    dlg.setTextFormat(QtCore.Qt.RichText)
    dlg.setText(
        "<b>Bambu Studio is already open.</b><br><br>"
        "By default, each Send to Bambu Studio opens a new window. "
        "To have each part appear as a new build plate in your existing "
        "window instead, enable <b>Single Instance</b> mode:<br><br>"
        "&nbsp;&nbsp;Bambu Studio &rarr; <i>File &rarr; Preferences</i> "
        "&rarr; General &rarr; check <b>Single Instance</b><br><br>"
        "Restart Bambu Studio once after enabling. "
        "This tip will not be shown again.")
    dlg.setStandardButtons(QtWidgets.QMessageBox.Ok)
    dlg.exec_()
    try:
        os.makedirs(os.path.dirname(_HINT_FLAG), exist_ok=True)
        with open(_HINT_FLAG, "w") as f:
            f.write("shown\n")
    except OSError:
        pass


def _build_launch_cmd(launcher_kind, launcher, filepath):
    """Build the argv list that opens `filepath` in Bambu Studio."""
    if launcher_kind == "flatpak":
        # --file-forwarding + @@ markers grant sandbox access to the file
        return [
            "flatpak", "run", "--file-forwarding", launcher,
            "@@", filepath, "@@",
        ]
    return list(launcher) + [filepath]


# ---------------------------------------------------------------------------
# Selection and export helpers
# ---------------------------------------------------------------------------

def _get_exportable_object():
    """Return the selected object suitable for mesh export, or None.

    Walks up to a PartDesign::Body when a feature inside one is selected.
    """
    sel = FreeCADGui.Selection.getSelectionEx()
    if len(sel) != 1:
        return None
    obj = sel[0].Object
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


def _sanitize_filename(name):
    """Reduce a FreeCAD label to a safe filename stem."""
    keep = "-_. "
    cleaned = "".join(c if (c.isalnum() or c in keep) else "_" for c in name)
    cleaned = cleaned.strip().strip(".") or "part"
    return cleaned


def _export_mesh(obj, filepath, linear_deflection, angular_deflection):
    """Tessellate `obj` and write a mesh file at `filepath`.

    The file extension determines the format: .3mf uses FreeCAD's 3MF
    exporter (preserves units, orientation, per-object grouping); .stl
    writes binary STL as a universal fallback.
    """
    ext = os.path.splitext(filepath)[1].lower()

    if hasattr(obj, "Shape") and obj.Shape and not obj.Shape.isNull():
        import MeshPart
        mesh_data = MeshPart.meshFromShape(
            Shape=obj.Shape,
            LinearDeflection=linear_deflection,
            AngularDeflection=angular_deflection,
            Relative=False,
        )
        if ext == ".3mf":
            # The 3MF exporter requires an actual document object.
            import Mesh
            doc = obj.Document
            temp = doc.addObject("Mesh::Feature", "_tcBambuTemp")
            temp.Label = _sanitize_filename(obj.Label)
            temp.Mesh = mesh_data
            try:
                Mesh.export([temp], filepath)
            finally:
                doc.removeObject(temp.Name)
        else:
            mesh_data.write(filepath)
    elif hasattr(obj, "Mesh"):
        if ext == ".3mf":
            import Mesh
            Mesh.export([obj], filepath)
        else:
            obj.Mesh.write(filepath)
    else:
        raise RuntimeError("Object has no exportable Shape or Mesh.")


def _shell_quote(s):
    if s and all(c.isalnum() or c in "-_./=:" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Task Panel
# ---------------------------------------------------------------------------

class SendToBambuTaskPanel:
    """PySide task panel with tessellation and format options."""

    def __init__(self, obj, launcher_kind, launcher):
        self.obj = obj
        self.launcher_kind = launcher_kind
        self.launcher = launcher
        self.form = self._build_ui()

    def _build_ui(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(widget)
        layout.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.AllNonFixedFieldsGrow)

        header = QtWidgets.QLabel("<b>Send to Bambu Studio</b>")
        header.setAlignment(QtCore.Qt.AlignCenter)
        layout.addRow(header)

        self.obj_label = QtWidgets.QLabel(self.obj.Label)
        layout.addRow("Object:", self.obj_label)

        sep1 = QtWidgets.QFrame()
        sep1.setFrameShape(QtWidgets.QFrame.HLine)
        layout.addRow(sep1)

        settings_header = QtWidgets.QLabel("<i>Mesh Settings</i>")
        settings_header.setAlignment(QtCore.Qt.AlignCenter)
        layout.addRow(settings_header)

        self.format_combo = QtWidgets.QComboBox()
        self.format_combo.addItems(["3MF (recommended)", "Binary STL"])
        self.format_combo.setToolTip(
            "3MF preserves units, orientation, and per-object grouping.\n"
            "STL is a universal fallback for older Bambu Studio builds.")
        layout.addRow("Format:", self.format_combo)

        self.linear_spin = QtWidgets.QDoubleSpinBox()
        self.linear_spin.setRange(0.001, 1.0)
        self.linear_spin.setValue(0.01)
        self.linear_spin.setSingleStep(0.01)
        self.linear_spin.setDecimals(3)
        self.linear_spin.setSuffix(" mm")
        self.linear_spin.setToolTip(
            "Linear deflection — max distance between the tessellated\n"
            "surface and the true CAD surface. 0.01 mm is print-grade\n"
            "for FDM; drop to 0.005 mm for SLA hero parts.")
        layout.addRow("Linear Deflection:", self.linear_spin)

        self.angular_spin = QtWidgets.QDoubleSpinBox()
        self.angular_spin.setRange(0.5, 30.0)
        self.angular_spin.setValue(5.0)
        self.angular_spin.setSingleStep(0.5)
        self.angular_spin.setDecimals(1)
        self.angular_spin.setSuffix(" deg")
        self.angular_spin.setToolTip(
            "Angular deflection — max angle between adjacent triangle\n"
            "normals on curved surfaces. 5 deg is print-grade.")
        layout.addRow("Angular Deflection:", self.angular_spin)

        sep2 = QtWidgets.QFrame()
        sep2.setFrameShape(QtWidgets.QFrame.HLine)
        layout.addRow(sep2)

        target = os.path.join(
            _STAGE_DIR,
            _sanitize_filename(self.obj.Label) + self._current_extension())
        self.path_label = QtWidgets.QLabel(
            '<span style="color: #666; font-size: 11px;">'
            'Will save to: {}</span>'.format(target))
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse)
        layout.addRow(self.path_label)

        launcher_desc = ("Flatpak Bambu Studio"
                         if self.launcher_kind == "flatpak"
                         else "Bambu Studio")
        info = QtWidgets.QLabel(
            '<span style="color: #666; font-size: 11px;">'
            'Will open in {}. Enable <b>Single Instance</b> in Bambu '
            'Studio &rarr; File &rarr; Preferences to add each part as a new '
            'build plate in the same window.</span>'
            .format(launcher_desc))
        info.setAlignment(QtCore.Qt.AlignCenter)
        info.setWordWrap(True)
        layout.addRow(info)

        self.format_combo.currentIndexChanged.connect(self._refresh_path)
        return widget

    def _current_extension(self):
        return ".3mf" if self.format_combo.currentIndex() == 0 else ".stl"

    def _refresh_path(self):
        target = os.path.join(
            _STAGE_DIR,
            _sanitize_filename(self.obj.Label) + self._current_extension())
        self.path_label.setText(
            '<span style="color: #666; font-size: 11px;">'
            'Will save to: {}</span>'.format(target))

    # -- Task panel interface -----------------------------------------------

    def accept(self):
        os.makedirs(_STAGE_DIR, exist_ok=True)
        filepath = os.path.join(
            _STAGE_DIR,
            _sanitize_filename(self.obj.Label) + self._current_extension())

        linear = self.linear_spin.value()
        angular_deg = self.angular_spin.value()
        angular_rad = angular_deg * 3.141592653589793 / 180.0

        try:
            _export_mesh(self.obj, filepath, linear, angular_rad)
        except Exception as e:
            FreeCAD.Console.PrintError(
                "Bambu Studio export failed: {}\n".format(e))
            QtWidgets.QMessageBox.critical(
                None, "Export Error",
                "Failed to export mesh:\n\n{}".format(e))
            return False

        cmd = _build_launch_cmd(self.launcher_kind, self.launcher, filepath)

        FreeCADGui.Control.closeDialog()

        if _bambu_studio_running():
            _maybe_show_single_instance_hint()

        launched = False
        try:
            # Clean environment: inheriting FreeCAD's AppImage variables
            # points Bambu's network plug-in at FreeCAD's CA bundle and
            # hands it FreeCAD's focus token. See launch_env.py.
            subprocess.Popen(cmd, **popen_kwargs())
            launched = True
        except (PermissionError, OSError) as e:
            FreeCAD.Console.PrintWarning(
                "Direct Bambu Studio launch failed ({}), showing command.\n"
                .format(e))

        if launched:
            FreeCAD.Console.PrintMessage(
                "Sent '{}' to Bambu Studio ({})\n".format(
                    self.obj.Label, filepath))
        else:
            shell_cmd = " ".join(_shell_quote(c) for c in cmd)
            _show_launch_dialog(shell_cmd, filepath)

        return True

    def reject(self):
        FreeCADGui.Control.closeDialog()
        return True

    def getStandardButtons(self):
        return (
            QtWidgets.QDialogButtonBox.Ok
            | QtWidgets.QDialogButtonBox.Cancel
        )


def _show_launch_dialog(shell_cmd, filepath):
    """Fallback dialog when direct launch fails (typically snap sandbox)."""
    dlg = QtWidgets.QDialog(None)
    dlg.setWindowTitle("Send to Bambu Studio")
    dlg.setMinimumWidth(500)
    layout = QtWidgets.QVBoxLayout(dlg)

    layout.addWidget(QtWidgets.QLabel(
        "<b>Mesh exported successfully.</b><br><br>"
        "FreeCAD cannot launch Bambu Studio directly.<br>"
        "Run this command in a terminal:"))

    cmd_edit = QtWidgets.QLineEdit(shell_cmd)
    cmd_edit.setReadOnly(True)
    cmd_edit.selectAll()
    layout.addWidget(cmd_edit)

    file_label = QtWidgets.QLabel(
        '<span style="color: #666; font-size: 11px;">'
        'File: {}</span>'.format(filepath))
    file_label.setWordWrap(True)
    layout.addWidget(file_label)

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

class SendToBambuStudioCommand:
    """Export the selection and open it in Bambu Studio for slicing."""

    def GetResources(self):
        return {
            "Pixmap": os.path.join(
                _plugin_dir, "resources", "icons", "SendToBambuStudio.svg"),
            "MenuText": "Send to Bambu Studio",
            "ToolTip": (
                "Tessellate the selected body at print-grade resolution\n"
                "and open it in Bambu Studio, ready to slice."
            ),
        }

    def IsActive(self):
        return _get_exportable_object() is not None

    def Activated(self):
        obj = _get_exportable_object()
        if obj is None:
            return

        found = _find_bambu_studio()
        if found is None:
            QtWidgets.QMessageBox.warning(
                None, "Send to Bambu Studio",
                "Bambu Studio was not found on this system.\n\n"
                "Install it from https://bambulab.com/en/download/studio\n"
                "and ensure the 'bambu-studio' executable is in your PATH,\n"
                "or install the Flatpak:\n"
                "  flatpak install flathub com.bambulab.BambuStudio")
            return

        kind, launcher = found
        panel = SendToBambuTaskPanel(obj, kind, launcher)
        FreeCADGui.Control.showDialog(panel)


FreeCADGui.addCommand(
    "TrailCurrent_SendToBambuStudio", SendToBambuStudioCommand())
