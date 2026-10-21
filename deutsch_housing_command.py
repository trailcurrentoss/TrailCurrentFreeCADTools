"""FreeCAD command and task-panel UI for the Deutsch termination housings.

One command places any size listed in deutsch_housing_geometry.VARIANTS. The
older single-size commands (DTM2P, DTM4P, DTM12P) stay registered so that
documents created with them keep recomputing.
"""

import os
import FreeCAD
import FreeCADGui
import Part

from FreeCAD import Vector
from PySide import QtWidgets, QtCore

_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_ICON = os.path.join(_plugin_dir, "resources", "icons", "DeutschHousing.svg")


# ---------------------------------------------------------------------------
# FeaturePython proxy & ViewProvider (enables double-click re-edit)
# ---------------------------------------------------------------------------

class _DeutschHousingProxy:
    """Data proxy for a housing PartDesign::FeatureAdditivePython."""

    def __init__(self, obj):
        obj.Proxy = self

    def execute(self, obj):
        """Recompute the fused shape."""
        if not hasattr(obj, "Housing_Variant"):
            return

        if hasattr(obj, "BaseFeature") and obj.BaseFeature is not None:
            base_shape = obj.BaseFeature.Shape
        elif getattr(obj, "Housing_OriginalBody", ""):
            original = obj.Document.getObject(obj.Housing_OriginalBody)
            if original is None or original.Shape.isNull():
                return
            base_shape = original.Shape
        else:
            return

        try:
            from deutsch_housing_geometry import (
                place_housing_from_frame, placed_housing)

            anchor, u_axis, v_axis, normal = _frame_for(obj)
            if anchor is None:
                return

            key = obj.Housing_Variant
            args = (anchor, u_axis, v_axis, normal,
                    obj.Housing_XOffset, obj.Housing_YOffset,
                    obj.Housing_Rotation)
            new_shape = place_housing_from_frame(base_shape, key, *args)
            if new_shape and not new_shape.isNull():
                new_shape.transformShape(
                    obj.Placement.inverse().toMatrix(), True)
                obj.Shape = new_shape
                if hasattr(obj, "AddSubShape"):
                    tool = placed_housing(key, *args)
                    tool.transformShape(
                        obj.Placement.inverse().toMatrix(), True)
                    obj.AddSubShape = tool
        except Exception as e:
            FreeCAD.Console.PrintError(
                "Deutsch housing recompute failed: {}\n".format(e))

    def __getstate__(self):
        return None

    def __setstate__(self, state):
        return None


class _DeutschHousingViewProvider:
    """ViewProvider that enables double-click re-editing."""

    def __init__(self, vobj):
        vobj.Proxy = self

    def attach(self, vobj):
        self.Object = vobj.Object

    def doubleClicked(self, vobj):
        try:
            return _open_edit_panel(vobj.Object)
        except Exception as e:
            FreeCAD.Console.PrintError(
                "Deutsch housing doubleClicked error: {}\n".format(e))
            return False

    def setEdit(self, vobj, mode=0):
        if mode != 0:
            return False
        try:
            return _open_edit_panel(vobj.Object)
        except Exception as e:
            FreeCAD.Console.PrintError(
                "Deutsch housing setEdit error: {}\n".format(e))
            return False

    def unsetEdit(self, vobj, mode=0):
        FreeCADGui.Control.closeDialog()
        return True

    def getIcon(self):
        return _ICON

    def __getstate__(self):
        return None

    def __setstate__(self, state):
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_body(obj):
    """Return the PartDesign::Body that *obj* belongs to, or None."""
    if hasattr(obj, "TypeId") and obj.TypeId == "PartDesign::Body":
        return obj
    if hasattr(obj, "getParentGeoFeatureGroup"):
        parent = obj.getParentGeoFeatureGroup()
        if (parent is not None
                and hasattr(parent, "TypeId")
                and parent.TypeId == "PartDesign::Body"):
            return parent
    return None


def _add_properties(obj, key):
    """Add the storage properties to a housing feature object."""
    from deutsch_housing_geometry import variant_keys
    obj.addProperty(
        "App::PropertyEnumeration", "Housing_Variant", "Housing",
        "Deutsch receptacle size")
    obj.Housing_Variant = variant_keys()
    obj.Housing_Variant = key
    obj.addProperty(
        "App::PropertyFloat", "Housing_XOffset", "Housing",
        "Offset along the face U axis (mm)")
    obj.addProperty(
        "App::PropertyFloat", "Housing_YOffset", "Housing",
        "Offset along the face V axis (mm)")
    obj.addProperty(
        "App::PropertyFloat", "Housing_Rotation", "Housing",
        "Rotation angle on the face (degrees)")
    obj.addProperty(
        "App::PropertyString", "Housing_FaceName", "Housing",
        "Face used on the original body (display only)")
    obj.addProperty(
        "App::PropertyString", "Housing_EdgeName", "Housing",
        "Edge used for front alignment (display only)")
    obj.addProperty(
        "App::PropertyString", "Housing_OriginalBody", "Housing",
        "Original body object name")
    # The frame (anchor + U/V/normal) is the source of truth for placement.
    # Storing it directly decouples the housing from face/edge indices,
    # which renumber whenever upstream features change.
    obj.addProperty(
        "App::PropertyVector", "Housing_FrameAnchor", "Housing",
        "Frame anchor in world coordinates (edge midpoint when edge-aligned)")
    obj.addProperty(
        "App::PropertyVector", "Housing_FrameU", "Housing",
        "Frame U axis (housing local X)")
    obj.addProperty(
        "App::PropertyVector", "Housing_FrameV", "Housing",
        "Frame V axis (housing local Y)")
    obj.addProperty(
        "App::PropertyVector", "Housing_FrameNormal", "Housing",
        "Frame normal (housing local Z, outward from face)")
    obj.addProperty(
        "App::PropertyBool", "Housing_EdgeAligned", "Housing",
        "Front of the housing sits on the stored edge")


def _store(obj, key, frame, edge_aligned, x_offset, y_offset, rotation,
           face_name, edge_name):
    anchor, u_axis, v_axis, normal = frame
    obj.Housing_Variant = key
    obj.Housing_FrameAnchor = Vector(anchor)
    obj.Housing_FrameU = Vector(u_axis)
    obj.Housing_FrameV = Vector(v_axis)
    obj.Housing_FrameNormal = Vector(normal)
    obj.Housing_EdgeAligned = edge_aligned
    obj.Housing_XOffset = x_offset
    obj.Housing_YOffset = y_offset
    obj.Housing_Rotation = rotation
    obj.Housing_FaceName = face_name
    obj.Housing_EdgeName = edge_name


def _frame_for(obj):
    """Return (anchor, u, v, normal) for *obj*, front-shifted if edge-aligned."""
    from deutsch_housing_geometry import edge_anchor
    n = Vector(obj.Housing_FrameNormal)
    if n.Length < 1e-9:
        return (None, None, None, None)
    anchor = Vector(obj.Housing_FrameAnchor)
    v_axis = Vector(obj.Housing_FrameV)
    if obj.Housing_EdgeAligned:
        anchor = edge_anchor(anchor, v_axis, obj.Housing_Variant)
    return anchor, Vector(obj.Housing_FrameU), v_axis, n


def _raw_frame(face, edge):
    """Frame from a face and optional edge; edge anchors are left unshifted."""
    from deutsch_housing_geometry import compute_face_frame, compute_edge_frame
    if edge is not None:
        return compute_edge_frame(face, edge), True
    return compute_face_frame(face), False


def _get_base_object(obj):
    """Return the base object that a housing result was derived from."""
    if hasattr(obj, "BaseFeature") and obj.BaseFeature is not None:
        return obj.BaseFeature
    name = getattr(obj, "Housing_OriginalBody", "")
    return obj.Document.getObject(name) if name else None


def _is_planar(face):
    surface = face.Surface
    return hasattr(surface, "Axis") or isinstance(surface, Part.Plane)


def _read_face_edge_from_selection(skip_obj=None):
    """Read the first Face (and optional Edge) from the active selection.

    Returns (source_obj, face_name, face, edge_name, edge) or
    (None, "", None, "", None) if nothing usable is selected.
    """
    for s in FreeCADGui.Selection.getSelectionEx():
        if skip_obj is not None and s.Object is skip_obj:
            continue
        face_name = next((n for n in s.SubElementNames
                          if n.startswith("Face")), "")
        if not face_name:
            continue
        edge_name = next((n for n in s.SubElementNames
                          if n.startswith("Edge")), "")
        try:
            face = getattr(s.Object.Shape, face_name)
        except Exception:
            continue
        edge = None
        if edge_name:
            try:
                edge = getattr(s.Object.Shape, edge_name)
            except Exception:
                edge_name = ""
        return s.Object, face_name, face, edge_name, edge
    return None, "", None, "", None


def _open_edit_panel(obj):
    if not hasattr(obj, "Housing_Variant"):
        return False
    original = _get_base_object(obj)
    if original is None:
        QtWidgets.QMessageBox.warning(
            None, "Deutsch Housing",
            "Cannot re-edit: the original body object was deleted.")
        return True
    panel = DeutschHousingTaskPanel(
        original, obj.Housing_FaceName, edge_name=obj.Housing_EdgeName,
        edit_obj=obj)
    FreeCADGui.Control.showDialog(panel)
    return True


# ---------------------------------------------------------------------------
# Task Panel (sidebar UI)
# ---------------------------------------------------------------------------

class DeutschHousingTaskPanel:
    """Task panel shown in the sidebar while placing or editing a housing."""

    def __init__(self, body_obj, face_name, edge_name="", edit_obj=None,
                 face=None, edge=None):
        self.body_obj = body_obj
        self.face_name = face_name
        self.edge_name = edge_name
        self.edit_obj = edit_obj
        if edit_obj is not None:
            # Keep the stored frame so the housing stays put even if the
            # face/edge names no longer resolve on the base shape.
            self._frame = (Vector(edit_obj.Housing_FrameAnchor),
                           Vector(edit_obj.Housing_FrameU),
                           Vector(edit_obj.Housing_FrameV),
                           Vector(edit_obj.Housing_FrameNormal))
            self._edge_aligned = edit_obj.Housing_EdgeAligned
        else:
            self._frame, self._edge_aligned = _raw_frame(face, edge)
        self.form = self._build_ui()
        if edit_obj is not None:
            self.variant_combo.setCurrentIndex(
                self.variant_combo.findData(edit_obj.Housing_Variant))
            self.x_offset_spin.setValue(edit_obj.Housing_XOffset)
            self.y_offset_spin.setValue(edit_obj.Housing_YOffset)
            self.rotation_spin.setValue(edit_obj.Housing_Rotation)
        self._refresh_attachment_label()

    def _build_ui(self):
        from deutsch_housing_geometry import (
            variant_keys, variant_label, DEFAULT_VARIANT)

        widget = QtWidgets.QWidget()
        layout = QtWidgets.QFormLayout(widget)
        layout.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.AllNonFixedFieldsGrow)

        header = QtWidgets.QLabel("<b>Deutsch Termination Housing</b>")
        header.setAlignment(QtCore.Qt.AlignCenter)
        layout.addRow(header)

        info = QtWidgets.QLabel(
            "<i>Places a termination housing for the chosen Deutsch\n"
            "receptacle onto the selected face, extending outward.</i>")
        info.setWordWrap(True)
        layout.addRow(info)

        self.variant_combo = QtWidgets.QComboBox()
        for key in variant_keys():
            self.variant_combo.addItem(variant_label(key), key)
        self.variant_combo.setCurrentIndex(
            self.variant_combo.findData(DEFAULT_VARIANT))
        layout.addRow("Connector:", self.variant_combo)

        self.attachment_label = QtWidgets.QLabel("")
        self.attachment_label.setWordWrap(True)
        layout.addRow(self.attachment_label)

        if self.edit_obj is not None:
            reattach_btn = QtWidgets.QPushButton("Re-attach to selection")
            reattach_btn.setToolTip(
                "Select a Face (and optional Edge) in the 3D view, then\n"
                "click here to re-anchor this housing without rebuilding\n"
                "the model. Useful when upstream geometry has changed.")
            reattach_btn.clicked.connect(self._on_reattach_clicked)
            layout.addRow(reattach_btn)

        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.HLine)
        layout.addRow(sep)

        def spin(lo, hi, step, suffix):
            s = QtWidgets.QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setSingleStep(step)
            s.setDecimals(1)
            s.setSuffix(suffix)
            return s

        self.x_offset_spin = spin(-500.0, 500.0, 1.0, " mm")
        self.x_label = QtWidgets.QLabel("X Offset:")
        layout.addRow(self.x_label, self.x_offset_spin)

        self.y_offset_spin = spin(-500.0, 500.0, 1.0, " mm")
        self.y_label = QtWidgets.QLabel("Y Offset:")
        layout.addRow(self.y_label, self.y_offset_spin)

        self.rotation_spin = spin(-180.0, 180.0, 5.0, " deg")
        self.rotation_spin.setToolTip("Rotation angle on the face (degrees).")
        layout.addRow("Rotation:", self.rotation_spin)

        return widget

    def _refresh_attachment_label(self):
        body_label = self.body_obj.Label if self.body_obj is not None else "?"
        if self._edge_aligned:
            self.attachment_label.setText(
                "<b>Attached to:</b> {}.{}<br/>"
                "<b>Front aligned to:</b> {}".format(
                    body_label, self.face_name, self.edge_name))
            self.x_label.setText("Along Edge:")
            self.x_offset_spin.setToolTip("Offset along the edge direction.")
            self.y_label.setText("From Edge:")
            self.y_offset_spin.setToolTip(
                "Offset perpendicular to the edge (positive = away from edge).")
        else:
            self.attachment_label.setText(
                "<b>Attached to:</b> {}.{}<br/>"
                "<i>Tip: Select a Face + Edge to align the housing front "
                "to the edge.</i>".format(body_label, self.face_name))
            self.x_label.setText("X Offset:")
            self.x_offset_spin.setToolTip(
                "Offset along the model X axis (projected onto the face).")
            self.y_label.setText("Y Offset:")
            self.y_offset_spin.setToolTip(
                "Offset along the model Y axis (projected onto the face).")

    def _on_reattach_clicked(self):
        src_obj, face_name, face, edge_name, edge = \
            _read_face_edge_from_selection(skip_obj=self.edit_obj)
        if src_obj is None or face is None:
            QtWidgets.QMessageBox.information(
                None, "Deutsch Housing",
                "Select a Face (and optionally an Edge) on a body in the\n"
                "3D view, then click 'Re-attach to selection' again.")
            return
        if not _is_planar(face):
            QtWidgets.QMessageBox.warning(
                None, "Non-Planar Face", "Please select a flat (planar) face.")
            return
        self._frame, self._edge_aligned = _raw_frame(face, edge)
        self.body_obj = src_obj
        self.face_name = face_name
        self.edge_name = edge_name
        self._refresh_attachment_label()

    def accept(self):
        from deutsch_housing_geometry import place_housing_from_frame

        doc = FreeCAD.ActiveDocument
        key = self.variant_combo.currentData()
        values = (self.x_offset_spin.value(), self.y_offset_spin.value(),
                  self.rotation_spin.value())
        body = _find_body(self.body_obj)
        is_pd_edit = (self.edit_obj is not None
                      and hasattr(self.edit_obj, "BaseFeature"))

        new_shape = None
        if not is_pd_edit:
            from deutsch_housing_geometry import edge_anchor
            anchor, u_axis, v_axis, normal = self._frame
            if self._edge_aligned:
                anchor = edge_anchor(anchor, v_axis, key)
            try:
                new_shape = place_housing_from_frame(
                    self.body_obj.Shape, key, anchor, u_axis, v_axis, normal,
                    *values)
            except Exception as e:
                FreeCAD.Console.PrintError(
                    "Deutsch housing placement failed: {}\n".format(e))
                QtWidgets.QMessageBox.critical(
                    None, "Deutsch Housing Error",
                    "Fuse operation failed:\n\n{}\n\n"
                    "Make sure you selected a flat face with enough "
                    "room for the housing.".format(e))
                return False

        store = (key, self._frame, self._edge_aligned) + values + (
            self.face_name, self.edge_name)

        if is_pd_edit:
            result_obj = self.edit_obj
            _store(result_obj, *store)
        elif body is not None:
            result_obj = doc.addObject(
                "PartDesign::FeatureAdditivePython", "DeutschHousing")
            _DeutschHousingProxy(result_obj)
            _DeutschHousingViewProvider(result_obj.ViewObject)
            _add_properties(result_obj, key)
            _store(result_obj, *store)
            result_obj.Housing_OriginalBody = body.Name
            body.addObject(result_obj)
        else:
            if self.edit_obj is not None:
                doc.removeObject(self.edit_obj.Name)
            result_obj = doc.addObject("Part::FeaturePython", "DeutschHousing")
            _DeutschHousingProxy(result_obj)
            _DeutschHousingViewProvider(result_obj.ViewObject)
            _add_properties(result_obj, key)
            _store(result_obj, *store)
            result_obj.Housing_OriginalBody = self.body_obj.Name
            result_obj.Shape = new_shape
            src_vo = self.body_obj.ViewObject
            dst_vo = result_obj.ViewObject
            src_vo.Visibility = False
            if hasattr(src_vo, "ShapeColor"):
                dst_vo.ShapeColor = src_vo.ShapeColor
            if hasattr(src_vo, "Transparency"):
                dst_vo.Transparency = src_vo.Transparency

        doc.recompute()
        FreeCADGui.Control.closeDialog()
        FreeCAD.Console.PrintMessage(
            "{} housing placed onto {}.{}{}\n".format(
                key, self.body_obj.Label, self.face_name,
                " aligned to " + self.edge_name if self._edge_aligned else ""))
        return True

    def reject(self):
        FreeCADGui.Control.closeDialog()
        return True

    def getStandardButtons(self):
        return (QtWidgets.QDialogButtonBox.Ok
                | QtWidgets.QDialogButtonBox.Cancel)


# ---------------------------------------------------------------------------
# FreeCAD Command
# ---------------------------------------------------------------------------

class DeutschHousingCommand:
    """Place a Deutsch termination housing of any listed size onto a face."""

    def GetResources(self):
        return {
            "Pixmap": _ICON,
            "MenuText": "Place Deutsch Housing",
            "ToolTip": (
                "Deutsch Housing\n"
                "Place a termination housing for a Deutsch DTM receptacle\n"
                "(2, 3, 4, 6, 8 or 12 position) onto the selected flat face.\n"
                "Select a Face + Edge to align the housing front to the edge.\n"
                "Select an existing housing to re-edit it or change its size."
            ),
        }

    def IsActive(self):
        sel = FreeCADGui.Selection.getSelectionEx()
        if len(sel) != 1:
            return False
        if hasattr(sel[0].Object, "Housing_Variant"):
            return True
        return any(n.startswith("Face") for n in sel[0].SubElementNames)

    def Activated(self):
        sel = FreeCADGui.Selection.getSelectionEx()
        if not sel:
            return
        obj = sel[0].Object
        sub_names = sel[0].SubElementNames
        has_face_sub = any(n.startswith("Face") for n in sub_names)

        # Re-edit an existing housing (only when no face is selected)
        if hasattr(obj, "Housing_Variant") and not has_face_sub:
            _open_edit_panel(obj)
            return

        face_name = next((n for n in sub_names if n.startswith("Face")), "")
        edge_name = next((n for n in sub_names if n.startswith("Edge")), "")
        if not face_name:
            return
        face = getattr(obj.Shape, face_name)
        edge = getattr(obj.Shape, edge_name) if edge_name else None

        if not _is_planar(face):
            QtWidgets.QMessageBox.warning(
                None, "Non-Planar Face",
                "Please select a flat (planar) face.\n"
                "The housing can only be placed on flat surfaces.")
            return

        panel = DeutschHousingTaskPanel(
            obj, face_name, edge_name=edge_name, face=face, edge=edge)
        FreeCADGui.Control.showDialog(panel)


FreeCADGui.addCommand("TrailCurrent_DeutschHousing", DeutschHousingCommand())
