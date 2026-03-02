# Copyright (c) 2024 Seb Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0

from amaranth import *
from amaranth.lib import data
from amaranth.lib import enum as amaranth_enum


class Rotation(amaranth_enum.Enum, shape=unsigned(2)):
    """
    Display rotation, all 90-degree orientations.
    """
    NORMAL = 0
    LEFT = 1
    INVERTED = 2
    RIGHT = 3


class Pixel(data.Struct):
    """
    Framebuffer pixel format used throughout video/raster system.
    Separate color and intensity for convenience.
    This is tranformed to a true RGB value by a palette before it
    hits the DVI PHY.
    """

    color:     unsigned(4)
    intensity: unsigned(4)

    def intensity_max():
        return 0xF


class ScanPixel(data.Struct):
    """
    Framebuffer Pixel coupled with timing info. 1 per dvi clock.
    Useful for beamracing overlays hooked up between the FB and PHY.
    """
    pixel: Pixel
    x:     signed(12)
    y:     signed(12)
    de:    unsigned(1)
    hsync: unsigned(1)
    vsync: unsigned(1)


class DVIPixel(data.Struct):
    """
    RGB pixel with sync signals.
    The DVI PHY needs 1 per dvi clock.
    """
    r:     unsigned(8)
    g:     unsigned(8)
    b:     unsigned(8)
    de:    unsigned(1)
    hsync: unsigned(1)
    vsync: unsigned(1)
