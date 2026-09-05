"""`tray_icon.py`: the dot, its glyph, and the byte layout `CreateIcon` reads.

Every assertion here is arithmetic over a buffer, so this file runs everywhere -- which
is the point of the module existing apart from `tray.py`. The two facts it pins hardest
are the ones that were measured against a real `CreateIcon`/`DrawIconEx` round trip and
cannot be re-measured from a suite: rows run top-down, and the colour is not
premultiplied by the alpha. Both are invisible until a shape is drawn, and both were
wrong at some point while this change was being written.
"""

from __future__ import annotations

import pytest
from support import load_script

tray_icon = load_script("scripts/tray_icon.py")
tray_state = load_script("scripts/tray_state.py")


def pixel(buffer: bytes, x: int, y: int, size: int = 16) -> tuple[int, int, int, int]:
    """One BGRA pixel out of a buffer, addressed the way the icon is drawn: x right, y
    down from the top-left."""
    offset = (y * size + x) * 4
    blue, green, red, alpha = buffer[offset : offset + 4]
    return blue, green, red, alpha


def test_the_pixel_buffer_is_the_size_createicon_expects():
    """32 bits per pixel over a 16x16 square. A buffer of the wrong length is read past
    its end by a C function that was told how big it is by the width and height."""
    pixels = tray_icon.icon_pixels((1, 2, 3), size=16)
    assert len(pixels) == 16 * 16 * 4


def test_the_pixels_are_bgra_not_rgba():
    """Windows DIBs are little-endian BGRA. Getting this backwards swaps red and blue,
    which for this icon means a failure shows up as the healthy colour."""
    assert tray_icon.pack(0.8, 0.4, 0.1, 1.0) == bytes((0x1A, 0x66, 0xCC, 0xFF))


def test_the_centre_of_the_dot_is_the_state_colour_in_bgra_order():
    """The same check through the function that actually feeds `CreateIcon`, because
    `_pack` could be right while its callers hand it the channels in the wrong order."""
    blue, green, red, alpha = pixel(tray_icon.icon_pixels((0xC6, 0x28, 0x28)), 8, 8)
    assert alpha == 0xFF
    assert red > 0x80 > green
    assert blue == green


def test_colour_is_not_premultiplied_by_the_alpha():
    """Measured, not assumed: `DrawIconEx` applies the alpha to the colour itself, so a
    buffer that has already applied it renders a ring of muddy pixels around the dot."""
    assert tray_icon.pack(1.0, 1.0, 1.0, 0.5) == bytes((0xFF, 0xFF, 0xFF, 0x80))


def test_the_corners_are_transparent_and_the_middle_is_not():
    """The dot is a dot because of the alpha channel -- there is no other mechanism
    shaping it, and an opaque corner is the square this replaced."""
    pixels = tray_icon.icon_pixels((10, 20, 30))
    assert pixel(pixels, 0, 0)[3] == 0
    assert pixel(pixels, 15, 15)[3] == 0
    assert pixel(pixels, 8, 8)[3] == 0xFF


def test_the_edge_of_the_dot_is_antialiased():
    """Some pixel on the rim has to be partly covered. All-or-nothing alpha is a circle
    drawn as a staircase, which at 16 pixels is what the supersampling is for."""
    pixels = tray_icon.icon_pixels((10, 20, 30))
    alphas = {pixel(pixels, x, y)[3] for y in range(16) for x in range(16)}
    assert any(0 < alpha < 0xFF for alpha in alphas)


def test_the_glyph_is_drawn_in_white_over_the_colour():
    """The knocked-out mark is the half of the signal a colour-blind reader gets. If it
    came out in the state colour it would be invisible, which is the failure to catch."""
    plain = tray_icon.icon_pixels(tray_state.COLOURS[tray_state.FAIL])
    marked = tray_icon.icon_pixels(
        tray_state.COLOURS[tray_state.FAIL], glyph=tray_icon.GLYPHS[tray_state.FAIL]
    )
    assert plain != marked
    assert pixel(marked, 8, 8)[:3] == (0xFF, 0xFF, 0xFF)


def test_every_level_has_its_own_glyph():
    """A level `COLOURS` knows and `GLYPHS` does not is a bare dot in the tray -- legal,
    silent, and reporting less than the other two."""
    assert set(tray_icon.GLYPHS) == set(tray_state.COLOURS)
    assert len(set(tray_icon.GLYPHS.values())) == len(tray_icon.GLYPHS)


def test_the_rows_run_top_down_so_the_check_mark_points_the_right_way():
    """`CreateIcon` reads its bits top scan line first. Flip them and every glyph is
    mirrored: the check mark's long arm would rise to the left. Verified against a real
    `DrawIconEx` once; this is the assertion that keeps it that way.
    """
    marked = tray_icon.icon_pixels(
        tray_state.COLOURS[tray_state.OK], glyph=tray_icon.GLYPHS[tray_state.OK]
    )
    white = {
        (x, y)
        for y in range(16)
        for x in range(16)
        if pixel(marked, x, y)[:3] == (0xFF, 0xFF, 0xFF)
    }
    lowest_left = max(y for x, y in white if x < 8)
    lowest_right = max(y for x, y in white if x > 8)
    assert lowest_left > lowest_right


def test_stroke_distance_measures_to_the_segment_not_the_infinite_line():
    """Round caps and the "!" dot both come out of this: a zero-length stroke is a
    circle, and a point past the end of a stroke is measured from the end, not from the
    line it lies on."""
    assert tray_icon.stroke_distance(0.0, 0.0, (0.0, 0.0, 0.0, 0.0)) == 0.0
    assert tray_icon.stroke_distance(4.0, 0.0, (0.0, 0.0, 1.0, 0.0)) == pytest.approx(3.0)
    assert tray_icon.stroke_distance(0.5, 2.0, (0.0, 0.0, 1.0, 0.0)) == pytest.approx(2.0)


def test_each_state_yields_a_distinct_buffer():
    buffers = {
        level: tray_icon.icon_pixels(colour, glyph=tray_icon.GLYPHS[level])
        for level, colour in tray_state.COLOURS.items()
    }
    assert len(set(buffers.values())) == len(tray_state.COLOURS)


def test_the_sampler_reports_fill_and_glyph_separately():
    """`icon_pixels` stays two loops deep by asking this for the geometry and doing the
    colour itself, so the split it returns is the seam and worth its own assertion."""
    covered, shade_sum, ink = tray_icon.sample_pixel(0, 0, 16, ())
    assert (covered, shade_sum, ink) == (0, 0.0, 0)

    covered, shade_sum, ink = tray_icon.sample_pixel(8, 8, 16, ())
    assert covered == tray_icon.SUPERSAMPLE**2
    assert ink == 0
    assert shade_sum > 0

    covered, shade_sum, ink = tray_icon.sample_pixel(8, 8, 16, ((0.0, 8.0, 16.0, 8.0),))
    assert ink == covered
    assert shade_sum == 0.0


def test_the_dot_is_shaded_from_top_to_bottom():
    """The gradient is the difference between a dot and a sticker. Flat fill is what this
    looks like when `TOP_SHADE` and `BOTTOM_SHADE` stop being applied."""
    pixels = tray_icon.icon_pixels((0x80, 0x80, 0x80))
    top = pixel(pixels, 8, 2)[:3]
    bottom = pixel(pixels, 8, 13)[:3]
    assert min(top) > max(bottom)


def test_the_icon_scales_to_a_size_windows_might_ask_for():
    """`make_icon` takes a size, and a dot hard-coded to 16 would be a 16-pixel disc in
    the corner of a 32-pixel icon."""
    pixels = tray_icon.icon_pixels((10, 20, 30), size=32)
    assert len(pixels) == 32 * 32 * 4
    assert pixel(pixels, 16, 16, size=32)[3] == 0xFF
    assert pixel(pixels, 0, 0, size=32)[3] == 0
