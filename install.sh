#!/bin/bash
# Install the TrailCurrent FreeCAD Tools workbench into FreeCAD.
#
# Usage (local development):
#   ./install.sh
#
# For snap installs: copies files (snap sandbox can't follow symlinks to
# external storage).  For native installs: creates a symlink.
#
# Re-run this script after making changes to sync updates (snap only;
# native symlinks pick up changes automatically).
#
# Addon Manager users: You don't need this script.  Install directly
# from FreeCAD → Tools → Addon Manager → search "TrailCurrent FreeCAD Tools".

set -e

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_NAME="$(basename "$PLUGIN_DIR")"

# Detect FreeCAD Mod directory.
#
# FreeCAD 1.0 and later keep per-version user data, so the plugin path is
# ~/.local/share/FreeCAD/v1-1/Mod rather than the older unversioned
# ~/.local/share/FreeCAD/Mod. Checking only the unversioned path silently
# misses a 1.x install: the script either errors out or writes to a directory
# the running FreeCAD never reads, and the installed copy then drifts behind
# the repository without anyone noticing. Prefer the newest versioned
# directory, sorted by version so v1-10 beats v1-9.
MOD_DIR=""
IS_SNAP=0

if [ -d "$HOME/snap/freecad/common/Mod" ]; then
    MOD_DIR="$HOME/snap/freecad/common/Mod"
    IS_SNAP=1
else
    VERSIONED="$(ls -d "$HOME"/.local/share/FreeCAD/v*/ 2>/dev/null | sort -V | tail -1)"
    if [ -n "$VERSIONED" ]; then
        MOD_DIR="${VERSIONED}Mod"
        mkdir -p "$MOD_DIR"
    elif [ -d "$HOME/.local/share/FreeCAD/Mod" ]; then
        MOD_DIR="$HOME/.local/share/FreeCAD/Mod"
    elif [ -d "$HOME/.FreeCAD/Mod" ]; then
        MOD_DIR="$HOME/.FreeCAD/Mod"
    fi
fi

if [ -z "$MOD_DIR" ]; then
    echo "ERROR: Could not find FreeCAD Mod directory."
    echo "Searched:"
    echo "  $HOME/snap/freecad/common/Mod"
    echo "  $HOME/.local/share/FreeCAD/v*/Mod   (FreeCAD 1.0+)"
    echo "  $HOME/.local/share/FreeCAD/Mod      (older releases)"
    echo "  $HOME/.FreeCAD/Mod"
    exit 1
fi

echo "FreeCAD Mod directory: $MOD_DIR"

DEST="$MOD_DIR/$PLUGIN_NAME"

# The workbench used to ship under a different folder name. A leftover copy
# would register a second workbench with the old class name, so clear it.
for OLD in "$MOD_DIR/Utilities-LogoFreeCADPlugin"; do
    if [ -L "$OLD" ] || [ -d "$OLD" ]; then
        rm -rf "$OLD"
        echo "Removed previous install: $OLD"
    fi
done

if [ "$IS_SNAP" = "1" ]; then
    # Snap: copy files (sandbox blocks symlinks to external storage)
    echo "Snap FreeCAD detected — copying files."

    # Remove old install (symlink or directory)
    if [ -L "$DEST" ]; then
        rm "$DEST"
    elif [ -d "$DEST" ]; then
        rm -rf "$DEST"
    fi

    cp -r "$PLUGIN_DIR" "$DEST"
    # Remove development artifacts from the copy
    rm -rf "$DEST/.git"
    rm -rf "$DEST/.claude"
    rm -rf "$DEST/__pycache__"
    find "$DEST" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    find "$DEST" -name "*.pyc" -delete 2>/dev/null || true
    echo "Copied: $PLUGIN_DIR -> $DEST"
else
    # Native install: symlink
    if [ -L "$DEST" ]; then
        rm "$DEST"
    elif [ -d "$DEST" ]; then
        rm -rf "$DEST"
    fi

    ln -sf "$PLUGIN_DIR" "$DEST"
    echo "Linked: $DEST -> $PLUGIN_DIR"
fi

echo ""
echo "Restart FreeCAD to load the workbench."
echo "Look for 'TrailCurrent FreeCAD Tools' in the workbench dropdown."
echo ""
echo "Available tools:"
echo "  - Deboss TrailCurrent Logo        (multi-level relief logo, with or without text)"
echo "  - QR Code Emboss                  (scannable QR codes)"
echo "  - Place DTM2P / DTM4P / DTM12P    (Deutsch connector housings)"
echo "  - Send to Blender / Bambu Studio  (render or slice the selected body)"
echo "  - MCP Server                      (drive FreeCAD from an AI agent)"
