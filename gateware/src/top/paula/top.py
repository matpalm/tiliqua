"""Paula Sample-core loop controlled by MIDI record/play notes."""

import json
from pathlib import Path

from amaranth import *
from amaranth.lib import wiring

from tiliqua import midi
from tiliqua.build.cli import top_level_cli
from tiliqua.build.types import BitstreamHelp
from tiliqua.dsp import ASQ
from tiliqua.periph import eurorack_pmod
from tiliqua.platform import RebootProvider

from fake_agnus import FakeAgnus
from paula_audio_wrapper import PaulaAudioWrapper
from sample import Sample

class PaulaTop(Elaboratable):

    PAULA_PERIOD = 124
    USE_FAKE_AGNUS_DMA = True

    AUD0LEN = 0x52
    AUD0PER = 0x53
    AUD0VOL = 0x54
    AUD0DAT = 0x55

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

        cfg_path = Path(__file__).with_name("config.json")
        with open(cfg_path, "r") as f:
            cfg = json.loads(f.read())
        midi_cfg = cfg["samples"]["midi"]
        midi_channel = midi_cfg["channel"]
        record_note = midi_cfg["slots"][0]["record_note"]
        play_note = midi_cfg["slots"][0]["play_note"]

        m.submodules.car = car = platform.clock_domain_generator(self.clock_settings)
        m.submodules.reboot = reboot = RebootProvider(car.settings.frequencies.sync)

        midi_pins = platform.request("midi")
        m.submodules.serialrx = serialrx = midi.SerialRx(
            system_clk_hz=60e6, pins=midi_pins
        )
        m.submodules.midi_decode = midi_decode = midi.MidiDecodeSerial()
        wiring.connect(m, serialrx.o, midi_decode.i)

        m.submodules.pmod0_provider = pmod0_provider = eurorack_pmod.FFCProvider()
        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
            self.clock_settings.audio_clock
        )
        wiring.connect(m, pmod0.pins, pmod0_provider.pins)
        m.d.comb += pmod0.codec_mute.eq(0)

        m.submodules.fake_agnus = fake_agnus = FakeAgnus()
        m.submodules.paudio = paudio = PaulaAudioWrapper()
        m.submodules.sample0 = sample0 = Sample()

        config_state = Signal(unsigned(3), init=0)
        reg_addr = Signal(unsigned(8), init=0)
        reg_data = Signal(unsigned(16), init=0)
        reset_ctr = Signal(range(64), init=63)
        pa_rst = Signal()

        clk7_div = Signal(range(8), init=0)
        clk7_en_pulse = Signal(init=0)
        strhor_div = Signal(range(480), init=0)
        strhor_pulse = Signal(init=0)
        write_hold = Signal(range(2), init=0)
        dma_prime_writes = Signal(range(8), init=0)

        sample_tick = Signal()
        rec_evt = Signal()
        play_evt = Signal()
        in0_sample = Signal(signed(ASQ.as_shape().width))
        sample_shift = max(ASQ.as_shape().width - 8, 0)
        in0_s8 = Signal(signed(8))
        sample0_s8 = Signal(signed(8))
        in0_direct = Signal(signed(ASQ.as_shape().width))
        sample_word = Signal(unsigned(16))
        pa_l = Signal(signed(15))
        out_shift = max(ASQ.as_shape().width - 15, 0)
        direct_shift = max(ASQ.as_shape().width - 8, 0)

        m.d.comb += [
            pmod0.o_cal.ready.eq(1),
            pmod0.i_cal.valid.eq(1),
            sample_tick.eq(pmod0.o_cal.valid),
            in0_sample.eq(pmod0.o_cal.payload[0].as_value()),
            in0_s8.eq(in0_sample >> sample_shift),
            sample0_s8.eq(sample0.o_sample.as_value() >> sample_shift),
            sample_word.eq(Cat(sample0_s8.as_unsigned(), sample0_s8.as_unsigned())),
            in0_direct.eq(in0_s8 << direct_shift),
            pa_l.eq(paudio.ldata.as_signed()),
            pmod0.i_cal.payload[0].as_value().eq(pa_l << out_shift),
            pmod0.i_cal.payload[1].as_value().eq(in0_direct),
            pmod0.i_cal.payload[2].eq(0),
            pmod0.i_cal.payload[3].eq(0),
            paudio.clk7_en.eq(clk7_en_pulse),
            paudio.cck.eq(clk7_en_pulse),
            pa_rst.eq(reset_ctr != 0),
            paudio.rst.eq(pa_rst),
            paudio.strhor.eq(strhor_pulse),
            paudio.reg_address_in.eq(reg_addr),
            paudio.data_in.eq(reg_data),
            paudio.dmaena.eq(
                Mux(
                    pa_rst,
                    C(0, 4),
                    Mux(self.USE_FAKE_AGNUS_DMA, C(0b0001, 4), C(0, 4)),
                )
            ),
            paudio.audpen.eq(0),
            fake_agnus.i_audio_dmal.eq(Cat(paudio.dmal[0], C(0, 1))),
            fake_agnus.i_audio_dmas.eq(Cat(paudio.dmas[0], C(0, 1))),
            fake_agnus.i_sample_tick.eq(sample_tick),
            fake_agnus.i_sample0_word.eq(sample_word),
            fake_agnus.i_sample1_word.eq(0),
            fake_agnus.i_reg_write.eq(reg_addr != 0),
            fake_agnus.i_reg_addr.eq(reg_addr),
            fake_agnus.i_reg_data.eq(reg_data),
            sample0.i_sample.eq(in0_sample),
            sample0.sample_tick.eq(sample_tick),
            sample0.record_toggle_evt.eq(rec_evt),
            sample0.playback_evt.eq(play_evt),
            midi_decode.o.ready.eq(1),
            rec_evt.eq(0),
            play_evt.eq(0),
        ]

        with m.If(midi_decode.o.valid):
            msg = midi_decode.o.payload
            with m.If(
                (msg.status.kind == midi.Status.Kind.NOTE_ON)
                & (msg.status.nibble.channel == midi_channel)
                & (msg.midi_payload.note_on.velocity != 0)
            ):
                with m.If(msg.midi_payload.note_on.note == record_note):
                    m.d.comb += rec_evt.eq(1)
                with m.Elif(msg.midi_payload.note_on.note == play_note):
                    m.d.comb += play_evt.eq(1)

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
                            reg_data.eq(self.PAULA_PERIOD),
                            config_state.eq(2),
                            write_hold.eq(1),
                        ]
                    with m.Case(2):
                        m.d.sync += [
                            reg_addr.eq(self.AUD0VOL),
                            reg_data.eq(64),
                            config_state.eq(3),
                            write_hold.eq(1),
                        ]
                    with m.Case(3):
                        m.d.sync += [
                            reg_addr.eq(self.AUD0LEN),
                            reg_data.eq(Sample.MAX_CAPTURE_SAMPLES),
                            config_state.eq(4),
                            write_hold.eq(1),
                            dma_prime_writes.eq(Mux(self.USE_FAKE_AGNUS_DMA, 4, 0)),
                        ]
                    with m.Case(4):
                        with m.If(~pa_rst):
                            with m.If(self.USE_FAKE_AGNUS_DMA):
                                with m.If(dma_prime_writes != 0):
                                    m.d.sync += [
                                        reg_addr.eq(self.AUD0DAT),
                                        reg_data.eq(fake_agnus.o_audio_data0),
                                        write_hold.eq(1),
                                        dma_prime_writes.eq(dma_prime_writes - 1),
                                    ]
                                with m.Else():
                                    m.d.sync += [
                                        reg_addr.eq(self.AUD0DAT),
                                        reg_data.eq(fake_agnus.o_audio_data0),
                                        write_hold.eq(1),
                                    ]
                            with m.Else():
                                m.d.sync += [
                                    reg_addr.eq(self.AUD0DAT),
                                    reg_data.eq(sample_word),
                                    write_hold.eq(1),
                                ]

        return m


if __name__ == "__main__":
    top_level_cli(PaulaTop, video_core=False)
