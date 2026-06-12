from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out


class FakeAgnus(wiring.Component):
    """Minimal Agnus-side model for Paula AUD0DAT feeding.

    Captures from input when record is active, then replays that buffer when
    playback is enabled.

    State behavior:
    - `record_note` starts recording on next positive zero crossing.
    - `record_note` while recording stops on next positive zero crossing and
      immediately starts playback if any sample was captured.
    - `play_note` toggles playback immediately.
    - `record_note` while playing back stops playback and arms recording.

    Playback rate modulation is driven by AUD0PER programming in top.py.
    Each AUD0DAT word is packed as two consecutive sample bytes.
    """

    AUD0DAT = 0x55

    PHASE_WIDTH = 32
    FRAC_BITS = 16

    PHASE_INC_1X = 1 << FRAC_BITS
    PAULA_CCK_HZ = 60_000_000 // 8
    # Keep this in sync with Paula2Top.PAULA_BASE_PERIOD.
    DEFAULT_AUD0PER = 320

    INPUT_SAMPLE_HZ = 48_000
    CAPTURE_SECONDS = 1

    # Capture at the default AUD0PER byte rate so DFT playback cadence
    # matches recorded sample cadence.
    CAPTURE_BYTES = (
        (PAULA_CCK_HZ * CAPTURE_SECONDS) // (2 * DEFAULT_AUD0PER) // 2
    ) * 2
    CAPTURE_WORDS = CAPTURE_BYTES // 2

    # Exact fractional capture strobe ratio over i_sample_tick events:
    # (PAULA_CCK_HZ / (2 * DEFAULT_AUD0PER)) / INPUT_SAMPLE_HZ.
    CAPTURE_NUM = PAULA_CCK_HZ
    CAPTURE_DEN = 2 * DEFAULT_AUD0PER * INPUT_SAMPLE_HZ
    CAPTURE_EDGE = CAPTURE_DEN - CAPTURE_NUM

    i_reset: In(1)
    i_audio_dmal: In(1)
    i_audio_dmas: In(1)
    i_record_toggle_evt: In(1)
    i_playback_evt: In(1)
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

        recording = Signal(init=0)
        playback_enabled = Signal(init=0)
        pending_record_start = Signal(init=0)
        pending_record_stop = Signal(init=0)

        in_sample_s8 = Signal(signed(8))
        in_prev_s8 = Signal(signed(8), init=0)
        in_pos_zero_cross = Signal()

        capture_count = Signal(range(self.CAPTURE_BYTES + 1), init=0)
        capture_word_ptr = Signal(range(self.CAPTURE_WORDS), init=0)
        valid_words = Signal(range(self.CAPTURE_WORDS + 1), init=0)
        capture_half = Signal(init=0)
        capture_first = Signal(unsigned(8), init=0)
        capture_acc = Signal(range(self.CAPTURE_DEN), init=0)
        capture_strobe = Signal()

        play_word_addr = Signal(range(self.CAPTURE_WORDS), init=0)
        phase = Signal(unsigned(self.PHASE_WIDTH), init=0)
        phase_inc = Signal(unsigned(self.PHASE_WIDTH))
        phase_next = Signal(unsigned(self.PHASE_WIDTH))
        phase_wrap = Signal(unsigned(self.PHASE_WIDTH))
        mem_limit_words = Signal(range(self.CAPTURE_WORDS + 1))
        mem_limit_phase = Signal(unsigned(self.PHASE_WIDTH))

        dat_write = Signal()
        dat_write_prev = Signal(init=0)
        dat_write_edge = Signal()

        m.d.comb += [
            dat_write.eq(self.i_reg_write & (self.i_reg_addr == self.AUD0DAT)),
            dat_write_edge.eq(dat_write & ~dat_write_prev),
            in_sample_s8.eq(self.i_sample_in.as_signed()),
            in_pos_zero_cross.eq(
                self.i_sample_tick & (in_prev_s8 <= 0) & (in_sample_s8 > 0)
            ),
            capture_strobe.eq(
                self.i_sample_tick
                & recording
                & (capture_count < self.CAPTURE_BYTES)
                & (capture_acc >= self.CAPTURE_EDGE)
            ),
            wr.en.eq(capture_strobe & capture_half),
            wr.addr.eq(capture_word_ptr),
            wr.data.eq(Cat(self.i_sample_in, capture_first)),
            rd.en.eq(1),
            rd.addr.eq(play_word_addr),
            self.o_audio_data0.eq(
                Mux(playback_enabled & (valid_words != 0), rd.data, C(0, 16))
            ),
            self.o_capture_done.eq(~recording),
            self.o_phase_inc.eq(phase_inc),
            mem_limit_words.eq(
                Mux(
                    valid_words == 0,
                    C(1, mem_limit_words.shape().width),
                    valid_words,
                )
            ),
            mem_limit_phase.eq(mem_limit_words << self.FRAC_BITS),
            phase_next.eq(phase + phase_inc),
            phase_wrap.eq(phase_next - mem_limit_phase),
        ]

        m.d.comb += phase_inc.eq(self.PHASE_INC_1X)

        m.d.sync += dat_write_prev.eq(dat_write)

        with m.If(self.i_reset):
            m.d.sync += [
                recording.eq(0),
                playback_enabled.eq(0),
                pending_record_start.eq(0),
                pending_record_stop.eq(0),
                in_prev_s8.eq(0),
                capture_count.eq(0),
                capture_word_ptr.eq(0),
                valid_words.eq(0),
                capture_half.eq(0),
                capture_first.eq(0),
                capture_acc.eq(0),
                play_word_addr.eq(0),
                phase.eq(0),
            ]
        with m.Else():
            with m.If(self.i_record_toggle_evt):
                with m.If(recording):
                    # Toggle stop while recording; apply at next + zero crossing.
                    with m.If(pending_record_stop):
                        m.d.sync += pending_record_stop.eq(0)
                    with m.Else():
                        m.d.sync += pending_record_stop.eq(1)
                with m.Else():
                    # Arm recording start at next + zero crossing.
                    with m.If(pending_record_start):
                        m.d.sync += pending_record_start.eq(0)
                    with m.Else():
                        m.d.sync += [
                            playback_enabled.eq(0),
                            pending_record_start.eq(1),
                            pending_record_stop.eq(0),
                        ]
            with m.Elif(self.i_playback_evt):
                with m.If(playback_enabled):
                    m.d.sync += playback_enabled.eq(0)
                with m.Elif((~recording) & (~pending_record_start) & (valid_words != 0)):
                    m.d.sync += [
                        playback_enabled.eq(1),
                        play_word_addr.eq(0),
                        phase.eq(0),
                    ]

            # Record transitions are zero-cross aligned.
            with m.If(in_pos_zero_cross):
                with m.If(pending_record_stop & recording):
                    m.d.sync += [
                        recording.eq(0),
                        pending_record_stop.eq(0),
                        capture_half.eq(0),
                        playback_enabled.eq(valid_words != 0),
                        play_word_addr.eq(0),
                        phase.eq(0),
                    ]
                with m.Elif(pending_record_start & ~recording):
                    m.d.sync += [
                        recording.eq(1),
                        pending_record_start.eq(0),
                        playback_enabled.eq(0),
                        capture_count.eq(0),
                        capture_word_ptr.eq(0),
                        valid_words.eq(0),
                        capture_half.eq(0),
                        capture_first.eq(0),
                        capture_acc.eq(0),
                        play_word_addr.eq(0),
                        phase.eq(0),
                    ]

            with m.If(self.i_sample_tick):
                m.d.sync += in_prev_s8.eq(in_sample_s8)

            with m.If(
                self.i_sample_tick & recording & (capture_count < self.CAPTURE_BYTES)
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
                        m.d.sync += [
                            capture_half.eq(0),
                            valid_words.eq(capture_word_ptr + 1),
                        ]
                        with m.If(capture_word_ptr < (self.CAPTURE_WORDS - 1)):
                            m.d.sync += capture_word_ptr.eq(capture_word_ptr + 1)

                    with m.If((capture_count + 1) >= self.CAPTURE_BYTES):
                        m.d.sync += [
                            recording.eq(0),
                            capture_half.eq(0),
                        ]
                with m.Else():
                    m.d.sync += capture_acc.eq(capture_acc + self.CAPTURE_NUM)

            with m.If(dat_write_edge & playback_enabled & (valid_words != 0)):
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
