#!/bin/bash
# Render the Tuomin app icon from packaging/icon/tuomin.svg into an .icns bundle
# icon plus the menu-bar template images. macOS-only (qlmanage, sips, iconutil).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ICON_DIR="$REPO_ROOT/packaging/icon"
SVG="$ICON_DIR/tuomin.svg"
WORK="$(mktemp -d -t tuomin-icon)"
trap 'rm -rf "$WORK"' EXIT

# Renderer: AppKit via the repo's PyObjC venv (qlmanage flattens SVG onto an
# opaque background, which breaks the Dock icon corners and the menu-bar glyph).
PY="$REPO_ROOT/.venv/bin/python"
RENDER="$PY $SCRIPT_DIR/render_icon.py"

# 1. iconset at every size macOS expects (each rendered from the SVG, not resampled)
ICONSET="$WORK/Tuomin.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  $RENDER "$SVG" "$ICONSET/icon_${size}x${size}.png" "$size"
  $RENDER "$SVG" "$ICONSET/icon_${size}x${size}@2x.png" "$((size * 2))"
done
iconutil -c icns "$ICONSET" -o "$ICON_DIR/Tuomin.icns"

# 2. 1024 px PNG for the Dock in source runs and for docs
$RENDER "$SVG" "$ICON_DIR/tuomin-1024.png" 1024

# 3. menu-bar template glyph (monochrome with alpha, 18 pt and @2x)
$RENDER "$ICON_DIR/menubar.svg" "$ICON_DIR/menubar.png" 18
$RENDER "$ICON_DIR/menubar.svg" "$ICON_DIR/menubar@2x.png" 36

echo "wrote $ICON_DIR/Tuomin.icns and tuomin-1024.png"
