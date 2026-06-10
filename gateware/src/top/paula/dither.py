from amaranth import *
from tiliqua.dsp import ASQ


class Dither8BitQuantiser(Elaboratable):
    """TPDF dither + signed 8-bit quantizer for fixed-point audio samples."""

    INPUT_WIDTH = ASQ.as_shape().width
    TRUNC_SHIFT = max(INPUT_WIDTH - 8, 0)
    DITHER_REF_BITS = 8

    def __init__(self):
        self.i_sample = Signal(signed(self.INPUT_WIDTH))
        self.tick = Signal()
        self.o_sample_q8 = Signal(signed(8))

    def elaborate(self, platform):
        m = Module()

        # psuedo random linear shift register
        lfsr = Signal(16, init=0xACE1)
        lfsr_fb = Signal()
        rnd_a = Signal(unsigned(8))
        rnd_b = Signal(unsigned(8))
        dither_raw = Signal(signed(10))
        dither_scaled = Signal(signed(self.INPUT_WIDTH + 4))
        sample_dithered = Signal(signed(self.INPUT_WIDTH + 5))
        sample_shifted = Signal(signed(self.INPUT_WIDTH + 5))

        m.d.comb += [
            rnd_a.eq(lfsr[0:8]),
            rnd_b.eq(lfsr[8:16]),
            dither_raw.eq(rnd_a.as_unsigned() - rnd_b.as_unsigned()),
            lfsr_fb.eq(lfsr[0] ^ lfsr[2] ^ lfsr[3] ^ lfsr[5]),
        ]

        if self.TRUNC_SHIFT >= self.DITHER_REF_BITS:
            m.d.comb += dither_scaled.eq(
                dither_raw << (self.TRUNC_SHIFT - self.DITHER_REF_BITS)
            )
        else:
            m.d.comb += dither_scaled.eq(
                dither_raw >> (self.DITHER_REF_BITS - self.TRUNC_SHIFT)
            )

        m.d.comb += [
            sample_dithered.eq(self.i_sample + dither_scaled),
            sample_shifted.eq(sample_dithered >> self.TRUNC_SHIFT),
        ]

        with m.If(sample_shifted > 127):
            m.d.comb += self.o_sample_q8.eq(127)
        with m.Elif(sample_shifted < -128):
            m.d.comb += self.o_sample_q8.eq(-128)
        with m.Else():
            m.d.comb += self.o_sample_q8.eq(sample_shifted)

        with m.If(self.tick):
            m.d.sync += lfsr.eq(Cat(lfsr[1:16], lfsr_fb))

        return m
