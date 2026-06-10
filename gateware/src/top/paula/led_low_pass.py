from amaranth import *

from tiliqua.dsp import ASQ
from tiliqua.dsp.filters import OnePole

class LedLowPass(Elaboratable):
    """hacky Amiga LEDish lowpass filter ( as 2 cascaded one-poles )"""

    INPUT_WIDTH = ASQ.as_shape().width
    SHIFT = 2

    def __init__(self):
        self.i_sample = Signal(signed(self.INPUT_WIDTH))
        self.tick = Signal()
        self.o_sample = Signal(signed(self.INPUT_WIDTH))

    def elaborate(self, platform):
        m = Module()

        m.submodules.pole1 = pole1 = OnePole()
        m.submodules.pole2 = pole2 = OnePole()

        m.d.comb += [
            pole1.i.valid.eq(self.tick),
            pole1.i.payload.eq(self.i_sample),
            pole1.shift.eq(self.SHIFT),
            pole1.o.ready.eq(pole2.i.ready),
            pole2.i.valid.eq(pole1.o.valid),
            pole2.i.payload.eq(pole1.o.payload),
            pole2.shift.eq(self.SHIFT),
            pole2.o.ready.eq(1),
            self.o_sample.eq(pole2.o.payload),
        ]

        return m
