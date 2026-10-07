# -*- coding: utf-8 -*-
"""XML-RPC server that runs inside FreeCAD to expose its Python environment.

This server runs in a background thread and marshals code execution to
FreeCAD's main (GUI) thread via a QTimer-polled queue, ensuring all
FreeCAD/Part/Gui operations execute safely.
"""

import ast
import io
import json
import queue
import re
import sys
import threading
import traceback
from xmlrpc.server import SimpleXMLRPCServer

import FreeCAD

DEFAULT_PORT = 12785

_server = None
_server_thread = None
_exec_queue = queue.Queue()
_timer = None
_watchdog = None
_namespace = {}
_dialog_log = []


# ── Modal dialog guard ─────────────────────────────────────────────
#
# FreeCAD library code raises modal dialogs on its own initiative -- the DXF
# importer asks permission to download its helper libraries, for instance.
# A modal dialog spins a nested event loop on the main thread, which is the
# same thread this server marshals work onto. Nothing queued afterwards can
# run, so every subsequent call times out and the only recovery is a human
# clicking the button. A scripted caller has no hands. We answer on its
# behalf and report what was suppressed, so the caller gets a fast, truthful
# failure instead of an unexplained hang.


def _install_dialog_guard():
    """Replace blocking dialog entry points with non-blocking stubs.

    Returns a restore token (or None if PySide is unavailable). Only active
    while MCP-supplied code runs; interactive FreeCAD use is untouched.
    """
    try:
        from PySide import QtWidgets
    except Exception:
        return None

    saved = []

    def record(kind, args):
        detail = " | ".join(a for a in args if isinstance(a, str))
        _dialog_log.append("{}: {}".format(kind, detail[:500]))

    def patch(owner, name, replacement):
        try:
            saved.append((owner, name, owner.__dict__.get(name, getattr(owner, name, None))))
            setattr(owner, name, replacement)
        except Exception:
            pass

    mb = QtWidgets.QMessageBox
    for meth, answer in (("question", mb.No), ("information", mb.Ok),
                         ("warning", mb.Ok), ("critical", mb.Ok), ("about", None)):
        def stub(*args, _m=meth, _a=answer, **kwargs):
            record("QMessageBox." + _m, args)
            return _a
        patch(mb, meth, staticmethod(stub))

    def dlg_exec(self, *args, **kwargs):
        try:
            title = self.windowTitle()
        except Exception:
            title = type(self).__name__
        record("modal " + type(self).__name__, (title,))
        try:
            self.reject()
        except Exception:
            pass
        return 0  # QDialog.Rejected

    for cls in (QtWidgets.QDialog, QtWidgets.QMessageBox, QtWidgets.QFileDialog,
                QtWidgets.QInputDialog, QtWidgets.QProgressDialog):
        for meth in ("exec_", "exec"):
            if hasattr(cls, meth):
                patch(cls, meth, dlg_exec)

    return saved


def _remove_dialog_guard(saved):
    if not saved:
        return
    for owner, name, original in reversed(saved):
        try:
            if original is None:
                delattr(owner, name)
            else:
                setattr(owner, name, original)
        except Exception:
            pass


def _watchdog_tick():
    """Safety net: force-close any modal widget that slipped past the guard.

    Runs on the main thread. If a modal dialog was raised outside a guarded
    execution (a background task, or an event-loop re-entry we did not wrap)
    this closes it so the server does not stay wedged.
    """
    try:
        from PySide import QtWidgets
        w = QtWidgets.QApplication.activeModalWidget()
        if w is not None and not _exec_queue.empty():
            try:
                title = w.windowTitle()
            except Exception:
                title = type(w).__name__
            _dialog_log.append("watchdog closed modal: {}".format(title))
            FreeCAD.Console.PrintWarning(
                "MCP watchdog closed a modal dialog: {}\n".format(title))
            w.close()
    except Exception:
        pass


def _init_namespace():
    """Pre-import common FreeCAD modules into the execution namespace."""
    for alias, module_name in [
        ("FreeCAD", "FreeCAD"), ("App", "FreeCAD"),
        ("FreeCADGui", "FreeCADGui"), ("Gui", "FreeCADGui"),
        ("Part", "Part"), ("Mesh", "Mesh"), ("Draft", "Draft"),
        ("Sketcher", "Sketcher"), ("PartDesign", "PartDesign"),
        ("Import", "Import"), ("ImportGui", "ImportGui"),
        ("importSVG", "importSVG"), ("importDXF", "importDXF"),
        ("MeshPart", "MeshPart"), ("BOPTools", "BOPTools"),
    ]:
        try:
            _namespace[alias] = __import__(module_name)
        except ImportError:
            pass
    # Convenience builtins
    import builtins
    _namespace["__builtins__"] = builtins
    # Capture helpers, so screenshot() can reach them from executed code
    _namespace["_mcp_looks_blank"] = _looks_blank
    _namespace["_mcp_grab_view"] = _grab_view_widget


# ── Console capture ────────────────────────────────────────────────
#
# FreeCAD.Console.PrintWarning / PrintError do not go to sys.stderr. They go
# to the Console observers, and the one a caller can read back is the Report
# view. Recompute failures, deprecation notices and most "this did nothing"
# diagnostics only ever appear there, so a caller that watches stdout and
# stderr sees a clean success while the Report view fills with red.
#
# The Report view keeps no record of each line's level, but its syntax
# highlighter colours lines by level at display time, and those colours are
# readable from the block layout. The colours are learned by printing one
# line at each level, so a theme or preference change cannot break it.

_report_widget = None
_level_by_colour = {}
_TIMESTAMP = re.compile(r"^\d{2}:\d{2}:\d{2}\s+")
_MAX_CONSOLE_LINES = 40


def _report_view():
    global _report_widget
    if _report_widget is None:
        try:
            from PySide import QtWidgets
            import FreeCADGui
            _report_widget = FreeCADGui.getMainWindow().findChild(
                QtWidgets.QTextEdit, "Report view")
        except Exception:
            _report_widget = None
    return _report_widget


def _flush_report_view(rv):
    """Deliver the Report view's pending messages.

    ReportOutput receives Console messages as posted Qt events, so a line
    printed a moment ago is not in the widget until the event loop runs.
    Delivering only this widget's events avoids re-entering the queue timer.
    """
    try:
        from PySide import QtCore
        QtCore.QCoreApplication.sendPostedEvents(rv, 0)
    except Exception:
        pass


def _block_level(block):
    try:
        for r in block.layout().formats():
            lvl = _level_by_colour.get(r.format.foreground().color().name())
            if lvl:
                return lvl
    except Exception:
        pass
    return "message"


def _calibrate_report_levels():
    """Learn which colours the Report view uses for warnings and errors.

    The colours come from the OutputWindow preferences when the user or a
    theme has set them. Only when they are unset does this print one line
    at each level and read the colour back, which is the one time the
    probe lines appear in the Report view.
    """
    rv = _report_view()
    if rv is None:
        return
    try:
        pg = FreeCAD.ParamGet("User parameter:BaseApp/Preferences/OutputWindow")
        for key, lvl in (("colorWarning", "warning"), ("colorError", "error")):
            rgba = pg.GetUnsigned(key, 0)
            if rgba:
                _level_by_colour["#%06x" % (rgba >> 8)] = lvl
        if len(set(_level_by_colour.values())) == 2:
            return
    except Exception:
        pass
    try:
        qd = rv.document()
        n0 = max(qd.blockCount() - 1, 0)
        probes = {"warning": "MCP console capture: warning colour",
                  "error": "MCP console capture: error colour"}
        FreeCAD.Console.PrintWarning(probes["warning"] + "\n")
        FreeCAD.Console.PrintError(probes["error"] + "\n")
        _flush_report_view(rv)
        blk = qd.findBlockByNumber(n0)
        while blk.isValid():
            for lvl, text in probes.items():
                if text in blk.text():
                    for r in blk.layout().formats():
                        _level_by_colour[r.format.foreground().color().name()] = lvl
            blk = blk.next()
    except Exception:
        pass


class _ReportCapture:
    """Snapshot the Report view, then collect what was added, by level."""

    def __init__(self):
        self.rv = None
        self.saved_status = {}
        try:
            rv = _report_view()
            if rv is None:
                return
            if not _level_by_colour:
                _calibrate_report_levels()
            # Record warnings and errors while we watch, whatever the user's
            # Report view filter says; restored in collect().
            for key in ("Wrn", "Err"):
                try:
                    self.saved_status[key] = FreeCAD.Console.GetStatus("ReportOutput", key)
                    FreeCAD.Console.SetStatus("ReportOutput", key, True)
                except Exception:
                    pass
            qd = rv.document()
            self.n0 = max(qd.blockCount() - 1, 0)
            self.prefix = qd.findBlockByNumber(self.n0).text()
            self.rv = rv
        except Exception:
            self.rv = None

    def collect(self):
        if self.rv is None:
            return {}
        result = {}
        try:
            _flush_report_view(self.rv)
            for key, val in self.saved_status.items():
                try:
                    FreeCAD.Console.SetStatus("ReportOutput", key, val)
                except Exception:
                    pass
            lines = {"warning": [], "error": []}
            blk = self.rv.document().findBlockByNumber(self.n0)
            first = True
            while blk.isValid():
                text = blk.text()
                if first:
                    first = False
                    if self.prefix and text.startswith(self.prefix):
                        text = text[len(self.prefix):]
                text = _TIMESTAMP.sub("", text).rstrip()
                lvl = _block_level(blk)
                if text and lvl in lines:
                    lines[lvl].append(text)
                blk = blk.next()
            for lvl, key in (("warning", "console_warnings"), ("error", "console_errors")):
                found = lines[lvl]
                if found:
                    if len(found) > _MAX_CONSOLE_LINES:
                        extra = len(found) - _MAX_CONSOLE_LINES
                        found = found[:_MAX_CONSOLE_LINES] + ["... {} more".format(extra)]
                    result[key] = found
        except Exception:
            pass
        return result


def _compile(code):
    """Pick eval or exec from the syntax alone, before anything runs.

    Trying eval first and falling back to exec inside the except clause
    chains every genuine exception under a bogus SyntaxError at line 1, so
    the caller reads two tracebacks and the real one is the second.
    """
    try:
        ast.parse(code, mode="eval")
        is_expr = True
    except SyntaxError:
        is_expr = False
    # Compiled outside the handler: a genuine SyntaxError in statement code
    # must surface on its own, not chained under the eval attempt.
    return compile(code, "<mcp>", "eval" if is_expr else "exec"), is_expr


def _process_queue():
    """Called on the main thread by QTimer. Executes queued code."""
    while not _exec_queue.empty():
        try:
            code, timeout_event, container = _exec_queue.get_nowait()
        except queue.Empty:
            break

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        old_stdout, old_stderr = sys.stdout, sys.stderr

        del _dialog_log[:]
        guard = _install_dialog_guard()
        capture = _ReportCapture()

        try:
            sys.stdout = stdout_buf
            sys.stderr = stderr_buf
            compiled, is_expr = _compile(code)
            if is_expr:
                result = eval(compiled, _namespace)
                if result is not None:
                    container["result"] = repr(result)
            else:
                exec(compiled, _namespace)
        except Exception:
            container["error"] = traceback.format_exc()
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            _remove_dialog_guard(guard)

        container.update(capture.collect())

        stdout_val = stdout_buf.getvalue()
        stderr_val = stderr_buf.getvalue()
        if stdout_val:
            container["stdout"] = stdout_val
        if stderr_val:
            container["stderr"] = stderr_val
        if _dialog_log:
            # Surface suppressed prompts: the operation may have silently taken
            # the "No" branch, and the caller needs to know that happened.
            container["dialogs"] = list(_dialog_log)

        timeout_event.set()


# ── RPC-exposed methods ────────────────────────────────────────────

def ping():
    """Health check."""
    return True


def execute(code, timeout=60):
    """Execute Python code on FreeCAD's main thread.

    Returns a dict with optional keys: result, stdout, stderr, error,
    console_warnings, console_errors, dialogs.
    """
    event = threading.Event()
    container = {}
    _exec_queue.put((code, event, container))
    if not event.wait(timeout=timeout):
        return {"error": "Execution timed out after {}s".format(timeout)}
    return container


def _looks_blank(path):
    """True if the image has no drawn content.

    Counts distinct colours along three horizontal scanlines. A blank view is
    either a flat fill or a vertical gradient; both are constant left-to-right,
    so any scanline collapses to one or two colours. Geometry always breaks
    that up. Checking horizontally rather than globally is what makes this
    safe against FreeCAD's default gradient background.
    """
    try:
        from PySide import QtGui
    except Exception:
        return False
    img = QtGui.QImage(path)
    if img.isNull() or img.width() < 4 or img.height() < 4:
        return True
    best = 0
    for frac in (0.25, 0.5, 0.75):
        y = int(img.height() * frac)
        seen = set()
        for x in range(0, img.width(), max(1, img.width() // 400)):
            seen.add(img.pixel(x, y))
            if len(seen) > 8:
                break
        best = max(best, len(seen))
    return best <= 2


def _grab_view_widget(path):
    """Capture the on-screen 3D viewport via Qt.

    Second opinion when saveImage() produces an empty image. This renders
    through the path the user is already looking at, and it includes the
    navigation cube and axis cross -- so if this is blank too, the scene
    really is empty rather than the offscreen render having failed.
    """
    from PySide import QtWidgets
    import FreeCADGui as Gui
    mdi = Gui.getMainWindow().findChild(QtWidgets.QMdiArea)
    sub = mdi.currentSubWindow() if mdi else None
    if sub is None:
        raise RuntimeError("no active 3D subwindow to grab")
    if not sub.grab().save(path):
        raise RuntimeError("Qt grab failed to write " + path)


_SHOT = """
import os
import FreeCADGui as Gui
Gui.updateGui()
_used = 'offscreen'
# The active view may be a TechDraw page or a spreadsheet; use a 3D view.
_v = Gui.ActiveDocument.ActiveView if Gui.ActiveDocument else None
if _v is None or not hasattr(_v, 'saveImage'):
    _views = Gui.ActiveDocument.mdiViewsOfType('Gui::View3DInventor') if Gui.ActiveDocument else []
    if not _views:
        raise RuntimeError('no 3D view to capture')
    _v = _views[0]
if {m!r} in ('auto', 'offscreen'):
    _v.saveImage({p!r}, {w}, {h}, {bg!r})
    if {m!r} == 'auto' and _mcp_looks_blank({p!r}):
        _mcp_grab_view({p!r})
        _used = 'widget'
        if _mcp_looks_blank({p!r}):
            print('WARNING: the 3D view is empty. Nothing is visible to '
                  'capture. In PartDesign the Body Tip feature is what '
                  'renders -- check body.Tip.ViewObject.Visibility, not just '
                  'body.ViewObject.Visibility. An App::Link also renders '
                  'nothing when its source Body Tip is hidden.')
        else:
            print('NOTE: offscreen render came back blank; used a widget grab.')
else:
    _mcp_grab_view({p!r})
    _used = 'widget'
print('screenshot:', {p!r}, 'via', _used, os.path.getsize({p!r}), 'bytes')
"""


def screenshot(path, width=1920, height=1080, background="Current", method="auto"):
    """Save a picture of the active 3D view and report if it came out empty.

    A silently blank screenshot is worse than an error: it reads as success
    and the caller goes on believing it saw the model. This checks the result
    and says so.

    method: "auto" (offscreen, retried as a widget grab when blank),
    "offscreen", or "widget".
    """
    return execute(_SHOT.format(p=path, w=width, h=height,
                                bg=background, m=method))


# ── Server lifecycle ───────────────────────────────────────────────

def start(port=DEFAULT_PORT):
    """Start the RPC server. Returns True on success, False if already running."""
    global _server, _server_thread, _timer, _watchdog

    if _server is not None:
        FreeCAD.Console.PrintWarning(
            "MCP RPC server is already running on port {}\n".format(port)
        )
        return False

    _init_namespace()

    # Use allow_reuse_address so the port can be rebound after
    # unclean shutdown without waiting for TIME_WAIT to expire.
    SimpleXMLRPCServer.allow_reuse_address = True
    try:
        _server = SimpleXMLRPCServer(
            ("127.0.0.1", port),
            allow_none=True,
            logRequests=False,
        )
    except OSError as e:
        FreeCAD.Console.PrintError(
            "MCP RPC server failed to start on port {}: {}\n".format(port, e)
        )
        return False
    # Introspection first, so a client can ask what this server exposes
    # instead of having to know the method names in advance. Without it
    # system.listMethods raises, and a caller has no way to discover that
    # screenshot() exists at all.
    _server.register_introspection_functions()

    _server.register_function(execute, "execute")
    _server.register_function(ping, "ping")
    _server.register_function(screenshot, "screenshot")

    _server_thread = threading.Thread(
        target=_server.serve_forever, daemon=True
    )
    _server_thread.start()

    # QTimer to poll the execution queue on the main thread
    from PySide import QtCore  # noqa: PySide ships with FreeCAD

    _timer = QtCore.QTimer()
    _timer.timeout.connect(_process_queue)
    _timer.start(50)  # 50 ms polling interval

    # Safety net for modal dialogs raised outside a guarded execution.
    _watchdog = QtCore.QTimer()
    _watchdog.timeout.connect(_watchdog_tick)
    _watchdog.start(1000)

    # Learn the Report view's level colours now, so the first captured call
    # does not have to print its own probe lines.
    _calibrate_report_levels()

    FreeCAD.Console.PrintMessage(
        "MCP RPC server started on 127.0.0.1:{}\n".format(port)
    )
    return True


def stop():
    """Stop the RPC server. Returns True on success."""
    global _server, _server_thread, _timer, _watchdog

    if _server is None:
        return False

    if _timer is not None:
        _timer.stop()
        _timer = None

    if _watchdog is not None:
        _watchdog.stop()
        _watchdog = None

    _server.shutdown()
    _server = None
    _server_thread = None

    FreeCAD.Console.PrintMessage("MCP RPC server stopped\n")
    return True


def is_running():
    return _server is not None
