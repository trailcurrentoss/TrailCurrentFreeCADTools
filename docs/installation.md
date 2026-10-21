# Installation Guide

## Option 1: FreeCAD Addon Manager (Recommended)

The Addon Manager is FreeCAD's built-in package manager. This is the easiest way to install.

### Adding the repository

1. Open FreeCAD
2. Go to **Edit > Preferences**
3. Select **Addon Manager** in the left sidebar (under Python)
4. Under **Custom repositories**, click the **+** (green plus) button
5. Enter the Repository URL:
   ```
   https://github.com/trailcurrentoss/TrailCurrentFreeCADTools
   ```
6. Enter the Branch name: `main`
7. Click **OK** to close Preferences

### Installing

1. Open **Tools > Addon Manager**
2. Search for **TrailCurrent FreeCAD Tools** (or scroll to the T section alphabetically)
3. Click the entry to view details
4. Click **Install**
5. Restart FreeCAD when prompted

### Updating

1. Open **Tools > Addon Manager**
2. Click **Check for updates** (top of the window)
3. If an update is available for TrailCurrent FreeCAD Tools, select it and click **Update**
4. Restart FreeCAD

## Option 2: Manual Install (Linux)

If you cloned or downloaded the repository yourself, an install script is provided for Linux systems.

### Clone and install

```bash
git clone https://github.com/trailcurrentoss/TrailCurrentFreeCADTools.git
cd TrailCurrentFreeCADTools
chmod +x install.sh
./install.sh
```

The script automatically detects your FreeCAD installation:

| FreeCAD Install | Mod Directory | Install Method |
|-----------------|---------------|----------------|
| Snap package | `~/snap/freecad/common/Mod/` | Copies files (snap sandbox blocks symlinks) |
| Native / AppImage | `~/.local/share/FreeCAD/Mod/` | Creates a symlink |
| Legacy native | `~/.FreeCAD/Mod/` | Creates a symlink |

For **snap installs**, you need to re-run `./install.sh` after pulling updates, because the files are copied rather than symlinked.

For **native installs**, the symlink means changes to the cloned repo take effect immediately (just restart FreeCAD).

### Manual copy (any OS)

If the install script doesn't cover your setup, you can copy the plugin manually:

1. Find your FreeCAD Mod directory:
   - Open FreeCAD
   - Go to **Edit > Preferences > General**
   - Note the **User configuration** path
   - Your Mod directory is at that same level, e.g. `~/.local/share/FreeCAD/Mod/`
   - Or type this in the FreeCAD Python console:
     ```python
     FreeCAD.getUserAppDataDir() + "Mod"
     ```

2. Copy the entire plugin folder into that Mod directory:
   ```bash
   cp -r TrailCurrentFreeCADTools /path/to/FreeCAD/Mod/
   ```

3. Restart FreeCAD

## Dependencies

The `qrcode` Python package is **bundled** inside the plugin directory (`qrcode/`). No separate `pip install` is needed. This avoids compatibility issues with FreeCAD's sandboxed Python environment (especially snap installs).

## Verifying the Installation

After restarting FreeCAD:

1. Look for **TrailCurrent FreeCAD Tools** in the workbench dropdown (the dropdown selector in the main toolbar, usually set to "Start" or "Part Design" by default)
2. If you see it listed, the installation was successful
3. Switch to it and you should see the toolbar: **Deboss Logo**, **Deboss Logo + Text**, **QR Code Emboss**, **Place Deutsch Housing**, **Send to Blender**, **Send to Bambu Studio** and **MCP Server**

### Troubleshooting

**Workbench doesn't appear in the dropdown:**
- Make sure you restarted FreeCAD after installing
- Check the FreeCAD Python console (View > Panels > Python console) for error messages mentioning "TrailCurrent"
- Verify the files are in the correct Mod directory by checking that `InitGui.py` exists at the top level of the plugin folder inside Mod

**"No module named logo_command" or "No module named qr_command" error:**
- The plugin directory may not be on Python's search path. This is usually handled automatically, but if you see this error, check that all `.py` files are in the same directory as `InitGui.py`

**"qrcode package not installed" warning in the QR panel:**
- The bundled `qrcode/` directory may be missing. Re-clone or re-download the repository and reinstall.

## Uninstalling

**Addon Manager installs:** Open **Tools > Addon Manager**, find TrailCurrent FreeCAD Tools, and click **Uninstall**.

**Manual installs:** Delete the plugin folder from your FreeCAD Mod directory and restart FreeCAD.
