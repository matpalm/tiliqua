"""Paula standalone with MIDI-controlled record/playback on channels 0 and 1.

Record/play notes and CC mappings are loaded from local midi_config.json.
Channel 0 captures from in0 and plays on out0 (left).
Channel 1 captures from in1 and plays on out1 (right).

In CPU-feed mode, AUDxDAT is written by local schedulers paced from AUDxPER,
which removes DMAL snapshot ceiling effects at higher playback rates.
"""

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
from midi import MidiProcessing, RegisterMapping
from paula_audio_wrapper import PaulaAudioWrapper


class Paula2Top(Elaboratable):

    PAULA_CCK_HZ = 60_000_000 // 8
    PAULA_MIN_PERIOD = 121
    PAULA_MAX_PERIOD = 0xFFFF

    # Keep default period aligned with FakeAgnus capture-rate decimation.
    PAULA_BASE_PERIOD = FakeAgnus.DEFAULT_AUD0PER
    PAULA_MIDI_FAST_PERIOD = PAULA_MIN_PERIOD
    PAULA_MIDI_SLOW_PERIOD = PAULA_MAX_PERIOD
    PAULA_LENGTH_WORDS = FakeAgnus.CAPTURE_WORDS
    PAULA_DMA_ENABLE_MASK = 0b0011

    # Local CPU-fed AUDxDAT pacing instead of DMAL-driven writes.
    USE_CPU_FEED = True

    AUD0LEN = 0x52  # cc10  todo
    AUD0PER = 0x53  # cc74
    AUD0VOL = 0x54  # cc71
    AUD0DAT = 0x55

    AUD1LEN = 0x5A  # cc77
    AUD1PER = 0x5B  # cc93
    AUD1VOL = 0x5C  # cc73
    AUD1DAT = 0x5D

    ADKCON = 0x4F

    bitstream_help = BitstreamHelp(
        brief="Paula2 ch0/ch1 note rec/play + MIDI CC volume",
        io_left=[
            "audio in 0",
            "audio in 1",
            "cv reserve 0",
            "",
            "paula out 0",
            "paula out 1",
            "",
            "",
        ],
        io_right=["", "", "", "", "", "TRS MIDI in"],
    )

    def __init__(self, clock_settings):
        self.clock_settings = clock_settings
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.car = car = platform.clock_domain_generator(self.clock_settings)
        m.submodules.reboot = reboot = RebootProvider(car.settings.frequencies.sync)

        with open(Path(__file__).with_name("midi_config.json"), "r") as f:
            midi_cfg = json.loads(f.read())

        record_note0 = midi_cfg["samples"][0]["record_note"]
        play_note0 = midi_cfg["samples"][0]["play_note"]
        record_note1 = midi_cfg["samples"][1]["record_note"]
        play_note1 = midi_cfg["samples"][1]["play_note"]

        period_cc0 = midi_cfg["paula_channels"][0]["period_cc"]
        volume_cc0 = midi_cfg["paula_channels"][0]["volume_cc"]
        period_cc1 = midi_cfg["paula_channels"][1]["period_cc"]
        volume_cc1 = midi_cfg["paula_channels"][1]["volume_cc"]

        adkcon_audio_set_bits = midi_cfg["adkcon_audio_set_bits"] & 0xFF

        self.register_mappings = {
            "AUD0PER": RegisterMapping(
                enc_range=(0, 127),
                reg_range=(self.PAULA_MIDI_FAST_PERIOD, self.PAULA_MIDI_SLOW_PERIOD),
                reg_init=self.PAULA_BASE_PERIOD,
                mapping="exp",
                anchor=(64, self.PAULA_BASE_PERIOD),
            ),
            "AUD1PER": RegisterMapping(
                enc_range=(0, 127),
                reg_range=(self.PAULA_MIDI_FAST_PERIOD, self.PAULA_MIDI_SLOW_PERIOD),
                reg_init=self.PAULA_BASE_PERIOD,
                mapping="exp",
                anchor=(64, self.PAULA_BASE_PERIOD),
            ),
            "AUD0VOL": RegisterMapping(
                enc_range=(0, 127), reg_range=(0, 64), reg_init=64
            ),
            "AUD1VOL": RegisterMapping(
                enc_range=(0, 127), reg_range=(0, 64), reg_init=64
            ),
        }

        cc_mapping = {
            period_cc0: self.register_mappings["AUD0PER"],
            volume_cc0: self.register_mappings["AUD0VOL"],
            period_cc1: self.register_mappings["AUD1PER"],
            volume_cc1: self.register_mappings["AUD1VOL"],
        }

        m.submodules.midi_proc = midi_proc = MidiProcessing(
            midi_channel=int(midi_cfg["midi_channel"]),
            cc_mapping=cc_mapping,
            system_clk_hz=car.settings.frequencies.sync,
        )

        m.submodules.pmod0_provider = pmod0_provider = eurorack_pmod.FFCProvider()
        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
            self.clock_settings.audio_clock
        )
        wiring.connect(m, pmod0.pins, pmod0_provider.pins)

        m.submodules.paudio = paudio = PaulaAudioWrapper()
        m.submodules.fake_agnus0 = fake_agnus0 = FakeAgnus(
            aud_dat_addr=FakeAgnus.AUD0DAT
        )
        m.submodules.fake_agnus1 = fake_agnus1 = FakeAgnus(
            aud_dat_addr=FakeAgnus.AUD1DAT
        )

        config_state = Signal(unsigned(4), init=0)
        reg_addr = Signal(unsigned(8), init=0)
        reg_data = Signal(unsigned(16), init=0)
        reg_write = Signal(init=0)

        reset_ctr = Signal(range(64), init=63)
        pa_rst = Signal()

        clk7_div = Signal(range(8), init=0)
        clk7_en_pulse = Signal(init=0)

        strhor_div = Signal(range(480), init=0)
        strhor_pulse = Signal(init=0)

        write_hold = Signal(range(2), init=0)

        dma_prime_writes = Signal(range(64), init=0)

        aud0per_written = Signal(unsigned(16), init=self.PAULA_BASE_PERIOD)
        aud1per_written = Signal(unsigned(16), init=self.PAULA_BASE_PERIOD)

        # Keep these wide so CPU-feed pacing is not constrained by counter width.
        cpu_feed_ctr0 = Signal(unsigned(32), init=0)
        cpu_feed_ctr1 = Signal(unsigned(32), init=0)
        cpu_feed_due0 = Signal()
        cpu_feed_due1 = Signal()
        cpu_feed_sel = Signal(init=0)

        sample_tick = Signal()
        record_toggle_evt0 = Signal()
        playback_evt0 = Signal()
        record_toggle_evt1 = Signal()
        playback_evt1 = Signal()

        sample_width = ASQ.as_shape().width
        in_shift = max(sample_width - 8, 0)
        raw_in0 = Signal(signed(sample_width))
        raw_in1 = Signal(signed(sample_width))
        in0_s8 = Signal(signed(8))
        in1_s8 = Signal(signed(8))

        pa_l = Signal(signed(15))
        pa_r = Signal(signed(15))
        pa_l_x2 = Signal(signed(16))
        pa_r_x2 = Signal(signed(16))
        pa_l_boost = Signal(signed(15))
        pa_r_boost = Signal(signed(15))
        out_shift = max(ASQ.as_shape().width - 15, 0)

        m.d.comb += [
            pmod0.o_cal.ready.eq(1),
            pmod0.i_cal.valid.eq(1),
            pmod0.codec_mute.eq(0),
            sample_tick.eq(pmod0.o_cal.valid),
            record_toggle_evt0.eq(
                midi_proc.o_note_on_valid & (midi_proc.o_note == record_note0)
            ),
            playback_evt0.eq(
                midi_proc.o_note_on_valid & (midi_proc.o_note == play_note0)
            ),
            record_toggle_evt1.eq(
                midi_proc.o_note_on_valid & (midi_proc.o_note == record_note1)
            ),
            playback_evt1.eq(
                midi_proc.o_note_on_valid & (midi_proc.o_note == play_note1)
            ),
            raw_in0.eq(pmod0.o_cal.payload[0].as_value()),
            raw_in1.eq(pmod0.o_cal.payload[1].as_value()),
            in0_s8.eq(raw_in0 >> in_shift),
            in1_s8.eq(raw_in1 >> in_shift),
            pa_l.eq(paudio.ldata.as_signed()),
            pa_r.eq(paudio.rdata.as_signed()),
            pa_l_x2.eq(pa_l.as_signed() << 1),
            pa_r_x2.eq(pa_r.as_signed() << 1),
            pa_l_boost.eq(
                Mux(
                    pa_l_x2 > C(16383, signed(16)),
                    C(16383, signed(16)),
                    Mux(
                        pa_l_x2 < C(-16384, signed(16)),
                        C(-16384, signed(16)),
                        pa_l_x2,
                    ),
                )
            ),
            pa_r_boost.eq(
                Mux(
                    pa_r_x2 > C(16383, signed(16)),
                    C(16383, signed(16)),
                    Mux(
                        pa_r_x2 < C(-16384, signed(16)),
                        C(-16384, signed(16)),
                        pa_r_x2,
                    ),
                )
            ),
            pa_rst.eq(reset_ctr != 0),
            pmod0.i_cal.payload[0].as_value().eq(pa_l_boost << out_shift),
            pmod0.i_cal.payload[1].as_value().eq(pa_r_boost << out_shift),
            pmod0.i_cal.payload[2].as_value().eq(0),
            pmod0.i_cal.payload[3].as_value().eq(0),
            paudio.clk7_en.eq(clk7_en_pulse),
            paudio.cck.eq(clk7_en_pulse),
            paudio.rst.eq(pa_rst),
            paudio.strhor.eq(strhor_pulse),
            paudio.reg_address_in.eq(reg_addr),
            paudio.data_in.eq(reg_data),
            paudio.dmaena.eq(
                Mux(
                    pa_rst,
                    C(0, 4),
                    C(0, 4) if self.USE_CPU_FEED else C(self.PAULA_DMA_ENABLE_MASK, 4),
                )
            ),
            paudio.audpen.eq(0),
            fake_agnus0.i_reset.eq(reset_ctr != 0),
            fake_agnus0.i_audio_dmal.eq(paudio.dmal[0]),
            fake_agnus0.i_audio_dmas.eq(paudio.dmas[0]),
            fake_agnus0.i_record_toggle_evt.eq(record_toggle_evt0),
            fake_agnus0.i_playback_evt.eq(playback_evt0),
            fake_agnus0.i_sample_tick.eq(sample_tick),
            fake_agnus0.i_sample_in.eq(in0_s8.as_unsigned()),
            fake_agnus0.i_reg_write.eq(reg_write),
            fake_agnus0.i_reg_addr.eq(reg_addr),
            fake_agnus0.i_reg_data.eq(reg_data),
            fake_agnus1.i_reset.eq(reset_ctr != 0),
            fake_agnus1.i_audio_dmal.eq(paudio.dmal[1]),
            fake_agnus1.i_audio_dmas.eq(paudio.dmas[1]),
            fake_agnus1.i_record_toggle_evt.eq(record_toggle_evt1),
            fake_agnus1.i_playback_evt.eq(playback_evt1),
            fake_agnus1.i_sample_tick.eq(sample_tick),
            fake_agnus1.i_sample_in.eq(in1_s8.as_unsigned()),
            fake_agnus1.i_reg_write.eq(reg_write),
            fake_agnus1.i_reg_addr.eq(reg_addr),
            fake_agnus1.i_reg_data.eq(reg_data),
            cpu_feed_due0.eq(cpu_feed_ctr0 >= ((aud0per_written << 1) - 1)),
            cpu_feed_due1.eq(cpu_feed_ctr1 >= ((aud1per_written << 1) - 1)),
            strhor_pulse.eq(clk7_en_pulse & (strhor_div == 479)),
        ]

        with m.If(reset_ctr != 0):
            m.d.sync += reset_ctr.eq(reset_ctr - 1)
        with m.Elif((config_state == 0) & (~pa_rst)):
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
                m.d.sync += strhor_div.eq(0)
            with m.Else():
                m.d.sync += strhor_div.eq(strhor_div + 1)

        with m.If(pa_rst):
            m.d.sync += [
                aud0per_written.eq(self.PAULA_BASE_PERIOD),
                aud1per_written.eq(self.PAULA_BASE_PERIOD),
                cpu_feed_ctr0.eq(0),
                cpu_feed_ctr1.eq(0),
                cpu_feed_sel.eq(0),
            ]

        with m.If(clk7_en_pulse):
            m.d.sync += [
                reg_addr.eq(0),
                reg_data.eq(0),
                reg_write.eq(0),
            ]

            with m.If(write_hold != 0):
                m.d.sync += write_hold.eq(write_hold - 1)
            with m.Else():
                with m.Switch(config_state):
                    with m.Case(1):
                        m.d.sync += [
                            reg_addr.eq(self.ADKCON),
                            reg_data.eq(0x8000 | adkcon_audio_set_bits),
                            reg_write.eq(1),
                            config_state.eq(2),
                            write_hold.eq(1),
                        ]
                    with m.Case(2):
                        m.d.sync += [
                            reg_addr.eq(self.AUD0PER),
                            reg_data.eq(self.PAULA_BASE_PERIOD),
                            reg_write.eq(1),
                            config_state.eq(3),
                            write_hold.eq(1),
                        ]
                    with m.Case(3):
                        m.d.sync += [
                            reg_addr.eq(self.AUD0VOL),
                            reg_data.eq(64),
                            reg_write.eq(1),
                            config_state.eq(4),
                            write_hold.eq(1),
                        ]
                    with m.Case(4):
                        m.d.sync += [
                            reg_addr.eq(self.AUD0LEN),
                            reg_data.eq(self.PAULA_LENGTH_WORDS),
                            reg_write.eq(1),
                            config_state.eq(5),
                            write_hold.eq(1),
                        ]
                    with m.Case(5):
                        m.d.sync += [
                            reg_addr.eq(self.AUD1PER),
                            reg_data.eq(self.PAULA_BASE_PERIOD),
                            reg_write.eq(1),
                            config_state.eq(6),
                            write_hold.eq(1),
                        ]
                    with m.Case(6):
                        m.d.sync += [
                            reg_addr.eq(self.AUD1VOL),
                            reg_data.eq(64),
                            reg_write.eq(1),
                            config_state.eq(7),
                            write_hold.eq(1),
                        ]
                    with m.Case(7):
                        m.d.sync += [
                            reg_addr.eq(self.AUD1LEN),
                            reg_data.eq(self.PAULA_LENGTH_WORDS),
                            reg_write.eq(1),
                            config_state.eq(8),
                            dma_prime_writes.eq(16 if self.USE_CPU_FEED else 12),
                            write_hold.eq(1),
                        ]
                    with m.Case(8):
                        aud0per = self.register_mappings["AUD0PER"]
                        aud1per = self.register_mappings["AUD1PER"]
                        aud0vol = self.register_mappings["AUD0VOL"]
                        aud1vol = self.register_mappings["AUD1VOL"]

                        with m.If(aud0per.target != aud0per.written):
                            m.d.sync += [
                                reg_addr.eq(self.AUD0PER),
                                reg_data.eq(aud0per.target),
                                reg_write.eq(1),
                                aud0per.written.eq(aud0per.target),
                                aud0per_written.eq(aud0per.target),
                                cpu_feed_ctr0.eq(0),
                                write_hold.eq(0 if self.USE_CPU_FEED else 1),
                            ]
                        with m.Elif(aud1per.target != aud1per.written):
                            m.d.sync += [
                                reg_addr.eq(self.AUD1PER),
                                reg_data.eq(aud1per.target),
                                reg_write.eq(1),
                                aud1per.written.eq(aud1per.target),
                                aud1per_written.eq(aud1per.target),
                                cpu_feed_ctr1.eq(0),
                                write_hold.eq(0 if self.USE_CPU_FEED else 1),
                            ]
                        with m.Elif(aud0vol.target != aud0vol.written):
                            m.d.sync += [
                                reg_addr.eq(self.AUD0VOL),
                                reg_data.eq(aud0vol.target),
                                reg_write.eq(1),
                                aud0vol.written.eq(aud0vol.target),
                                write_hold.eq(0 if self.USE_CPU_FEED else 1),
                            ]
                        with m.Elif(aud1vol.target != aud1vol.written):
                            m.d.sync += [
                                reg_addr.eq(self.AUD1VOL),
                                reg_data.eq(aud1vol.target),
                                reg_write.eq(1),
                                aud1vol.written.eq(aud1vol.target),
                                write_hold.eq(0 if self.USE_CPU_FEED else 1),
                            ]
                        with m.Elif(self.USE_CPU_FEED & (dma_prime_writes != 0)):
                            with m.If(cpu_feed_sel == 0):
                                m.d.sync += [
                                    reg_addr.eq(self.AUD0DAT),
                                    reg_data.eq(fake_agnus0.o_audio_data0),
                                    reg_write.eq(1),
                                    dma_prime_writes.eq(dma_prime_writes - 1),
                                    cpu_feed_ctr0.eq(0),
                                    cpu_feed_sel.eq(1),
                                    write_hold.eq(0),
                                ]
                            with m.Else():
                                m.d.sync += [
                                    reg_addr.eq(self.AUD1DAT),
                                    reg_data.eq(fake_agnus1.o_audio_data0),
                                    reg_write.eq(1),
                                    dma_prime_writes.eq(dma_prime_writes - 1),
                                    cpu_feed_ctr1.eq(0),
                                    cpu_feed_sel.eq(0),
                                    write_hold.eq(0),
                                ]
                        with m.Elif(self.USE_CPU_FEED & cpu_feed_due0 & cpu_feed_due1):
                            with m.If(cpu_feed_sel == 0):
                                m.d.sync += [
                                    reg_addr.eq(self.AUD0DAT),
                                    reg_data.eq(fake_agnus0.o_audio_data0),
                                    reg_write.eq(1),
                                    cpu_feed_ctr0.eq(0),
                                    cpu_feed_sel.eq(1),
                                    write_hold.eq(0),
                                ]
                            with m.Else():
                                m.d.sync += [
                                    reg_addr.eq(self.AUD1DAT),
                                    reg_data.eq(fake_agnus1.o_audio_data0),
                                    reg_write.eq(1),
                                    cpu_feed_ctr1.eq(0),
                                    cpu_feed_sel.eq(0),
                                    write_hold.eq(0),
                                ]
                        with m.Elif(self.USE_CPU_FEED & cpu_feed_due0):
                            m.d.sync += [
                                reg_addr.eq(self.AUD0DAT),
                                reg_data.eq(fake_agnus0.o_audio_data0),
                                reg_write.eq(1),
                                cpu_feed_ctr0.eq(0),
                                write_hold.eq(0),
                            ]
                        with m.Elif(self.USE_CPU_FEED & cpu_feed_due1):
                            m.d.sync += [
                                reg_addr.eq(self.AUD1DAT),
                                reg_data.eq(fake_agnus1.o_audio_data0),
                                reg_write.eq(1),
                                cpu_feed_ctr1.eq(0),
                                write_hold.eq(0),
                            ]
                        with m.Elif(self.USE_CPU_FEED):
                            m.d.sync += [
                                cpu_feed_ctr0.eq(cpu_feed_ctr0 + 1),
                                cpu_feed_ctr1.eq(cpu_feed_ctr1 + 1),
                            ]

        return m


if __name__ == "__main__":
    top_level_cli(Paula2Top, video_core=False)
