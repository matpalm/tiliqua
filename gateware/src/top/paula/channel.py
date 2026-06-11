from amaranth import *

from tiliqua.dsp import ASQ

from sample import Sample


class Channel(Elaboratable):
    """Per-channel sample capture/playback and Paula word formatting."""

    def __init__(self, record_note: int, play_note: int):
        self.sample_width = ASQ.as_shape().width
        self.record_note = record_note
        self.play_note = play_note

        self.i_sample_tick = Signal()
        self.i_sample = Signal(signed(self.sample_width))

        self.i_note_on_valid = Signal()
        self.i_note = Signal(unsigned(8))

        self.o_sample_word = Signal(unsigned(16))

    def elaborate(self, platform):
        m = Module()

        m.submodules.sample = sample = Sample()

        rec_evt = Signal()
        play_evt = Signal()

        sample_shift = max(self.sample_width - 8, 0)
        sample_s8 = Signal(signed(8))

        m.d.comb += [
            rec_evt.eq(self.i_note_on_valid & (self.i_note == self.record_note)),
            play_evt.eq(self.i_note_on_valid & (self.i_note == self.play_note)),
            sample.i_sample.eq(self.i_sample),
            sample.sample_tick.eq(self.i_sample_tick),
            sample.record_start_evt.eq(rec_evt),
            sample.playback_evt.eq(play_evt),
            sample_s8.eq(sample.o_sample.as_value() >> sample_shift),
            self.o_sample_word.eq(
                Cat(sample_s8.as_unsigned(), sample_s8.as_unsigned())
            ),
        ]

        return m
