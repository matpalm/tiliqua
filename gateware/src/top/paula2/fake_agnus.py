from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out


class FakeAgnus(wiring.Component):
    """Minimal Agnus-side model for Paula AUD0DAT feeding.

    Generates a byte stream from a phase accumulator and packs each AUD0DAT word
    as two consecutive samples: high byte is earlier sample, low byte is next.
    """

    AUD0DAT = 0x55

    PHASE_WIDTH = 24
    FRAC_BITS = 16

    PHASE_INC_1X = 1 << FRAC_BITS
    PHASE_INC_MIN = PHASE_INC_1X // 4
    PHASE_INC_MAX = PHASE_INC_1X * 4
    PHASE_INC_STEP = PHASE_INC_1X // 128

    SWEEP_TICKS = 1024

    i_reset: In(1)
    i_audio_dmal: In(1)
    i_audio_dmas: In(1)

    i_reg_write: In(1)
    i_reg_addr: In(unsigned(8))
    i_reg_data: In(unsigned(16))

    o_audio_data0: Out(unsigned(16))
    o_phase_inc: Out(unsigned(PHASE_WIDTH))

    def elaborate(self, platform):
        m = Module()

        phase = Signal(unsigned(self.PHASE_WIDTH), init=0)
        phase_inc = Signal(unsigned(self.PHASE_WIDTH), init=self.PHASE_INC_1X)
        phase_dir_up = Signal(init=1)
        sweep_div = Signal(range(self.SWEEP_TICKS), init=0)

        current_word = Signal(unsigned(16), init=0x0001)

        dat_write = Signal()
        dat_write_prev = Signal(init=0)
        dat_write_edge = Signal()

        phase_plus_inc = Signal(unsigned(self.PHASE_WIDTH))
        phase_plus_2inc = Signal(unsigned(self.PHASE_WIDTH))
        sample_hi = Signal(unsigned(8))
        sample_lo = Signal(unsigned(8))

        m.d.comb += [
            dat_write.eq(self.i_reg_write & (self.i_reg_addr == self.AUD0DAT)),
            dat_write_edge.eq(dat_write & ~dat_write_prev),
            phase_plus_inc.eq(phase + phase_inc),
            phase_plus_2inc.eq(phase + (phase_inc << 1)),
            sample_hi.eq(phase[self.FRAC_BITS : self.FRAC_BITS + 8]),
            sample_lo.eq(phase_plus_inc[self.FRAC_BITS : self.FRAC_BITS + 8]),
            self.o_audio_data0.eq(current_word),
            self.o_phase_inc.eq(phase_inc),
        ]

        m.d.sync += dat_write_prev.eq(dat_write)

        with m.If(self.i_reset):
            m.d.sync += [
                phase.eq(0),
                phase_inc.eq(self.PHASE_INC_1X),
                phase_dir_up.eq(1),
                sweep_div.eq(0),
                current_word.eq(0x0001),
            ]
        with m.Elif(dat_write_edge):
            m.d.sync += [
                # High byte plays first in Paula, so keep sample_hi earlier.
                current_word.eq(Cat(sample_lo, sample_hi)),
                phase.eq(phase_plus_2inc),
            ]

            with m.If(sweep_div == (self.SWEEP_TICKS - 1)):
                m.d.sync += sweep_div.eq(0)
                with m.If(phase_dir_up):
                    with m.If(phase_inc >= (self.PHASE_INC_MAX - self.PHASE_INC_STEP)):
                        m.d.sync += [
                            phase_inc.eq(self.PHASE_INC_MAX),
                            phase_dir_up.eq(0),
                        ]
                    with m.Else():
                        m.d.sync += phase_inc.eq(phase_inc + self.PHASE_INC_STEP)
                with m.Else():
                    with m.If(phase_inc <= (self.PHASE_INC_MIN + self.PHASE_INC_STEP)):
                        m.d.sync += [
                            phase_inc.eq(self.PHASE_INC_MIN),
                            phase_dir_up.eq(1),
                        ]
                    with m.Else():
                        m.d.sync += phase_inc.eq(phase_inc - self.PHASE_INC_STEP)
            with m.Else():
                m.d.sync += sweep_div.eq(sweep_div + 1)

        return m
