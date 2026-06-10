"""Paula Sample-core loop controlled by MIDI record/play notes."""

import json
from pathlib import Path

from amaranth import *
from amaranth.lib import wiring

from tiliqua.build.cli import top_level_cli
from tiliqua.build.types import BitstreamHelp
from tiliqua.dsp import ASQ
from tiliqua.periph import eurorack_pmod
from tiliqua.platform import RebootProvider

from fake_agnus import FakeAgnus
from channel import Channel
from midi import MidiProcessing, RegisterMapping
from paula_audio_wrapper import PaulaAudioWrapper
from sample import Sample

class PaulaTop(Elaboratable):

    PAULA_DFT_PERIOD = 124
    PAULA_MINUS_ONE_OCTAVE = PAULA_DFT_PERIOD // 2
    PAULA_PLUS_ONE_OCTAVE = PAULA_DFT_PERIOD * 2

    # TODO: if going to 3 channels, move these register defns in channel.py

    AUD0LEN = 0x52
    AUD0PER = 0x53
    AUD0VOL = 0x54
    AUD0DAT = 0x55

    AUD1LEN = 0x5A
    AUD1PER = 0x5B
    AUD1VOL = 0x5C
    AUD1DAT = 0x5D

    bitstream_help = BitstreamHelp(
        brief="Paula Sample-core loop (MIDI note control)",
        io_left=[
            "sample0 in",
            "sample1 in",
            "cv reserve 0",
            "",
            "sample0 out",
            "sample1 out",
            "sample1 lp out",
            "",
        ],
        io_right=["", "", "", "", "", "TRS MIDI in"],
    )

    def __init__(self, clock_settings):
        self.clock_settings = clock_settings
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        cfg_path = Path(__file__).with_name("midi_config.json")
        with open(cfg_path, "r") as f:
            midi_cfg = json.loads(f.read())

        m.submodules.car = car = platform.clock_domain_generator(self.clock_settings)
        m.submodules.reboot = reboot = RebootProvider(car.settings.frequencies.sync)

        self.register_mappings = {
            "AUD0VOL": RegisterMapping(
                enc_range=(0, 127), reg_range=(0, 64), reg_init=60
            )
        }

        m.submodules.midi_proc = midi_proc = MidiProcessing(
            midi_channel=midi_cfg["midi_channel"],
            cc_mapping={74: self.register_mappings["AUD0VOL"]},
        )

        m.submodules.pmod0_provider = pmod0_provider = eurorack_pmod.FFCProvider()
        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
            self.clock_settings.audio_clock
        )
        wiring.connect(m, pmod0.pins, pmod0_provider.pins)
        m.d.comb += pmod0.codec_mute.eq(0)

        m.submodules.fake_agnus = fake_agnus = FakeAgnus()
        m.submodules.paudio = paudio = PaulaAudioWrapper()

        channels = []
        for ch in [0, 1]:
            sample_cfg = midi_cfg["samples"][ch]
            channels.append(
                Channel(
                    record_note=sample_cfg["record_note"],
                    play_note=sample_cfg["play_note"],
                )
            )
        m.submodules += channels

        # state of register programming progress
        config_state = Signal(unsigned(4), init=0)
        # paula register address and data buses
        reg_addr = Signal(unsigned(8), init=0)
        reg_data = Signal(unsigned(16), init=0)
        # keep paula reset for initial cycles
        reset_ctr = Signal(range(64), init=63)
        # paula reset signal
        pa_rst = Signal()

        # sync clock down to 7 MHz.
        clk7_div = Signal(range(8), init=0)
        # one-cycle pulse for 7 MHz paula timing
        clk7_en_pulse = Signal(init=0)
        # divider for horizontal strobe timing
        strhor_div = Signal(range(480), init=0)
        # one-cycle horizontal strobe pulse
        strhor_pulse = Signal(init=0)
        # spacing between register writes
        write_hold = Signal(range(2), init=0)
        # num startup AUD0DAT prime writes to do
        dma_prime_writes = Signal(range(16), init=0)
        dma_feed_sel = Signal(init=0)

        # Audio rate handshake pulse.
        sample_tick = Signal()

        # paula left-channel output sample.
        pa_l = Signal(signed(15))
        # paula right-channel output sample.
        pa_r = Signal(signed(15))

        # paula -> ASQ for outbound
        out_shift = max(ASQ.as_shape().width - 15, 0)
        # direct_shift = max(ASQ.as_shape().width - 8, 0)

        m.d.comb += [
            pmod0.o_cal.ready.eq(1),
            pmod0.i_cal.valid.eq(1),
            sample_tick.eq(pmod0.o_cal.valid),
            pa_l.eq(paudio.ldata.as_signed()),
            pa_r.eq(paudio.rdata.as_signed()),
            pa_rst.eq(reset_ctr != 0),
            pmod0.i_cal.payload[0].as_value().eq(pa_l << out_shift),
            pmod0.i_cal.payload[1].as_value().eq(pa_r << out_shift),
            pmod0.i_cal.payload[2].eq(0),
            pmod0.i_cal.payload[3].eq(0),
            paudio.clk7_en.eq(clk7_en_pulse),
            paudio.cck.eq(clk7_en_pulse),
            paudio.rst.eq(pa_rst),
            paudio.strhor.eq(strhor_pulse),
            paudio.reg_address_in.eq(reg_addr),
            paudio.data_in.eq(reg_data),
            # during reset turn off DMA fully, otherwise just have two running
            paudio.dmaena.eq(
                Mux(
                    pa_rst,
                    C(0, 4),
                    C(0b0011, 4),
                )
            ),
            paudio.audpen.eq(0),
            fake_agnus.i_audio_dmal.eq(Cat(paudio.dmal[0], paudio.dmal[1])),
            fake_agnus.i_audio_dmas.eq(Cat(paudio.dmas[0], paudio.dmas[1])),
            fake_agnus.i_sample_tick.eq(sample_tick),
            fake_agnus.i_sample0_word.eq(channels[0].o_sample_word),
            fake_agnus.i_sample1_word.eq(channels[1].o_sample_word),
            fake_agnus.i_reg_write.eq(reg_addr != 0),
            fake_agnus.i_reg_addr.eq(reg_addr),
            fake_agnus.i_reg_data.eq(reg_data),
        ]

        for ch in [0, 1]:
            m.d.comb += [
                channels[ch].i_sample_tick.eq(sample_tick),
                channels[ch].i_sample.eq(pmod0.o_cal.payload[ch].as_value()),
                channels[ch].i_note_on_valid.eq(midi_proc.o_note_on_valid),
                channels[ch].i_note.eq(midi_proc.o_note),
            ]

        with m.If(reset_ctr != 0):
            m.d.sync += reset_ctr.eq(reset_ctr - 1)
        with m.Elif(config_state == 0):
            m.d.sync += config_state.eq(1)

        with m.If(clk7_div == 7):
            m.d.sync += [
                clk7_div.eq(0),
                clk7_en_pulse.eq(1),
            ]
        with m.Else():
            m.d.sync += [
                clk7_div.eq(clk7_div + 1),
                clk7_en_pulse.eq(0),
            ]

        with m.If(clk7_en_pulse):
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
            m.d.sync += strhor_pulse.eq(0)

        with m.If(clk7_en_pulse):
            with m.If(write_hold != 0):
                m.d.sync += write_hold.eq(write_hold - 1)
            with m.Else():
                with m.Switch(config_state):
                    with m.Case(1):
                        m.d.sync += [
                            reg_addr.eq(self.AUD0PER),
                            reg_data.eq(self.PAULA_DFT_PERIOD),
                            config_state.eq(3),  # note: straight to 3
                            write_hold.eq(1),
                        ]
                    # with m.Case(2):
                    #     aud0vol = self.register_mappings["AUD0VOL"]
                    #     m.d.sync += [
                    #         reg_addr.eq(self.AUD0VOL),
                    #         reg_data.eq(aud0vol.target),
                    #         aud0vol.written.eq(aud0vol.target),
                    #         config_state.eq(3),
                    #         write_hold.eq(1),
                    #     ]
                    with m.Case(3):
                        m.d.sync += [
                            reg_addr.eq(self.AUD0LEN),
                            reg_data.eq(Sample.MAX_CAPTURE_SAMPLES),
                            config_state.eq(4),
                            write_hold.eq(1),
                        ]
                    with m.Case(4):
                        m.d.sync += [
                            reg_addr.eq(self.AUD1PER),
                            reg_data.eq(self.PAULA_DFT_PERIOD),
                            config_state.eq(5),
                            write_hold.eq(1),
                        ]
                    with m.Case(5):
                        m.d.sync += [
                            reg_addr.eq(self.AUD1VOL),
                            reg_data.eq(64),
                            config_state.eq(6),
                            write_hold.eq(1),
                        ]
                    with m.Case(6):
                        m.d.sync += [
                            reg_addr.eq(self.AUD1LEN),
                            reg_data.eq(Sample.MAX_CAPTURE_SAMPLES),
                            config_state.eq(7),
                            write_hold.eq(1),
                            dma_prime_writes.eq(8),
                            dma_feed_sel.eq(0),
                        ]
                    with m.Case(7):
                        with m.If(~pa_rst):
                            aud0vol = self.register_mappings["AUD0VOL"]
                            with m.If(aud0vol.target != aud0vol.written):
                                m.d.sync += [
                                    reg_addr.eq(self.AUD0VOL),
                                    reg_data.eq(aud0vol.target),
                                    aud0vol.written.eq(aud0vol.target),
                                    write_hold.eq(1),
                                ]
                            with m.Else():
                                with m.If(dma_prime_writes != 0):
                                    with m.If(dma_feed_sel == 0):
                                        m.d.sync += [
                                            reg_addr.eq(self.AUD0DAT),
                                            reg_data.eq(fake_agnus.o_audio_data0),
                                            write_hold.eq(1),
                                            dma_prime_writes.eq(dma_prime_writes - 1),
                                            dma_feed_sel.eq(1),
                                        ]
                                    with m.Else():
                                        m.d.sync += [
                                            reg_addr.eq(self.AUD1DAT),
                                            reg_data.eq(fake_agnus.o_audio_data1),
                                            write_hold.eq(1),
                                            dma_prime_writes.eq(dma_prime_writes - 1),
                                            dma_feed_sel.eq(0),
                                        ]
                                with m.Else():
                                    with m.If(dma_feed_sel == 0):
                                        m.d.sync += [
                                            reg_addr.eq(self.AUD0DAT),
                                            reg_data.eq(fake_agnus.o_audio_data0),
                                            write_hold.eq(1),
                                            dma_feed_sel.eq(1),
                                        ]
                                    with m.Else():
                                        m.d.sync += [
                                            reg_addr.eq(self.AUD1DAT),
                                            reg_data.eq(fake_agnus.o_audio_data1),
                                            write_hold.eq(1),
                                            dma_feed_sel.eq(0),
                                        ]

        return m


if __name__ == "__main__":
    top_level_cli(PaulaTop, video_core=False)
