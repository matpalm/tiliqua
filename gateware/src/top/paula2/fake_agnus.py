from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out


class FakeAgnus(wiring.Component):
    """Minimal Agnus-side model for Paula AUD0DAT feeding.

    Captures 1 second of input at startup, then replays that buffer while
    switching playback rate in 1-second segments: 1x, 1/4x, 1x, 4x, repeat.
    Each AUD0DAT word is packed as two consecutive sample bytes.
    """

    AUD0DAT = 0x55

    PHASE_WIDTH = 32
    FRAC_BITS = 16

    PHASE_INC_1X = 1 << FRAC_BITS

    INPUT_SAMPLE_HZ = 48_000
    PLAYBACK_1X_BYTE_HZ = 31_250
    CAPTURE_SECONDS = 1

    CAPTURE_BYTES = PLAYBACK_1X_BYTE_HZ * CAPTURE_SECONDS
    CAPTURE_WORDS = CAPTURE_BYTES // 2

    CAPTURE_NUM = PLAYBACK_1X_BYTE_HZ
    CAPTURE_DEN = INPUT_SAMPLE_HZ
    CAPTURE_EDGE = CAPTURE_DEN - CAPTURE_NUM

    RATE_SEGMENT_TICKS = INPUT_SAMPLE_HZ

    i_reset: In(1)
    i_audio_dmal: In(1)
    i_audio_dmas: In(1)
    i_sample_tick: In(1)
    i_sample_in: In(unsigned(8))

    i_reg_write: In(1)
    i_reg_addr: In(unsigned(8))
    i_reg_data: In(unsigned(16))

    o_audio_data0: Out(unsigned(16))
    o_capture_done: Out(1)
    o_phase_inc: Out(unsigned(PHASE_WIDTH))

    def elaborate(self, platform):
        m = Module()

        m.submodules.sample_mem = sample_mem = Memory(
            shape=unsigned(16), depth=self.CAPTURE_WORDS, init=[]
        )
        wr = sample_mem.write_port()
        rd = sample_mem.read_port()

        capturing = Signal(init=1)
        capture_count = Signal(range(self.CAPTURE_BYTES + 1), init=0)
        capture_word_ptr = Signal(range(self.CAPTURE_WORDS), init=0)
        capture_half = Signal(init=0)
        capture_first = Signal(unsigned(8), init=0)
        capture_acc = Signal(range(self.CAPTURE_DEN), init=0)
        capture_strobe = Signal()

        play_word_addr = Signal(range(self.CAPTURE_WORDS), init=0)
        phase = Signal(unsigned(self.PHASE_WIDTH), init=0)
        phase_inc = Signal(unsigned(self.PHASE_WIDTH))
        phase_next = Signal(unsigned(self.PHASE_WIDTH))
        phase_wrap = Signal(unsigned(self.PHASE_WIDTH))

        segment = Signal(range(4), init=0)
        segment_ticks = Signal(range(self.RATE_SEGMENT_TICKS + 1), init=0)

        mem_limit_phase = Const(self.CAPTURE_WORDS << self.FRAC_BITS, self.PHASE_WIDTH)

        dat_write = Signal()
        dat_write_prev = Signal(init=0)
        dat_write_edge = Signal()

        m.d.comb += [
            dat_write.eq(self.i_reg_write & (self.i_reg_addr == self.AUD0DAT)),
            dat_write_edge.eq(dat_write & ~dat_write_prev),
            capture_strobe.eq(
                self.i_sample_tick
                & capturing
                & (capture_count < self.CAPTURE_BYTES)
                & (capture_acc >= self.CAPTURE_EDGE)
            ),
            wr.en.eq(capture_strobe & capture_half),
            wr.addr.eq(capture_word_ptr),
            wr.data.eq(Cat(self.i_sample_in, capture_first)),
            rd.en.eq(1),
            rd.addr.eq(play_word_addr),
            self.o_audio_data0.eq(Mux(capturing, C(0, 16), rd.data)),
            self.o_capture_done.eq(~capturing),
            self.o_phase_inc.eq(phase_inc),
            phase_next.eq(phase + phase_inc),
            phase_wrap.eq(phase_next - mem_limit_phase),
        ]

        with m.Switch(segment):
            with m.Case(1):
                m.d.comb += phase_inc.eq(self.PHASE_INC_1X // 4)
            with m.Case(3):
                m.d.comb += phase_inc.eq(self.PHASE_INC_1X * 4)
            with m.Default():
                m.d.comb += phase_inc.eq(self.PHASE_INC_1X)

        m.d.sync += dat_write_prev.eq(dat_write)

        with m.If(self.i_reset):
            m.d.sync += [
                capturing.eq(1),
                capture_count.eq(0),
                capture_word_ptr.eq(0),
                capture_half.eq(0),
                capture_first.eq(0),
                capture_acc.eq(0),
                play_word_addr.eq(0),
                phase.eq(0),
                segment.eq(0),
                segment_ticks.eq(0),
            ]
        with m.Else():
            with m.If(
                self.i_sample_tick & capturing & (capture_count < self.CAPTURE_BYTES)
            ):
                with m.If(capture_strobe):
                    m.d.sync += [
                        capture_acc.eq(capture_acc - self.CAPTURE_EDGE),
                        capture_count.eq(capture_count + 1),
                    ]

                    with m.If(capture_half == 0):
                        m.d.sync += [
                            capture_first.eq(self.i_sample_in),
                            capture_half.eq(1),
                        ]
                    with m.Else():
                        m.d.sync += capture_half.eq(0)
                        with m.If(capture_word_ptr < (self.CAPTURE_WORDS - 1)):
                            m.d.sync += capture_word_ptr.eq(capture_word_ptr + 1)

                    with m.If((capture_count + 1) >= self.CAPTURE_BYTES):
                        m.d.sync += [
                            capturing.eq(0),
                            phase.eq(0),
                            play_word_addr.eq(0),
                            segment.eq(0),
                            segment_ticks.eq(0),
                        ]
                with m.Else():
                    m.d.sync += capture_acc.eq(capture_acc + self.CAPTURE_NUM)

            with m.If(~capturing & self.i_sample_tick):
                with m.If(segment_ticks == (self.RATE_SEGMENT_TICKS - 1)):
                    m.d.sync += segment_ticks.eq(0)
                    with m.If(segment == 3):
                        m.d.sync += segment.eq(0)
                    with m.Else():
                        m.d.sync += segment.eq(segment + 1)
                with m.Else():
                    m.d.sync += segment_ticks.eq(segment_ticks + 1)

            with m.If(dat_write_edge & ~capturing):
                with m.If(phase_next >= mem_limit_phase):
                    m.d.sync += [
                        phase.eq(phase_wrap),
                        play_word_addr.eq(phase_wrap[self.FRAC_BITS :]),
                    ]
                with m.Else():
                    m.d.sync += [
                        phase.eq(phase_next),
                        play_word_addr.eq(phase_next[self.FRAC_BITS :]),
                    ]

            with m.If(capture_count >= self.CAPTURE_BYTES):
                m.d.sync += capture_count.eq(self.CAPTURE_BYTES)

        return m
