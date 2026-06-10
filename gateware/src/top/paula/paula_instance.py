from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from tiliqua.dsp import ASQ

from fake_agnus import FakeAgnus
from paula_audio_wrapper import PaulaAudioWrapper
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

        PAULA_PERIOD_DEFAULT = 124

        AUD0LEN = 0x52
        AUD0PER = 0x53
        AUD0VOL = 0x54
        AUD0DAT = 0x55
        AUD1LEN = 0x5A
        AUD1PER = 0x5B
        AUD1VOL = 0x5C
        AUD1DAT = 0x5D

        m.submodules.sample0 = sample0 = Sample()
        m.submodules.sample1 = sample1 = Sample()
        m.submodules.fake_agnus = fake_agnus = FakeAgnus()
        m.submodules.paudio = paudio = PaulaAudioWrapper()

        # Initial Paula-style register bank for channel playback control.
        aud_loc0 = Signal(unsigned(16), init=0)
        aud_len0 = Signal(unsigned(16), init=0)
        aud_per0 = Signal(unsigned(16), init=0)
        aud_dmaen0 = Signal(init=0)

        aud_loc1 = Signal(unsigned(16), init=0)
        aud_len1 = Signal(unsigned(16), init=0)
        aud_per1 = Signal(unsigned(16), init=0)
        aud_dmaen1 = Signal(init=0)

        aud_dmaen0_prev = Signal(init=0)
        aud_dmaen1_prev = Signal(init=0)
        dma_toggle_evt0 = Signal()
        dma_toggle_evt1 = Signal()
        dma_start_evt0 = Signal()
        dma_start_evt1 = Signal()

        # Simple register write sequencer to set AUDx regs in order.
        cfg_pending0 = Signal(unsigned(2), init=0)
        cfg_pending1 = Signal(unsigned(2), init=0)
        play_target0 = Signal(init=0)
        play_target1 = Signal(init=0)
        reg_addr = Signal(unsigned(8), init=0)
        reg_data = Signal(unsigned(16), init=0)
        dat_phase = Signal(init=0)

        sample_shift = max(ASQ.as_shape().width - 8, 0)
        out_shift = max(ASQ.as_shape().width - 15, 0)
        sample0_q8 = Signal(signed(8))
        sample1_q8 = Signal(signed(8))
        sample0_word = Signal(unsigned(16))
        sample1_word = Signal(unsigned(16))
        pa_l = Signal(signed(15))
        pa_r = Signal(signed(15))

        # Approximate Minimig Paula timing enables in sync domain.
        clk7_div = Signal(range(8), init=0)
        clk7_en_pulse = Signal(init=0)
        cck_phase = Signal(init=0)
        cck_pulse = Signal(init=0)
        strhor_div = Signal(range(480), init=0)
        strhor_pulse = Signal(init=0)

        with m.If(clk7_div == 7):
            m.d.sync += [
                clk7_div.eq(0),
                clk7_en_pulse.eq(1),
                cck_phase.eq(~cck_phase),
            ]
        with m.Else():
            m.d.sync += [
                clk7_div.eq(clk7_div + 1),
                clk7_en_pulse.eq(0),
            ]

        with m.If(clk7_en_pulse):
            with m.If(cck_phase):
                m.d.sync += cck_pulse.eq(1)
            with m.Else():
                m.d.sync += cck_pulse.eq(0)

            with m.If(strhor_div == 479):
                m.d.sync += [
                    strhor_div.eq(0),
                    strhor_pulse.eq(1),
                ]
            with m.Else():
                m.d.sync += [
                    strhor_div.eq(strhor_div + 1),
                    strhor_pulse.eq(0),
                ]
        with m.Else():
            m.d.sync += [
                cck_pulse.eq(0),
                strhor_pulse.eq(0),
            ]

        m.d.comb += [
            dma_toggle_evt0.eq(aud_dmaen0 ^ aud_dmaen0_prev),
            dma_toggle_evt1.eq(aud_dmaen1 ^ aud_dmaen1_prev),
            dma_start_evt0.eq(aud_dmaen0 & ~aud_dmaen0_prev),
            dma_start_evt1.eq(aud_dmaen1 & ~aud_dmaen1_prev),
            sample0_q8.eq(sample0.o_sample.as_value() >> sample_shift),
            sample1_q8.eq(sample1.o_sample.as_value() >> sample_shift),
            sample0_word.eq(Cat(sample0_q8.as_unsigned(), sample0_q8.as_unsigned())),
            sample1_word.eq(Cat(sample1_q8.as_unsigned(), sample1_q8.as_unsigned())),
            pa_l.eq(paudio.ldata.as_signed()),
            pa_r.eq(paudio.rdata.as_signed()),
            sample0.i_sample.eq(self.i_sample0),
            sample0.sample_tick.eq(self.sample_tick),
            sample0.record_toggle_evt.eq(self.i_record_evt0),
            # Sample playback follows Paula voice DMA state changes.
            sample0.playback_evt.eq(dma_toggle_evt0),
            sample1.i_sample.eq(self.i_sample1),
            sample1.sample_tick.eq(self.sample_tick),
            sample1.record_toggle_evt.eq(self.i_record_evt1),
            sample1.playback_evt.eq(dma_toggle_evt1),
            # Route Paula voices through L/R: v0->left(out0), v1->right(out1).
            self.o_sample0.as_value().eq(pa_l << out_shift),
            self.o_sample1.as_value().eq(pa_r << out_shift),
            # Fake Agnus observes Paula's DMA/restart requests for future handoff.
            fake_agnus.i_audio_dmal.eq(Cat(paudio.dmal[0], paudio.dmal[1])),
            fake_agnus.i_audio_dmas.eq(Cat(paudio.dmas[0], paudio.dmas[1])),
            fake_agnus.i_reg_write.eq(reg_addr != 0),
            fake_agnus.i_reg_addr.eq(reg_addr),
            fake_agnus.i_reg_data.eq(reg_data),
            paudio.clk7_en.eq(clk7_en_pulse),
            paudio.cck.eq(cck_pulse),
            paudio.rst.eq(0),
            paudio.strhor.eq(strhor_pulse),
            paudio.reg_address_in.eq(reg_addr),
            paudio.data_in.eq(reg_data),
            # First two Paula voices map to the two sample pads.
            paudio.dmaena.eq(Cat(aud_dmaen0, aud_dmaen1, C(0, 2))),
            paudio.audpen.eq(0),
        ]

        # Playback MIDI events now also update Paula-like registers.
        with m.If(self.i_play_evt0):
            with m.If(aud_dmaen0):
                m.d.sync += [
                    play_target0.eq(0),
                    aud_dmaen0.eq(0),
                    cfg_pending0.eq(0),
                ]
            with m.Else():
                m.d.sync += [
                    aud_loc0.eq(0),
                    aud_len0.eq(Sample.MAX_CAPTURE_SAMPLES),
                    aud_per0.eq(PAULA_PERIOD_DEFAULT),
                    play_target0.eq(1),
                    cfg_pending0.eq(1),
                ]

        with m.If(self.i_play_evt1):
            with m.If(aud_dmaen1):
                m.d.sync += [
                    play_target1.eq(0),
                    aud_dmaen1.eq(0),
                    cfg_pending1.eq(0),
                ]
            with m.Else():
                m.d.sync += [
                    aud_loc1.eq(0),
                    aud_len1.eq(Sample.MAX_CAPTURE_SAMPLES),
                    aud_per1.eq(PAULA_PERIOD_DEFAULT),
                    play_target1.eq(1),
                    cfg_pending1.eq(1),
                ]

        # Channel register/data feed into paula_audio.
        m.d.sync += [
            reg_addr.eq(0),
            reg_data.eq(0),
        ]

        with m.If(self.sample_tick):
            m.d.sync += dat_phase.eq(~dat_phase)
            # Feed paula_audio AUDxDAT continuously from current sample outputs.
            with m.If(dat_phase == 0):
                m.d.sync += [
                    reg_addr.eq(AUD0DAT),
                    reg_data.eq(sample0_word),
                ]
            with m.Else():
                m.d.sync += [
                    reg_addr.eq(AUD1DAT),
                    reg_data.eq(sample1_word),
                ]
            # Background channel setup writes.
            with m.If(cfg_pending0 != 0):
                with m.Switch(cfg_pending0):
                    with m.Case(1):
                        m.d.sync += [
                            reg_addr.eq(AUD0LEN),
                            reg_data.eq(aud_len0),
                            cfg_pending0.eq(2),
                        ]
                    with m.Case(2):
                        m.d.sync += [
                            reg_addr.eq(AUD0PER),
                            reg_data.eq(aud_per0),
                            cfg_pending0.eq(3),
                        ]
                    with m.Case(3):
                        m.d.sync += [
                            reg_addr.eq(AUD0VOL),
                            reg_data.eq(64),
                            cfg_pending0.eq(0),
                            aud_dmaen0.eq(play_target0),
                        ]
            with m.Elif(cfg_pending1 != 0):
                with m.Switch(cfg_pending1):
                    with m.Case(1):
                        m.d.sync += [
                            reg_addr.eq(AUD1LEN),
                            reg_data.eq(aud_len1),
                            cfg_pending1.eq(2),
                        ]
                    with m.Case(2):
                        m.d.sync += [
                            reg_addr.eq(AUD1PER),
                            reg_data.eq(aud_per1),
                            cfg_pending1.eq(3),
                        ]
                    with m.Case(3):
                        m.d.sync += [
                            reg_addr.eq(AUD1VOL),
                            reg_data.eq(64),
                            cfg_pending1.eq(0),
                            aud_dmaen1.eq(play_target1),
                        ]

        m.d.sync += [
            aud_dmaen0_prev.eq(aud_dmaen0),
            aud_dmaen1_prev.eq(aud_dmaen1),
        ]

        return m
