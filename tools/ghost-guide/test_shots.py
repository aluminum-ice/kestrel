"""Tests for the screenshot framing and comparison (Pillow only, no browser)."""

import os
import sys

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import shots  # noqa: E402


def _png(tmp_path, name, colour, size=(824, 560)):
    path = str(tmp_path / name)
    Image.new("RGB", size, colour).save(path)
    return path


def test_same_pixels_sees_colour_changes_in_opaque_images(tmp_path):
    a = _png(tmp_path, "a.png", (10, 10, 10))
    b = _png(tmp_path, "b.png", (10, 10, 11))
    assert shots.same_pixels(a, _png(tmp_path, "a2.png", (10, 10, 10)))
    assert not shots.same_pixels(a, b)
    assert not shots.same_pixels(a, _png(tmp_path, "c.png", (10, 10, 10), (824, 561)))


def test_phone_frame_geometry(tmp_path):
    page = _png(tmp_path, "page.png", (24, 26, 30), (860, 1792))
    framed = shots.frame_phone(page)
    assert framed.size == (904, 1908)
    assert framed.getpixel((0, 0))[3] == 0                     # rounded corner
    assert framed.getpixel((452, 958))[:3] == (24, 26, 30)     # page
    assert framed.getpixel((452, 40))[:3] == shots.ISLAND_RGB  # Dynamic Island
    assert framed.getpixel((452, 80 + 15))[:3] == (24, 26, 30)  # status strip


def test_eink_frame_is_3x_with_margin():
    panel = Image.new("1", (264, 176), 255)
    framed = shots.frame_eink(panel)
    assert framed.size == (824, 560)
    assert framed.getpixel((2, 2)) == shots.EINK_BG
