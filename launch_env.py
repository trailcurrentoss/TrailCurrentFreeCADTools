"""Environment for external applications launched from FreeCAD.

FreeCAD's own launcher configures the process for FreeCAD, and a plain
``subprocess.Popen`` hands all of it to whatever is started next:

* The AppImage runtime sets ``APPDIR``, ``APPIMAGE``, ``ARGV0`` and ``OWD``,
  and FreeCAD's ``AppRun`` adds ``PYTHONHOME``, ``SSL_CERT_FILE``,
  ``GIT_SSL_CAINFO`` and others that point inside FreeCAD's read-only mount.
  A launched app then loads FreeCAD's CA bundle and Python, and when FreeCAD
  exits the mount disappears from under it. Another AppImage (Bambu Studio)
  also reads ``APPIMAGE``/``APPDIR`` and believes it is FreeCAD.
* The desktop gives FreeCAD a one-shot focus token (``XDG_ACTIVATION_TOKEN``
  / ``DESKTOP_STARTUP_ID``). Passed on, it lets the launched app take
  keyboard focus away from whatever window the user is typing in.

``child_env()`` returns a copy of the environment with all of that removed,
so an application starts exactly as it would from the desktop.
"""

import os

# Set by the AppImage runtime or by FreeCAD's AppRun, meaningless to any
# other program.
_APPIMAGE_VARS = (
    "APPDIR", "APPIMAGE", "ARGV0", "OWD",
    "PYTHONHOME", "PATH_TO_FREECAD_LIBDIR", "PREFIX",
    "QT_QPA_PLATFORM",
)

# Single-use desktop activation tokens issued to FreeCAD itself.
_ACTIVATION_VARS = ("XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID")

# Colon-separated search paths: drop only the entries inside the mount.
_PATH_LIST_VARS = (
    "PATH", "LD_LIBRARY_PATH", "PYTHONPATH", "XDG_DATA_DIRS",
    "QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "GSETTINGS_SCHEMA_DIR",
    "GST_PLUGIN_SYSTEM_PATH", "GST_PLUGIN_PATH", "PERLLIB",
)


def _inside(path, root):
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def child_env(base=None):
    """Return an environment dict safe to give an external application.

    ``base`` defaults to ``os.environ``; it is never modified.
    """
    env = dict(os.environ if base is None else base)
    appdir = env.get("APPDIR")

    for key in _ACTIVATION_VARS:
        env.pop(key, None)

    if not appdir:
        return env  # not running from an AppImage; nothing else to undo

    for key in _APPIMAGE_VARS:
        env.pop(key, None)

    for key in _PATH_LIST_VARS:
        if key not in env:
            continue
        kept = [p for p in env[key].split(os.pathsep)
                if p and not _inside(p, appdir)]
        if kept:
            env[key] = os.pathsep.join(kept)
        else:
            del env[key]

    # Anything else still pointing into the mount (SSL_CERT_FILE,
    # GIT_SSL_CAINFO, ``_``, ...) belongs to FreeCAD.
    for key in [k for k, v in env.items() if _inside(v, appdir)]:
        del env[key]

    return env


def popen_kwargs():
    """Keyword arguments for ``subprocess.Popen`` of a desktop application.

    The app gets a clean environment and its own session, so it is not tied
    to FreeCAD's process group and keeps running after FreeCAD exits.
    """
    kwargs = {"env": child_env()}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    return kwargs
