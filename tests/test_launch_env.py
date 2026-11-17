"""Checks for launch_env.child_env(), run with plain Python (no FreeCAD needed).

    python3 tests/test_launch_env.py

The fixture mirrors the environment a FreeCAD AppImage hands to its children:
AppImage runtime variables, AppRun exports pointing into the mount, a focus
token from the desktop, and search paths mixing mount and system entries.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from launch_env import child_env, popen_kwargs  # noqa: E402

MOUNT = "/tmp/.mount_FreeCAexample"

APPIMAGE_ENV = {
    "HOME": "/home/user",
    "DISPLAY": ":0",
    "LANG": "en_US.UTF-8",
    "APPDIR": MOUNT,
    "APPIMAGE": "/home/user/Applications/FreeCAD.AppImage",
    "ARGV0": "/home/user/Applications/FreeCAD.AppImage",
    "OWD": "/home/user",
    "PREFIX": MOUNT + "/usr",
    "PYTHONHOME": MOUNT + "/usr",
    "PATH_TO_FREECAD_LIBDIR": MOUNT + "/usr/lib",
    "SSL_CERT_FILE": MOUNT + "/usr/ssl/cacert.pem",
    "GIT_SSL_CAINFO": MOUNT + "/usr/ssl/cacert.pem",
    "QT_QPA_PLATFORM": "xcb",
    "FONTCONFIG_FILE": "/etc/fonts/fonts.conf",
    "XDG_ACTIVATION_TOKEN": "gnome-shell/FreeCAD/1234-0-host_TIME0",
    "DESKTOP_STARTUP_ID": "gnome-shell/FreeCAD/1234-0-host_TIME0",
    "_": MOUNT + "/usr/bin/freecad",
    "PATH": MOUNT + "/usr/bin:/usr/local/bin:/usr/bin:/bin",
    "LD_LIBRARY_PATH": MOUNT + "/usr/lib",
    # A path that merely starts with the mount's name is NOT inside it.
    "XDG_DATA_DIRS": MOUNT + "/usr/share:" + MOUNT + "x/share:/usr/share",
}

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def test_appimage_env_is_cleaned():
    before = dict(APPIMAGE_ENV)
    env = child_env(APPIMAGE_ENV)
    check(APPIMAGE_ENV == before, "input environment was modified")

    for key in ("APPDIR", "APPIMAGE", "ARGV0", "OWD", "PREFIX", "PYTHONHOME",
                "PATH_TO_FREECAD_LIBDIR", "SSL_CERT_FILE", "GIT_SSL_CAINFO",
                "QT_QPA_PLATFORM", "XDG_ACTIVATION_TOKEN",
                "DESKTOP_STARTUP_ID", "_", "LD_LIBRARY_PATH"):
        check(key not in env, "%s should be removed, got %r" % (key, env.get(key)))

    check(env.get("PATH") == "/usr/local/bin:/usr/bin:/bin",
          "PATH should keep only system entries, got %r" % env.get("PATH"))
    check(env.get("XDG_DATA_DIRS") == MOUNT + "x/share:/usr/share",
          "XDG_DATA_DIRS prefix match is wrong: %r" % env.get("XDG_DATA_DIRS"))

    for key in ("HOME", "DISPLAY", "LANG", "FONTCONFIG_FILE"):
        check(env.get(key) == APPIMAGE_ENV[key], "%s should be kept" % key)

    leaked = [k for k, v in env.items() if v == MOUNT or v.startswith(MOUNT + "/")]
    check(not leaked, "values still inside the mount: %s" % leaked)


def test_plain_env_only_loses_focus_token():
    plain = {
        "HOME": "/home/user",
        "PATH": "/usr/bin:/bin",
        "PYTHONHOME": "/opt/python",          # user's own; not ours to remove
        "SSL_CERT_FILE": "/etc/ssl/certs/ca.pem",
        "XDG_ACTIVATION_TOKEN": "token",
    }
    env = child_env(plain)
    expected = dict(plain)
    del expected["XDG_ACTIVATION_TOKEN"]
    check(env == expected, "non-AppImage env changed unexpectedly: %r" % env)


def test_popen_kwargs():
    kw = popen_kwargs()
    check("env" in kw and isinstance(kw["env"], dict), "popen_kwargs lacks env")
    if os.name == "posix":
        check(kw.get("start_new_session") is True,
              "launched apps should get their own session on POSIX")


if __name__ == "__main__":
    test_appimage_env_is_cleaned()
    test_plain_env_only_loses_focus_token()
    test_popen_kwargs()
    if failures:
        print("FAIL (%d)" % len(failures))
        for f in failures:
            print("  - " + f)
        sys.exit(1)
    print("OK: launch_env checks passed")
