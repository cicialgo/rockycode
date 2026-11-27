# RockyCode macOS app icon

The mark comes directly from [`brand/rockycode-note.svg`](../../../brand/rockycode-note.svg).
The canonical brand file is unchanged. Its 26 rectangle geometries are filled as
one compound path to remove antialiasing seams between adjacent blocks. No
AI-generated drawing, raster tracing, stroke expansion, or new note geometry.

- Solid white mark on a soft purple gradient with a restrained inner edge highlight.
- 1024 × 1024 RGBA canvas with transparent outside corners.
- Background tile: 896 × 896, centered with 64 px outer margins.
- Mark: 768 px wide (75% of canvas / 85.7% of tile); the previous marketplace
  mark occupied approximately 53% of its canvas. The dot is preserved.
- Every unique size is rendered directly from the SVG, never upscaled from PNG.
- Small sizes necessarily antialias the original pixel geometry; no hand-redrawn
  small-size substitute or pixel-snapping distortion is applied.

`RockyCode.iconset` follows Apple's [iconset naming and resolution contract](https://developer.apple.com/library/archive/documentation/GraphicsAnimation/Conceptual/HighResolutionOSX/Optimizing/Optimizing.html):

| File | Pixel dimensions |
| --- | --- |
| icon_16x16.png | 16 × 16 |
| icon_16x16@2x.png | 32 × 32 |
| icon_32x32.png | 32 × 32 |
| icon_32x32@2x.png | 64 × 64 |
| icon_128x128.png | 128 × 128 |
| icon_128x128@2x.png | 256 × 256 |
| icon_256x256.png | 256 × 256 |
| icon_256x256@2x.png | 512 × 512 |
| icon_512x512.png | 512 × 512 |
| icon_512x512@2x.png | 1024 × 1024 |

## Regenerate and verify

From the repository root, with Python 3.11+ and Inkscape 1.4 installed:

```sh
python3 apps/desktop/scripts/generate-macos-icons.py
python3 apps/desktop/scripts/test-macos-icons.py
python3 apps/desktop/scripts/generate-macos-icons.py --check
```

The generator uses only Python's standard library and the explicitly installed
Inkscape executable. It does not install dependencies, execute network calls, or
modify the brand source. Tested renderer: Inkscape 1.4 (e7c3feb100, 2024-10-09).
Other renderers/versions may produce different PNG bytes; inspect the result
before updating committed assets. `--inkscape /path/to/inkscape` supports macOS
installations whose CLI is not on PATH.

## Integration boundary

The soft-gradient visual direction was approved on 2026-10-19. The desktop project
currently has no release packager; no new packaging dependency or release workflow
is introduced. On macOS, the development client applies the approved PNG to its
Dock icon at startup, including when an existing Electron binary still has its
stock icon. This does not rewrite the binary or its bundle. The in-window brand
mark continues to use the canonical SVG. The explicit Electron setup script also
assembles the iconset into the development bundle; a future packaging step can
consume an ICNS generated on a Mac:

```sh
iconutil -c icns apps/desktop/assets/RockyCode.iconset -o apps/desktop/assets/RockyCode.icns
```

The original Linux cloud asset checks did not verify `iconutil`, Finder/Dock
display, or signed app-bundle integration. The PNGs are not a signed installer
or a claim of Icon Composer / Liquid Glass compatibility.

## Selected appearance

The default master and iconset use the approved soft-gradient treatment:
`#a38ade` → `#8867cc` → `#7351b9`, plus an 8/1024 px inner edge highlight
fading from 42% white to transparent. The highlight stays inside the original tile
boundary. There is no blur, glow, mark translucency, drop shadow, or glass filter.
This is a static raster/vector treatment, not native Liquid Glass.

The earlier flat proposal remains in Git history (commit `ea830e2`) rather than
shipping two competing defaults. For a fresh comparison, the generator's optional
`--variant flat` writes a separate `assets/flat/` directory; this is not used by the
canonical iconset. Both styles preserve exactly the same mark geometry and scale.

Visual approval does not replace Finder/Dock and packaging acceptance on a Mac.
