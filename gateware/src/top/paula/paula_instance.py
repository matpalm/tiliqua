from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from tiliqua.dsp import ASQ

from fake_agnus import FakeAgnus
from sample import Sample


class PaulaInstance(wiring.Component):
    """Paula-side owner for sample channels and register-backed control."""

    i_sample0: In(ASQ)
    i_sample1: In(ASQ)
    sample_tick: In(1)

    i_record_evt0: In(1)
    i_record_evt1: In(1)
    i_play_evt0: In(1)
    i_play_evt1: In(1)

    o_sample0: Out(ASQ)
    o_sample1: Out(ASQ)

    def elaborate(self, platform):
        m = Module()

        m.submodules.sample0 = sample0 = Sample()
        m.submodules.sample1 = sample1 = Sample()
        m.submodules.fake_agnus = fake_agnus = FakeAgnus()

        # Initial Paula-style register bank for channel playback control.
        aud_loc0 = Signal(unsigned(16), init=0)
        aud_len0 = Signal(unsigned(16), init=0)
        aud_per0 = Signal(unsigned(16), init=0)
        aud_dmaen0 = Signal(init=0)

        aud_loc1 = Signal(unsigned(16), init=0)
        aud_len1 = Signal(unsigned(16), init=0)
        aud_per1 = Signal(unsigned(16), init=0)
        aud_dmaen1 = Signal(init=0)

        m.d.comb += [
            sample0.i_sample.eq(self.i_sample0),
            sample0.sample_tick.eq(self.sample_tick),
            sample0.record_toggle_evt.eq(self.i_record_evt0),
            sample0.playback_evt.eq(self.i_play_evt0),
            sample1.i_sample.eq(self.i_sample1),
            sample1.sample_tick.eq(self.sample_tick),
            sample1.record_toggle_evt.eq(self.i_record_evt1),
            sample1.playback_evt.eq(self.i_play_evt1),
            self.o_sample0.eq(sample0.o_sample),
            self.o_sample1.eq(sample1.o_sample),
            # Placeholder DMA handshake wiring for the future feeder contract.
            fake_agnus.i_audio_dmal.eq(0),
            fake_agnus.i_audio_dmas.eq(0),
            fake_agnus.i_reg_write.eq(0),
            fake_agnus.i_reg_addr.eq(0),
            fake_agnus.i_reg_data.eq(0),
        ]

        # Playback MIDI events now also update Paula-like registers.
        with m.If(self.i_play_evt0):
            m.d.sync += [
                aud_loc0.eq(0),
                aud_len0.eq(Sample.MAX_CAPTURE_SAMPLES),
                aud_per0.eq(Sample.CAPTURE_DENOM),
                aud_dmaen0.eq(~aud_dmaen0),
            ]

        with m.If(self.i_play_evt1):
            m.d.sync += [
                aud_loc1.eq(0),
                aud_len1.eq(Sample.MAX_CAPTURE_SAMPLES),
                aud_per1.eq(Sample.CAPTURE_DENOM),
                aud_dmaen1.eq(~aud_dmaen1),
            ]

        return m
