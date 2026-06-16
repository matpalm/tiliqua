from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from amaranth_soc import wishbone

from sampling import Sample

class FakeAgnus(wiring.Component):
    """Minimal Agnus-side model for Paula AUDxDAT feeding.

    Captures from input when record is active, then replays that buffer when
    playback is enabled.

    State behavior:
    - `record_note` starts recording on next positive zero crossing.
    - `record_note` while recording stops on next positive zero crossing and
      immediately starts playback if any sample was captured.
    - `play_note` toggles playback immediately.
    - `record_note` while playing back stops playback and arms recording.

    Playback rate modulation is driven by AUD0PER programming in top.py.
    Playback loop bounds are controlled by AUDxSTART and AUDxLEN writes.
    Each AUD0DAT word is packed as two consecutive sample bytes.
    """

    PHASE_WIDTH = 32
    FRAC_BITS = 16

    PHASE_INC_1X = 1 << FRAC_BITS
    PAULA_CCK_HZ = 60_000_000 // 8  # todo: move into shared config between paula top
    DEFAULT_AUDxPER = 320

    INPUT_SAMPLE_HZ = 48_000
    CAPTURE_SECONDS = 10

    # capture at the default AUDxPER byte rate so default playback rate
    # matches recorded sample rate.
    CAPTURE_BYTES = ((PAULA_CCK_HZ * CAPTURE_SECONDS) // DEFAULT_AUDxPER // 2) * 2
    CAPTURE_WORDS = CAPTURE_BYTES // 2

    # fractional capture strobe ratio over i_sample_tick events:
    # (PAULA_CCK_HZ / DEFAULT_AUDxPER) / INPUT_SAMPLE_HZ.

    CAPTURE_NUM = PAULA_CCK_HZ
    CAPTURE_DEN = DEFAULT_AUDxPER * INPUT_SAMPLE_HZ
    CAPTURE_EDGE = CAPTURE_DEN - CAPTURE_NUM

    i_reset: In(1)
    i_audio_dmal: In(1)
    i_audio_dmas: In(1)
    i_record_toggle_evt: In(1)
    i_playback_evt: In(1)
    i_loop_playback: In(1)
    i_sample_tick: In(1)
    i_sample_in: In(unsigned(8))

    i_reg_write: In(1)
    i_reg_addr: In(unsigned(8))
    i_reg_data: In(unsigned(16))

    # not worth making it work with data_width=16 for now :/
    bus: Out(
        wishbone.Signature(
            addr_width=Sample.PSRAM_ADDR_WIDTH,
            data_width=32,
            granularity=8,
            features={"cti", "bte"},
        )
    )

    o_audio_data0: Out(unsigned(16))
    o_capture_done: Out(1)
    o_phase_inc: Out(unsigned(PHASE_WIDTH))

    def __init__(
        self,
        aud_dat_addr: int,
        aud_len_addr: int,
        aud_start_addr: int,
        psram_base_word: int,
    ):
        self.aud_dat_addr = int(aud_dat_addr)
        self.aud_len_addr = int(
            aud_dat_addr - 3 if aud_len_addr is None else aud_len_addr
        )
        self.aud_start_addr = int(
            aud_dat_addr - 5 if aud_start_addr is None else aud_start_addr
        )
        self.psram_base_word = int(psram_base_word)
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.sample = sample = Sample(
            depth=self.CAPTURE_WORDS,
            psram_base_word=self.psram_base_word,
        )

        recording = Signal(init=0)
        playback_enabled = Signal(init=0)
        pending_record_start = Signal(init=0)
        pending_record_stop = Signal(init=0)

        capture_count = Signal(range(self.CAPTURE_BYTES + 1), init=0)
        capture_word_ptr = Signal(range(self.CAPTURE_WORDS), init=0)
        valid_words = Signal(range(self.CAPTURE_WORDS + 1), init=0)
        capture_acc = Signal(range(self.CAPTURE_DEN), init=0)
        capture_strobe = Signal()
        capture_flush = Signal()

        play_word_addr = Signal(range(self.CAPTURE_WORDS), init=0)
        phase = Signal(unsigned(self.PHASE_WIDTH), init=0)
        phase_inc = Signal(unsigned(self.PHASE_WIDTH))
        phase_next = Signal(unsigned(self.PHASE_WIDTH))
        phase_wrap = Signal(unsigned(self.PHASE_WIDTH))
        max_start_words = Signal(range(self.CAPTURE_WORDS + 1))
        available_from_start = Signal(range(self.CAPTURE_WORDS + 1))
        loop_len_words = Signal(range(self.CAPTURE_WORDS + 1))
        loop_end_words = Signal(range(self.CAPTURE_WORDS + 1))
        programmed_len_words = Signal(
            range(self.CAPTURE_WORDS + 1), init=self.CAPTURE_WORDS
        )
        programmed_start_words = Signal(range(self.CAPTURE_WORDS), init=0)
        mem_limit_words = Signal(range(self.CAPTURE_WORDS + 1))
        mem_start_words = Signal(range(self.CAPTURE_WORDS + 1))
        mem_limit_phase = Signal(unsigned(self.PHASE_WIDTH))
        mem_start_phase = Signal(unsigned(self.PHASE_WIDTH))
        play_mem_limit_phase = Signal(unsigned(self.PHASE_WIDTH), init=(1 << self.FRAC_BITS))
        play_mem_start_phase = Signal(unsigned(self.PHASE_WIDTH), init=0)
        play_phase_wrap = Signal(unsigned(self.PHASE_WIDTH))

        dat_write = Signal()
        len_write = Signal()
        start_write = Signal()
        dat_write_prev = Signal(init=0)
        dat_write_edge = Signal()

        m.d.comb += [
            dat_write.eq(self.i_reg_write & (self.i_reg_addr == self.aud_dat_addr)),
            len_write.eq(self.i_reg_write & (self.i_reg_addr == self.aud_len_addr)),
            start_write.eq(self.i_reg_write & (self.i_reg_addr == self.aud_start_addr)),
            dat_write_edge.eq(dat_write & ~dat_write_prev),
            capture_strobe.eq(
                self.i_sample_tick
                & recording
                & (capture_count < self.CAPTURE_BYTES)
                & (capture_acc >= self.CAPTURE_EDGE)
            ),
            sample.i_reset.eq(self.i_reset),
            sample.i_sample_tick.eq(self.i_sample_tick),
            sample.i_sample_byte.eq(self.i_sample_in),
            sample.i_capture_flush.eq(capture_flush),
            sample.i_capture_strobe.eq(capture_strobe),
            sample.i_capture_byte.eq(self.i_sample_in),
            sample.i_capture_word_addr.eq(capture_word_ptr),
            sample.i_read_en.eq(playback_enabled & (valid_words != 0)),
            sample.i_read_addr.eq(play_word_addr),
            self.bus.adr.eq(sample.bus.adr),
            self.bus.dat_w.eq(sample.bus.dat_w),
            self.bus.sel.eq(sample.bus.sel),
            self.bus.cyc.eq(sample.bus.cyc),
            self.bus.stb.eq(sample.bus.stb),
            self.bus.we.eq(sample.bus.we),
            self.bus.cti.eq(sample.bus.cti),
            self.bus.bte.eq(sample.bus.bte),
            sample.bus.dat_r.eq(self.bus.dat_r),
            sample.bus.ack.eq(self.bus.ack),
            self.o_audio_data0.eq(
                Mux(
                    playback_enabled & (valid_words != 0) & sample.o_read_valid,
                    sample.o_read_data,
                    C(0, 16),
                )
            ),
            self.o_capture_done.eq(~recording),
            self.o_phase_inc.eq(phase_inc),
            max_start_words.eq(
                Mux(
                    valid_words == 0,
                    C(0, max_start_words.shape().width),
                    valid_words - 1,
                )
            ),
            mem_start_words.eq(
                Mux(
                    programmed_start_words >= max_start_words,
                    max_start_words,
                    programmed_start_words,
                )
            ),
            available_from_start.eq(
                Mux(
                    valid_words <= mem_start_words,
                    C(1, available_from_start.shape().width),
                    valid_words - mem_start_words,
                )
            ),
            loop_len_words.eq(
                Mux(
                    programmed_len_words <= 1,
                    C(1, loop_len_words.shape().width),
                    Mux(
                        programmed_len_words >= available_from_start,
                        available_from_start,
                        programmed_len_words,
                    ),
                )
            ),
            loop_end_words.eq(mem_start_words + loop_len_words),
            mem_limit_words.eq(
                Mux(
                    loop_end_words <= mem_start_words,
                    mem_start_words + 1,
                    loop_end_words,
                )
            ),
            mem_start_phase.eq(mem_start_words << self.FRAC_BITS),
            mem_limit_phase.eq(mem_limit_words << self.FRAC_BITS),
            phase_next.eq(phase + phase_inc),
            phase_wrap.eq(mem_start_phase + (phase_next - mem_limit_phase)),
            play_phase_wrap.eq(
                play_mem_start_phase + (phase_next - play_mem_limit_phase)
            ),
        ]

        m.d.comb += phase_inc.eq(self.PHASE_INC_1X)

        m.d.sync += dat_write_prev.eq(dat_write)

        with m.If(self.i_reset):
            m.d.sync += [
                recording.eq(0),
                playback_enabled.eq(0),
                pending_record_start.eq(0),
                pending_record_stop.eq(0),
                capture_flush.eq(0),
                capture_count.eq(0),
                capture_word_ptr.eq(0),
                valid_words.eq(0),
                capture_acc.eq(0),
                programmed_len_words.eq(self.CAPTURE_WORDS),
                programmed_start_words.eq(0),
                play_word_addr.eq(0),
                phase.eq(0),
            ]
        with m.Else():
            m.d.sync += [
                capture_flush.eq(0),
                play_mem_start_phase.eq(mem_start_phase),
                play_mem_limit_phase.eq(mem_limit_phase),
            ]

            with m.If(len_write):
                with m.If(self.i_reg_data <= 1):
                    m.d.sync += programmed_len_words.eq(1)
                with m.Elif(self.i_reg_data >= self.CAPTURE_WORDS):
                    m.d.sync += programmed_len_words.eq(self.CAPTURE_WORDS)
                with m.Else():
                    m.d.sync += programmed_len_words.eq(self.i_reg_data)

                # Apply new loop length immediately while playing.
                with m.If(playback_enabled):
                    m.d.sync += [
                        play_word_addr.eq(mem_start_words),
                        phase.eq(mem_start_phase),
                    ]

            with m.If(start_write):
                with m.If(self.i_reg_data >= self.CAPTURE_WORDS):
                    m.d.sync += programmed_start_words.eq(self.CAPTURE_WORDS - 1)
                with m.Else():
                    m.d.sync += programmed_start_words.eq(self.i_reg_data)

                # Apply new loop start immediately while playing.
                with m.If(playback_enabled):
                    m.d.sync += [
                        play_word_addr.eq(mem_start_words),
                        phase.eq(mem_start_phase),
                    ]

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
                        play_word_addr.eq(mem_start_words),
                        phase.eq(mem_start_phase),
                    ]

            # Record transitions are zero-cross aligned.
            with m.If(sample.o_pos_zero_cross):
                with m.If(pending_record_stop & recording):
                    m.d.sync += [
                        recording.eq(0),
                        pending_record_stop.eq(0),
                        capture_flush.eq(1),
                        playback_enabled.eq(valid_words != 0),
                        play_word_addr.eq(mem_start_words),
                        phase.eq(mem_start_phase),
                    ]
                with m.Elif(pending_record_start & ~recording):
                    m.d.sync += [
                        recording.eq(1),
                        pending_record_start.eq(0),
                        playback_enabled.eq(0),
                        capture_count.eq(0),
                        capture_word_ptr.eq(0),
                        valid_words.eq(0),
                        capture_flush.eq(1),
                        capture_acc.eq(0),
                        play_word_addr.eq(mem_start_words),
                        phase.eq(mem_start_phase),
                    ]

            with m.If(
                self.i_sample_tick & recording & (capture_count < self.CAPTURE_BYTES)
            ):
                with m.If(capture_strobe):
                    m.d.sync += [
                        capture_acc.eq(capture_acc - self.CAPTURE_EDGE),
                        capture_count.eq(capture_count + 1),
                    ]

                    with m.If(sample.o_capture_word_written):
                        m.d.sync += [
                            valid_words.eq(capture_word_ptr + 1),
                        ]
                        with m.If(capture_word_ptr < (self.CAPTURE_WORDS - 1)):
                            m.d.sync += capture_word_ptr.eq(capture_word_ptr + 1)

                    with m.If((capture_count + 1) >= self.CAPTURE_BYTES):
                        m.d.sync += [
                            capture_count.eq(self.CAPTURE_BYTES),
                            recording.eq(0),
                            capture_flush.eq(1),
                            pending_record_stop.eq(0),
                            playback_enabled.eq(valid_words != 0),
                            play_word_addr.eq(mem_start_words),
                            phase.eq(mem_start_phase),
                        ]
                with m.Else():
                    m.d.sync += capture_acc.eq(capture_acc + self.CAPTURE_NUM)

            with m.If(dat_write_edge & playback_enabled & (valid_words != 0)):
                with m.If(phase_next >= play_mem_limit_phase):
                    with m.If(self.i_loop_playback):
                        m.d.sync += [
                            phase.eq(play_phase_wrap),
                            play_word_addr.eq(play_phase_wrap[self.FRAC_BITS :]),
                        ]
                    with m.Else():
                        m.d.sync += playback_enabled.eq(0)
                with m.Else():
                    m.d.sync += [
                        phase.eq(phase_next),
                        play_word_addr.eq(phase_next[self.FRAC_BITS :]),
                    ]

            with m.If(recording & (capture_count >= self.CAPTURE_BYTES)):
                m.d.sync += [
                    capture_count.eq(self.CAPTURE_BYTES),
                    recording.eq(0),
                    capture_flush.eq(1),
                    pending_record_stop.eq(0),
                    playback_enabled.eq(valid_words != 0),
                    play_word_addr.eq(mem_start_words),
                    phase.eq(mem_start_phase),
                ]

        return m
