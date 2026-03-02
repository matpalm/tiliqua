# Utilities for color palette hardware.
#
# Copyright (c) 2024 Seb Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0

import colorsys

from amaranth import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out
from amaranth_soc import csr

from .types import ScanPixel, DVIPixel


def compute_color_palette():
    """
    Calculate 16*16 (256) color palette to map each 8-bit pixel storage
    into R8/G8/B8 pixel value for sending to the DVI PHY. Each pixel
    is stored as a 4-bit intensity and 4-bit color.

    For SoC bitstreams, the palette is usually calculated in firmware
    and written using the palette update interface to the hardware, so
    this just serves as a sane default.
    """
    n_i = 16  # intensity levels
    n_c = 16  # color levels
    rs, gs, bs = [], [], []
    for i in range(n_i):
        for c in range(n_c):
            r, g, b = colorsys.hls_to_rgb(
                    float(c)/n_c, float(1.35**(i+1))/(1.35**n_i), 0.75)
            rs.append(int(r*255))
            gs.append(int(g*255))
            bs.append(int(b*255))

    return rs, gs, bs


class ColorPalette(wiring.Component):
    """
    ScanPixel -> DVIPixel palette transform with updatable palette.

    Maps 8-bit pixels (4-bit intensity + 4-bit color) to 24-bit RGB via
    a 256-entry lookup table. The palette can be updated at runtime via
    the ``update`` stream interface.
    """

    def __init__(self):
        super().__init__({
            # Pre-palette pixel (1 per dvi clock)
            "i": In(ScanPixel),
            # Post-palette pixel (1 per dvi clock)
            "o": Out(DVIPixel),
            # Palette update interface
            "update": In(stream.Signature(data.StructLayout({
                "position": unsigned(8),
                "red":      unsigned(8),
                "green":    unsigned(8),
                "blue":     unsigned(8),
            }))),
        })

    def elaborate(self, platform) -> Module:
        m = Module()

        # Create memories for the palette
        rs, gs, bs = compute_color_palette()
        m.submodules.palette_r = palette_r = Memory(shape=unsigned(8), depth=256, init=rs)
        m.submodules.palette_g = palette_g = Memory(shape=unsigned(8), depth=256, init=gs)
        m.submodules.palette_b = palette_b = Memory(shape=unsigned(8), depth=256, init=bs)

        # Read ports (combinatorial lookup)
        rd_port_r = palette_r.read_port(domain="comb")
        rd_port_g = palette_g.read_port(domain="comb")
        rd_port_b = palette_b.read_port(domain="comb")

        pixel_in = Cat(self.i.pixel.color, self.i.pixel.intensity)

        m.d.comb += [
            rd_port_r.addr.eq(pixel_in),
            rd_port_g.addr.eq(pixel_in),
            rd_port_b.addr.eq(pixel_in),
        ]

        # Pass through sync signals, replace pixel with RGB
        m.d.comb += [
            self.o.r.eq(rd_port_r.data),
            self.o.g.eq(rd_port_g.data),
            self.o.b.eq(rd_port_b.data),
            self.o.de.eq(self.i.de),
            self.o.hsync.eq(self.i.hsync),
            self.o.vsync.eq(self.i.vsync),
        ]

        # Write ports for palette updates
        wport_r = palette_r.write_port()
        wport_g = palette_g.write_port()
        wport_b = palette_b.write_port()

        wports = [wport_r, wport_g, wport_b]
        m.d.comb += [
            wports[0].data.eq(self.update.payload.red),
            wports[1].data.eq(self.update.payload.green),
            wports[2].data.eq(self.update.payload.blue),
        ]
        m.d.comb += self.update.ready.eq(1)

        for wport in wports:
            with m.If(self.update.ready):
                m.d.comb += [
                    wport.addr.eq(self.update.payload.position),
                    wport.en.eq(self.update.valid),
                ]

        return m


class Peripheral(wiring.Component):

    class PaletteReg(csr.Register, access="w"):
        position: csr.Field(csr.action.W, unsigned(8))
        red:      csr.Field(csr.action.W, unsigned(8))
        green:    csr.Field(csr.action.W, unsigned(8))
        blue:     csr.Field(csr.action.W, unsigned(8))

    class PaletteBusyReg(csr.Register, access="r"):
        busy: csr.Field(csr.action.R, unsigned(1))

    def __init__(self):

        self.palette = ColorPalette()

        regs = csr.Builder(addr_width=6, data_width=8)

        self._palette      = regs.add("palette",      self.PaletteReg(),     offset=0x0)
        self._palette_busy = regs.add("palette_busy", self.PaletteBusyReg(), offset=0x4)

        self._bridge = csr.Bridge(regs.as_memory_map())

        super().__init__({
            "bus": In(csr.Signature(addr_width=regs.addr_width, data_width=regs.data_width)),
        })

        self.bus.memory_map = self._bridge.bus.memory_map

    def elaborate(self, platform) -> Module:

        m = Module()

        m.submodules.bridge = self._bridge

        wiring.connect(m, wiring.flipped(self.bus), self._bridge.bus)

        m.submodules.palette = self.palette

        # palette update logic
        palette_busy = Signal()
        m.d.comb += self._palette_busy.f.busy.r_data.eq(palette_busy)

        with m.If(self._palette.element.w_stb & ~palette_busy):
            m.d.sync += [
                palette_busy                          .eq(1),
                self.palette.update.valid            .eq(1),
                self.palette.update.payload.position .eq(self._palette.f.position.w_data),
                self.palette.update.payload.red      .eq(self._palette.f.red.w_data),
                self.palette.update.payload.green    .eq(self._palette.f.green.w_data),
                self.palette.update.payload.blue     .eq(self._palette.f.blue.w_data),
            ]

        with m.If(palette_busy & self.palette.update.ready):
            # coefficient has been written
            m.d.sync += [
                palette_busy.eq(0),
                self.palette.update.valid.eq(0),
            ]

        return m
