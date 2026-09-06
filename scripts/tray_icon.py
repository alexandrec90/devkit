#!/usr/bin/env python3
"""The tray icon's pixels: a coloured dot with a glyph knocked out of it.

Split out of `tray.py` rather than sitting beside the `ctypes`, because there is a real
seam here and the structural gate found it. Everything in this file is arithmetic over
floats -- no Win32, no handles, nothing that has to be mocked -- so it runs and is tested
on POSIX, while `tray.py` stays what its docstring claims to be: the `ctypes` and the
message loop. `make_icon` is the one line that joins them, and it lives over there with
the rest of the DLL surface.

The output is the BGRA byte buffer `CreateIcon` takes as its XOR bitmap. Two properties
of that buffer are not guesses and are pinned by tests, because both decide whether a
shape comes out right and neither mattered while this drew a solid square:

- **Rows run top-down**, which is the order `CreateIcon` reads device-dependent bitmap
  bits in. Flip them and every glyph is mirrored.
- **Colour is not premultiplied** by the alpha. `DrawIconEx` applies the alpha to the
  colour itself, so premultiplying here applies it twice.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tray_state

# 16x16 is what the notification area asks for at 100% scale, and Windows downsamples a
# larger icon cleanly while it cannot invent detail for a smaller one.
ICON_SIZE = 16

# Samples per pixel per axis, so 64 colour samples decide each pixel and its alpha lands
# on one of 65 levels. The dot and its glyph are round, the icon is 16 pixels across, and
# a hard-edged circle that small is a staircase; this is the whole of the antialiasing.
# The cost is sixteen thousand distance checks per icon, paid three times per session --
# `Tray.icon_for` caches, because an `HICON` per poll is the leak it was written to avoid.
SUPERSAMPLE = 8

# The dot, in the icon's own 16-unit coordinate space: radius, and the multipliers the
# fill is scaled by at the top and bottom row. The gradient is slight on purpose -- it is
# there to stop a flat disc reading as a sticker, not to be seen as a gradient.
DOT_RADIUS = 7.4
TOP_SHADE, BOTTOM_SHADE = 1.24, 0.80

# The glyph knocked out of the dot, per level, as round-capped strokes in the same
# 16-unit space (x right, y down; a zero-length stroke is a dot). This is redundancy with
# the colour rather than decoration: it is the half of the signal that survives a
# colour-blind reader, and it is why the tray can be read without first learning which of
# two similar-brightness colours means which.
GLYPH_WIDTH = 2.2
GLYPHS: dict[str, tuple[tuple[float, float, float, float], ...]] = {
    tray_state.OK: ((4.7, 8.2, 6.9, 10.5), (6.9, 10.5, 11.4, 5.6)),
    tray_state.WARN: ((8.0, 4.2, 8.0, 9.0), (8.0, 11.4, 8.0, 11.4)),
    tray_state.FAIL: ((5.2, 5.2, 10.8, 10.8), (10.8, 5.2, 5.2, 10.8)),
}

# Sample centres within one pixel, as fractions of it. Computed once: it is the same list
# for every pixel of every icon, and it is the innermost loop in the file.
_OFFSETS = tuple((index + 0.5) / SUPERSAMPLE for index in range(SUPERSAMPLE))


def stroke_distance(x: float, y: float, stroke: tuple[float, float, float, float]) -> float:
    """Distance from a point to a line *segment*, which is what gives the glyph round caps
    and joins for free: every point within half a stroke width of the segment is ink.

    To the segment and not to the infinite line it lies on -- that is the whole of it. A
    point past the end of a stroke is measured from the end, so a zero-length stroke is a
    circle, which is how the exclamation mark's dot is drawn.
    """
    ax, ay, bx, by = stroke
    run, rise = bx - ax, by - ay
    span = run * run + rise * rise
    along = 0.0 if span == 0 else max(0.0, min(1.0, ((x - ax) * run + (y - ay) * rise) / span))
    return math.hypot(x - (ax + along * run), y - (ay + along * rise))


def sample_pixel(
    col: int,
    row: int,
    size: int,
    glyph: tuple[tuple[float, float, float, float], ...],
) -> tuple[int, float, int]:
    """How much of one pixel the dot covers, split into fill and glyph.

    Returns the number of covered samples, the sum of the vertical shade over the ones
    that landed on fill, and the count that landed on the glyph. Geometry only: the
    colour never enters here, which is what keeps `icon_pixels` two loops deep instead of
    the four that tripped the structural gate.
    """
    scale = size / ICON_SIZE
    centre = size / 2.0
    radius = DOT_RADIUS * scale
    reach = GLYPH_WIDTH * scale / 2.0

    covered, ink, shade_sum = 0, 0, 0.0
    for offset_y in _OFFSETS:
        y = row + offset_y
        shade = TOP_SHADE + (BOTTOM_SHADE - TOP_SHADE) * (y / size)
        for offset_x in _OFFSETS:
            x = col + offset_x
            if math.hypot(x - centre, y - centre) > radius:
                continue
            covered += 1
            if any(stroke_distance(x, y, stroke) <= reach for stroke in glyph):
                ink += 1
            else:
                shade_sum += shade
    return covered, shade_sum, ink


def pack(red: float, green: float, blue: float, coverage: float) -> bytes:
    """One pixel as the BGRA `CreateIcon` takes: little-endian channel order, `coverage`
    as the alpha, and the colour left **un**-premultiplied.

    Which of the two alpha conventions applies is asserted both ways across the internet,
    so it was measured rather than assumed: hand `CreateIcon` a pixel of full-strength
    green at alpha 0x80, `DrawIconEx` it onto white, and the result is full-strength green
    blended half-and-half -- Windows applied the alpha to the colour itself. Premultiplying
    first applies it twice, which is invisible in the opaque middle of the dot and a ring
    of muddy pixels around its edge.
    """
    return bytes(
        (
            round(max(0.0, min(1.0, blue)) * 255.0),
            round(max(0.0, min(1.0, green)) * 255.0),
            round(max(0.0, min(1.0, red)) * 255.0),
            round(max(0.0, min(1.0, coverage)) * 255.0),
        )
    )


def icon_pixels(
    colour: tuple[int, int, int],
    size: int = ICON_SIZE,
    glyph: tuple[tuple[float, float, float, float], ...] = (),
) -> bytes:
    """An antialiased dot of `colour` carrying `glyph`, as the BGRA rows `CreateIcon` takes.

    A dot rather than the square this drew first: the notification area sits a few pixels
    from Windows' own round icons, and a full-bleed rectangle of colour reads as a
    rendering fault next to them rather than as a status. Everything outside the circle is
    transparent -- the alpha channel is load-bearing here, not decoration, and
    `make_icon`'s all-zero AND mask is what lets it be.
    """
    red, green, blue = (channel / 255.0 for channel in colour)
    samples = float(SUPERSAMPLE * SUPERSAMPLE)

    rows = []
    for row in range(size):
        for col in range(size):
            covered, shade_sum, ink = sample_pixel(col, row, size, glyph)
            if covered == 0:
                rows.append(b"\x00\x00\x00\x00")
                continue
            # Glyph samples are white, so each contributes a full 1.0 to every channel.
            rows.append(
                pack(
                    (red * shade_sum + ink) / covered,
                    (green * shade_sum + ink) / covered,
                    (blue * shade_sum + ink) / covered,
                    covered / samples,
                )
            )
    return b"".join(rows)
