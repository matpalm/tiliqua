from amaranth import *
from math import exp, pi

from tiliqua.dsp import ASQ


class LedLowPass(Elaboratable):
    """Approximate Amiga LED lowpass with two cascaded first-order RC stages.

    The poles are tuned so the *combined* -3 dB point is roughly 3.3 kHz at a
    48 kHz update rate.
    """

    INPUT_WIDTH = ASQ.as_shape().width

    SAMPLE_RATE_HZ = 48_000
    TARGET_CUTOFF_HZ = 3_300

    # For two identical cascaded one-poles, each pole needs fc ~= 1.5538x of
    # the desired combined -3 dB cutoff.
    CASCADE_POLE_SCALE = 1.553773974
    POLE_CUTOFF_HZ = TARGET_CUTOFF_HZ * CASCADE_POLE_SCALE

    COEFF_BITS = 16
    STATE_FRAC_BITS = 10

    _ALPHA = 1.0 - exp(-2.0 * pi * POLE_CUTOFF_HZ / SAMPLE_RATE_HZ)
    ALPHA_FP = int(round(_ALPHA * (1 << COEFF_BITS)))
    ALPHA_FP = max(1, min((1 << COEFF_BITS) - 1, ALPHA_FP))

    def __init__(self):
        self.tick = Signal()
        self.i_sample = Signal(signed(self.INPUT_WIDTH))
        self.o_sample = Signal(signed(self.INPUT_WIDTH))

    def elaborate(self, platform):
        m = Module()

        state_width = self.INPUT_WIDTH + self.STATE_FRAC_BITS + 2

        pole1_state = Signal(signed(state_width))
        pole2_state = Signal(signed(state_width))

        inp_ext = Signal(signed(state_width))
        pole1_delta = Signal(signed(state_width + 1))
        pole2_delta = Signal(signed(state_width + 1))
        pole1_step = Signal(signed(state_width + self.COEFF_BITS + 1))
        pole2_step = Signal(signed(state_width + self.COEFF_BITS + 1))

        m.d.comb += [
            inp_ext.eq(self.i_sample << self.STATE_FRAC_BITS),
            pole1_delta.eq(inp_ext - pole1_state),
            pole2_delta.eq(pole1_state - pole2_state),
            pole1_step.eq((pole1_delta * C(self.ALPHA_FP)) >> self.COEFF_BITS),
            pole2_step.eq((pole2_delta * C(self.ALPHA_FP)) >> self.COEFF_BITS),
            self.o_sample.eq(pole2_state >> self.STATE_FRAC_BITS),
        ]

        with m.If(self.tick):
            m.d.sync += [
                pole1_state.eq(pole1_state + pole1_step),
                pole2_state.eq(pole2_state + pole2_step),
            ]

        return m
