# Usage Guide

## Overview

The TrailCurrent FreeCAD Tools workbench collects the branding, connector, handoff and MCP
tools listed in the [README](../README.md). This guide walks through the two
branding tools in detail:

1. **Deboss TrailCurrent Logo** -- cuts the brand logo into a flat surface as a multi-level relief
2. **QR Code Emboss** -- applies a scannable QR code to a flat surface (embossed or debossed)

Both tools produce new solid objects with all parameters stored for re-editing.

---

## Logo Deboss

### Step-by-Step

#### 1. Open your project

Open the FreeCAD file containing the part you want to brand. This works with any solid body that has at least one flat face -- case lids, enclosure panels, flat plates, etc.

#### 2. Switch to the TrailCurrent FreeCAD Tools workbench

Use the workbench dropdown in the main toolbar and select **TrailCurrent FreeCAD Tools**.

#### 3. Select a flat face

In the 3D view, click on the flat face where you want the logo placed. The face must be:

- **Planar** -- flat surfaces only, not curved or cylindrical
- **Large enough** -- wider than your chosen logo diameter

The status bar at the bottom will show something like `Face6` to confirm the selection.

#### 4. Click the toolbar button

Click **Deboss TrailCurrent Logo** in the toolbar (or **TrailCurrent > Deboss TrailCurrent Logo** in the menu bar). A settings panel opens in the left sidebar.

#### 5. Adjust the settings

See the [Settings Reference](settings.md) for detailed explanations. In most cases you only need to adjust:

- **Logo Diameter** -- how large the logo is on the face
- **Total Depth** -- how deep the cut goes (keep this less than your wall thickness)
- **X/Y Offset** -- shift the logo relative to the face center

The three depth ratio sliders control how the four logo layers stack. The defaults produce a good result for most cases.

#### 6. Click OK

Press **OK** to run the deboss. A new **LogoDeboss** object appears in the model tree. The original body is hidden but not deleted.

### Logo Tips

**Choosing the right diameter:**
- Small enclosures (credit card to smartphone size): **12-18 mm**
- Medium cases (Raspberry Pi to router size): **18-25 mm**
- Large panels or covers: **25-40 mm**

**Choosing the right depth:**
- Measure your wall thickness first -- the total depth must be less than the wall thickness
- 3D printing with standard 1.2 mm walls: **0.6-0.8 mm**
- Thicker walls (2+ mm): **1.0-1.5 mm** for more dramatic effect
- Thin lids (under 1 mm): **0.3-0.5 mm**

---

## QR Code Emboss / Deboss

### Step-by-Step

#### 1. Select a flat face

Same as the logo tool -- click a flat (planar) face on your solid body.

#### 2. Click QR Code Emboss

Click the QR code icon in the toolbar (or **TrailCurrent > QR Code Emboss** in the menu bar). The QR code settings panel opens in the left sidebar.

#### 3. Enter the URL

Type or paste the URL you want the QR code to link to. The info line at the bottom updates in real time showing the QR version, module count, and module size.

**Keep URLs short.** Shorter URLs produce fewer modules and scan more reliably:
- `https://trailcurrent.com` (22 chars) -- Version 2, 29x29 modules
- `https://trailcurrent.com/docs/product-guide` (45 chars) -- Version 4, 33x33 modules

Consider using a URL shortener for long links.

#### 4. Choose emboss or deboss

- **Emboss (raised)** -- QR modules protrude from the surface. Best when you can paint or fill the raised area with a contrasting color.
- **Deboss (recessed)** -- QR modules are cut into the surface. Works well for 3D printing where the recessed areas create natural shadows.

#### 5. Adjust size and height

- **QR Size** -- the side length of the square QR code (5-200 mm). Larger = easier to scan.
- **Height / Depth** -- how far the modules protrude (emboss) or are recessed (deboss). 0.3-0.8 mm works well for most 3D prints.

#### 6. Position with offsets

Use **X Offset** and **Y Offset** to move the QR code away from the face center. This is useful when placing both a logo and a QR code on the same face.

#### 7. Click OK

Press **OK** to apply. A new **QRCodeEmboss** (or **QRCodeDeboss**) object appears in the model tree.

### QR Code Tips

**Scannability guidelines:**
- The info label shows module size in mm. Aim for **0.3 mm or larger** per module (shown in green). Below 0.3 mm (shown in red) the code may be hard to scan.
- To increase module size: increase QR Size, decrease URL length, or use a lower error correction level.
- For the best scan reliability, ensure good contrast between the QR modules and the surrounding surface when the part is printed.

**Error correction levels:**

| Level | Recovery | Best for |
|-------|----------|----------|
| L (7%) | Low | Maximum data capacity, clean environments |
| M (15%) | Medium | General purpose (default) |
| Q (25%) | High | Parts that may get scratched or dirty |
| H (30%) | Highest | Harsh environments, maximum robustness |

Higher error correction = more modules = larger QR code at the same size, so each module is smaller.

**Border (quiet zone):**
- The QR standard requires 4 modules of quiet zone. The default of 2 works for most phone scanners.
- If scanning is unreliable, try increasing the border to 3 or 4.

---

## Re-editing

Both tools support re-editing after the initial creation. All parameters (size, depth, URL, offsets, etc.) are stored on the result object.

### Double-click to re-edit

**Double-click** the LogoDeboss or QRCodeEmboss/QRCodeDeboss object in the model tree. The settings panel reopens with all parameters pre-filled from the previous values. Adjust and click **OK** to regenerate.

### Toolbar button re-edit

Alternatively, select the result object in the model tree (single click), then click the corresponding toolbar button. The panel opens in re-edit mode.

### What happens on re-edit

When you click OK after re-editing:
1. The old result object is deleted
2. A new result is created with the updated parameters
3. The original body remains hidden

### Stored properties

You can view the stored parameters in FreeCAD's Properties panel (View > Panels > Properties) when the result object is selected. These are read-only references -- to change them, use the re-edit workflow described above.

---

## Working with Results

- Both result objects are standalone solids. You can export them to STL, use them as input for further operations, or continue modeling on them.
- The original body is hidden, not deleted. Toggle its visibility in the model tree to bring it back.
- To undo, delete the result object and unhide the original body.
- To apply multiple features (e.g., a logo and a QR code on the same face), apply the first one, then select a face on the result and apply the second.

## Exporting for 3D Printing

1. Select the result object in the model tree
2. Go to **File > Export** (or **Part > Export CAD**)
3. Choose **STL Mesh (.stl)** as the format
4. Save the file

The multi-level logo relief and QR code modules should be clearly visible in your slicer's preview.

## Limitations

- **Flat faces only.** Both tools can only be applied to planar surfaces. A warning is shown if you select a non-planar face.
- **One operation at a time.** To apply multiple logos or QR codes, chain the operations: apply the first, then select a face on the result for the second.
- **Boolean complexity.** Very complex bodies with many features may cause boolean operations to be slow or fail. If this happens, try simplifying the body or using a slightly smaller size.
