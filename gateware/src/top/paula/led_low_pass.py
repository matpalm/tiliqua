from amaranth import *

from tiliqua.dsp import ASQ


class LedLowPass(Elaboratable):
    """Approximate the Amiga LED low-pass with two cascaded one-pole stages."""

    INPUT_WIDTH = ASQ.as_shape().width
    ALPHA_SHIFT = 15
    ALPHA = 11469  # ~= 0.35 * 2^15, ~3.3 kHz at 48 kHz

    def __init__(self):
        self.i_sample = Signal(signed(self.INPUT_WIDTH))
        self.tick = Signal()
        self.o_sample = Signal(signed(self.INPUT_WIDTH))

    def elaborate(self, platform):
        m = Module()

        lp1 = Signal(signed(self.INPUT_WIDTH), init=0)
        lp2 = Signal(signed(self.INPUT_WIDTH), init=0)
        err1 = Signal(signed(self.INPUT_WIDTH + 1))
        err2 = Signal(signed(self.INPUT_WIDTH + 1))
        delta1 = Signal(signed(self.INPUT_WIDTH + self.ALPHA_SHIFT + 2))
        delta2 = Signal(signed(self.INPUT_WIDTH + self.ALPHA_SHIFT + 2))

        m.d.comb += [
            err1.eq(self.i_sample - lp1),
            err2.eq(lp1 - lp2),
            delta1.eq((err1 * self.ALPHA) >> self.ALPHA_SHIFT),
            delta2.eq((err2 * self.ALPHA) >> self.ALPHA_SHIFT),
            self.o_sample.eq(lp2),
        ]

        with m.If(self.tick):
            m.d.sync += [
                lp1.eq(lp1 + delta1),
                lp2.eq(lp2 + delta2),
            ]

        return m
