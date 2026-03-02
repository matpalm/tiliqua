# Copyright (c) 2024 Seb Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0

from amaranth import *
from amaranth.build import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.wiring import In, Out

from . import PSQ, PSQ_BASE_FBITS
from ..video.framebuffer import DMAFramebuffer
from .plot import BlendMode, OffsetMode, PlotRequest


class Stroke(wiring.Component):

    """
    Frontend for stroke-raster converter (plotting of analog waveforms in CRT-style)

    Takes a synchronized stream of 4 channels (x, y, intensity, color) and
    generates ``PlotRequest`` commands for blended drawing to a framebuffer.

    There are a few optional signals exposed which can be used by user gateware or
    an SoC to scale or shift the waveforms around.
    """

    def __init__(self, *, default_hue=10, default_x=0, default_y=0):

        self.hue       = Signal(4, init=default_hue);
        self.intensity = Signal(4, init=8);
        self.scale_x   = Signal(4, init=6);
        self.scale_y   = Signal(4, init=6);
        self.scale_p   = Signal(4, init=10);
        self.scale_c   = Signal(4, init=10);
        self.x_offset  = Signal(signed(16), init=default_x)
        self.y_offset  = Signal(signed(16), init=default_y)

        super().__init__({
            # Point stream to render
            # 4 channels: x, y, intensity, color
            "i": In(stream.Signature(data.ArrayLayout(PSQ, 4))),
            # Plot request output to shared backend
            "o": Out(stream.Signature(PlotRequest)),
        })


    def elaborate(self, platform) -> Module:
        m = Module()

        self.point_stream = self.i

        # last sample
        sample_x = Signal(signed(16))
        sample_y = Signal(signed(16))
        sample_p = Signal(signed(16)) # intensity modulation
        sample_c = Signal(signed(16)) # color modulation

        # Pixel request generation
        new_color = Signal(unsigned(4))
        sample_intensity = Signal(unsigned(4))

        # Calculate new color (sample color + base hue)
        m.d.comb += new_color.eq(sample_c + self.hue)

        # Calculate sample intensity with bounds checking
        with m.If((sample_p + self.intensity > 0) & (sample_p + self.intensity <= 0xf)):
            m.d.comb += sample_intensity.eq(sample_p + self.intensity)
        with m.Else():
            m.d.comb += sample_intensity.eq(0)

        # Generate pixel request for the shared `PlotRequest` backend
        m.d.comb += [
            self.o.payload.x.eq(sample_x),
            self.o.payload.y.eq(sample_y),
            self.o.payload.pixel.color.eq(new_color),
            self.o.payload.pixel.intensity.eq(sample_intensity),
            self.o.payload.blend.eq(BlendMode.ADDITIVE),  # CRT sim uses additive blending
            self.o.payload.offset.eq(OffsetMode.CENTER),  # Scope plots are centered
        ]

        with m.FSM() as fsm:

            with m.State('LATCH0'):
                m.d.comb += self.point_stream.ready.eq(1)
                # Fired on every audio sample fs_strobe
                with m.If(self.point_stream.valid):
                    m.d.sync += [
                        sample_x.eq((self.point_stream.payload[0].reshape(PSQ_BASE_FBITS).as_value()>>self.scale_x) + self.x_offset),
                        # invert sample_y for positive scope -> up
                        sample_y.eq((-self.point_stream.payload[1].reshape(PSQ_BASE_FBITS).as_value()>>self.scale_y) + self.y_offset),
                        sample_p.eq(Mux(self.scale_p != 0xf, self.point_stream.payload[2].reshape(PSQ_BASE_FBITS).as_value()>>self.scale_p, 0)),
                        sample_c.eq(Mux(self.scale_c != 0xf, self.point_stream.payload[3].reshape(PSQ_BASE_FBITS).as_value()>>self.scale_c, 0)),
                    ]
                    m.next = 'SEND_PIXEL'

            with m.State('SEND_PIXEL'):
                m.d.comb += self.o.valid.eq(1)
                with m.If(self.o.ready):
                    m.next = 'LATCH0'

        return m
