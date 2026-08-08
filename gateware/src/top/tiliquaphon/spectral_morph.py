# Copyright (c) 2024 S. Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#

from amaranth import *
from amaranth.lib import stream, wiring
from amaranth.lib.wiring import In, Out

from amaranth_future import fixed

from tiliqua.dsp.block import Block
from tiliqua.dsp.complex import CQ
from tiliqua.dsp.stream_util import Merge


class SpectralMagnitudeMorph(wiring.Component):
    """First-pass spectral morph by crossfading complex FFT bins.

    This keeps the STFT analysis/synthesis pipeline and channel routing in
    place while blending the two spectra directly in rectangular form.
    """

    def __init__(self, shape: fixed.Shape, sz: int):
        self.shape = shape
        self.sz = sz
        super().__init__(
            {
                "alpha": In(self.shape, init=fixed.Const(0.0, shape=self.shape)),
                "i_carrier": In(stream.Signature(Block(CQ(self.shape)))),
                "i_modulator": In(stream.Signature(Block(CQ(self.shape)))),
                "o": Out(stream.Signature(Block(CQ(self.shape)))),
            }
        )

    def elaborate(self, platform) -> Module:
        m = Module()

        m.submodules.merge2 = merge2 = Merge(
            n_channels=2,
            shape=self.i_carrier.payload.shape(),
        )
        wiring.connect(m, wiring.flipped(self.i_carrier), merge2.i[0])
        wiring.connect(m, wiring.flipped(self.i_modulator), merge2.i[1])

        one_raw = fixed.Const(1.0, shape=self.shape, clamp=True).as_value()
        out_max = self.shape.max().as_value()
        out_min = self.shape.min().as_value()

        alpha_raw = self.alpha.as_value()
        carrier_real_raw = merge2.o.payload[0].sample.real.as_value()
        carrier_imag_raw = merge2.o.payload[0].sample.imag.as_value()
        modulator_real_raw = merge2.o.payload[1].sample.real.as_value()
        modulator_imag_raw = merge2.o.payload[1].sample.imag.as_value()

        blend_real_raw = (
            (carrier_real_raw * (one_raw - alpha_raw))
            + (modulator_real_raw * alpha_raw)
        ) >> self.shape.f_bits
        blend_imag_raw = (
            (carrier_imag_raw * (one_raw - alpha_raw))
            + (modulator_imag_raw * alpha_raw)
        ) >> self.shape.f_bits

        m.d.comb += [
            merge2.o.ready.eq(self.o.ready),
            self.o.valid.eq(merge2.o.valid),
            self.o.payload.first.eq(merge2.o.payload[0].first),
            self.o.payload.sample.real.eq(
                Mux(
                    blend_real_raw > out_max,
                    out_max,
                    Mux(blend_real_raw < out_min, out_min, blend_real_raw),
                )
            ),
            self.o.payload.sample.imag.eq(
                Mux(
                    blend_imag_raw > out_max,
                    out_max,
                    Mux(blend_imag_raw < out_min, out_min, blend_imag_raw),
                )
            ),
        ]

        return m
