#!/usr/bin/env python3
"""Render an SVG to a transparent PNG at an exact pixel size using AppKit.

Quick Look (`qlmanage`) flattens SVG thumbnails onto an opaque background,
which produced a boxed Dock icon and a solid square in the menu bar. AppKit's
SVG loader keeps the alpha channel. Usage:

    .venv/bin/python scripts/render_icon.py in.svg out.png 1024
"""
from __future__ import annotations

import sys


def render(svg_path: str, png_path: str, size: int) -> None:
    from AppKit import (
        NSBitmapImageRep, NSCalibratedRGBColorSpace, NSColor, NSCompositingOperationCopy,
        NSCompositingOperationSourceOver, NSGraphicsContext, NSImage, NSMakeRect,
        NSPNGFileType, NSRectFillUsingOperation,
    )

    image = NSImage.alloc().initWithContentsOfFile_(svg_path)
    if image is None:
        raise SystemExit(f"cannot load {svg_path}")
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0
    )
    rep.setSize_((size, size))
    NSGraphicsContext.saveGraphicsState()
    try:
        NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
        rect = NSMakeRect(0, 0, size, size)
        NSColor.clearColor().set()
        NSRectFillUsingOperation(rect, NSCompositingOperationCopy)
        image.drawInRect_fromRect_operation_fraction_(rect, NSMakeRect(0, 0, 0, 0), NSCompositingOperationSourceOver, 1.0)
    finally:
        NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(NSPNGFileType, None)
    if not data.writeToFile_atomically_(png_path, True):
        raise SystemExit(f"cannot write {png_path}")
    corner = rep.colorAtX_y_(1, 1)
    print(f"{png_path}: {size}px, corner alpha {corner.alphaComponent():.2f}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    render(sys.argv[1], sys.argv[2], int(sys.argv[3]))
