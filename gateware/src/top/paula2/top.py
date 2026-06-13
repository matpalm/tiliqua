"""Paula standalone with MIDI-controlled record/playback on channels 0 -> 3

Record/play notes and CC mappings are loaded from local midi_config.json.

Channel N captures from inN.

AUDxDAT is serviced from Paula DMA requests, with startup priming writes.

Outputs:
- out0: Paula left mix (ch0 + ch3)
- out1: Paula right mix (ch1 + ch2)
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

    NUM_CH = 3

    PAULA_CCK_HZ = 60_000_000 // 8
    PAULA_MIN_PERIOD = 121
    PAULA_MAX_PERIOD = 0xFFFF

    # Keep default period aligned with FakeAgnus capture-rate decimation.
    PAULA_BASE_PERIOD = FakeAgnus.DEFAULT_AUD0PER
    PAULA_MIDI_FAST_PERIOD = PAULA_MIN_PERIOD
    PAULA_MIDI_SLOW_PERIOD = PAULA_MAX_PERIOD
    PAULA_LENGTH_WORDS = FakeAgnus.CAPTURE_WORDS
    PAULA_DMA_ENABLE_MASK = 0b0111

    AUD0LEN = 0x52  # cc10
    AUD0PER = 0x53  # cc74
    AUD0VOL = 0x54  # cc71
    AUD0DAT = 0x55

    AUD1LEN = 0x5A  # cc77
    AUD1PER = 0x5B  # cc93
    AUD1VOL = 0x5C  # cc73
    AUD1DAT = 0x5D

    AUD2LEN = 0x62  # cc114
    AUD2PER = 0x63  # cc18
    AUD2VOL = 0x64  # cc19
    AUD2DAT = 0x65

    AUD3LEN = 0x6A  # cc17
    AUD3PER = 0x6B  # cc91
    AUD3VOL = 0x6C  # cc79
    AUD3DAT = 0x6D

    ADKCON = 0x4F

    bitstream_help = BitstreamHelp(
        brief="Paula2",
        io_left=[
            "audio in 0",
            "audio in 1",
            "audio in 2",
            "audio in 3",
            "paula out L",
            "paula out R",
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
        record_note2 = midi_cfg["samples"][2]["record_note"]
        play_note2 = midi_cfg["samples"][2]["play_note"]

        period_cc0 = midi_cfg["paula_channels"][0]["period_cc"]
        volume_cc0 = midi_cfg["paula_channels"][0]["volume_cc"]
        length_cc0 = midi_cfg["paula_channels"][0]["length_cc"]
        period_cc1 = midi_cfg["paula_channels"][1]["period_cc"]
        volume_cc1 = midi_cfg["paula_channels"][1]["volume_cc"]
        length_cc1 = midi_cfg["paula_channels"][1]["length_cc"]
        period_cc2 = midi_cfg["paula_channels"][2]["period_cc"]
        volume_cc2 = midi_cfg["paula_channels"][2]["volume_cc"]
        length_cc2 = midi_cfg["paula_channels"][2]["length_cc"]

        atvol0_note = midi_cfg["modulation"][0]["toggle_volume_note"]
        atper0_note = midi_cfg["modulation"][0]["toggle_period_note"]
        atvol1_note = midi_cfg["modulation"][1]["toggle_volume_note"]
        atper1_note = midi_cfg["modulation"][1]["toggle_period_note"]

        reset_cfg = midi_cfg.get("reset", {})
        reset_audx_note = reset_cfg.get("AUDx???")
        reset_atxxx_note = reset_cfg.get("AT???x")

        adkcon_audio_set_bits = int(midi_cfg.get("adkcon_audio_set_bits", 0)) & 0xFF

        self.register_mappings = {
            "AUD0PER": RegisterMapping(
                enc_range=(0, 127),
                reg_range=(self.PAULA_MIDI_SLOW_PERIOD, self.PAULA_MIDI_FAST_PERIOD),
                reg_init=self.PAULA_BASE_PERIOD,
                mapping="exp",
                anchor=(64, self.PAULA_BASE_PERIOD),
            ),
            "AUD1PER": RegisterMapping(
                enc_range=(0, 127),
                reg_range=(self.PAULA_MIDI_SLOW_PERIOD, self.PAULA_MIDI_FAST_PERIOD),
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
            "AUD2PER": RegisterMapping(
                enc_range=(0, 127),
                reg_range=(self.PAULA_MIDI_SLOW_PERIOD, self.PAULA_MIDI_FAST_PERIOD),
                reg_init=self.PAULA_BASE_PERIOD,
                mapping="exp",
                anchor=(64, self.PAULA_BASE_PERIOD),
            ),
            "AUD2VOL": RegisterMapping(
                enc_range=(0, 127), reg_range=(0, 64), reg_init=64
            ),
            "AUD0LEN": RegisterMapping(
                enc_range=(0, 127),
                reg_range=(1, self.PAULA_LENGTH_WORDS),
                reg_init=self.PAULA_LENGTH_WORDS,
            ),
            "AUD1LEN": RegisterMapping(
                enc_range=(0, 127),
                reg_range=(1, self.PAULA_LENGTH_WORDS),
                reg_init=self.PAULA_LENGTH_WORDS,
            ),
            "AUD2LEN": RegisterMapping(
                enc_range=(0, 127),
                reg_range=(1, self.PAULA_LENGTH_WORDS),
                reg_init=self.PAULA_LENGTH_WORDS,
            ),
        }

        cc_mapping = {
            period_cc0: self.register_mappings["AUD0PER"],
            volume_cc0: self.register_mappings["AUD0VOL"],
            length_cc0: self.register_mappings["AUD0LEN"],
            period_cc1: self.register_mappings["AUD1PER"],
            volume_cc1: self.register_mappings["AUD1VOL"],
            length_cc1: self.register_mappings["AUD1LEN"],
            period_cc2: self.register_mappings["AUD2PER"],
            volume_cc2: self.register_mappings["AUD2VOL"],
            length_cc2: self.register_mappings["AUD2LEN"],
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

        fake_agni = []
        fake_agni.append(
            FakeAgnus(
                aud_dat_addr=FakeAgnus.AUD0DAT,
                aud_len_addr=FakeAgnus.AUD0LEN,
            )
        )
        fake_agni.append(
            FakeAgnus(
                aud_dat_addr=FakeAgnus.AUD1DAT,
                aud_len_addr=FakeAgnus.AUD1LEN,
            )
        )
        fake_agni.append(
            FakeAgnus(
                aud_dat_addr=FakeAgnus.AUD2DAT,
                aud_len_addr=FakeAgnus.AUD2LEN,
            )
        )
        assert len(fake_agni) == self.NUM_CH
        m.submodules += fake_agni

        fake_agnus0 = fake_agni[0]
        fake_agnus1 = fake_agni[1]
        fake_agnus2 = fake_agni[2]

        # m.submodules.fake_agnus2 = fake_agnus2 = FakeAgnus(
        #     aud_dat_addr=FakeAgnus.AUD2DAT,
        #     aud_len_addr=FakeAgnus.AUD2LEN,
        # )

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

        dma_feed_sel = Signal(range(3), init=0)
        dma_req_pending = Signal(unsigned(3), init=0)

        adkcon_target_bits = Signal(unsigned(8), init=adkcon_audio_set_bits)
        adkcon_written_bits = Signal(unsigned(8), init=0)
        adkcon_bits_to_set = Signal(unsigned(8))
        adkcon_bits_to_clear = Signal(unsigned(8))
        adkcon_toggle_mask = Signal(unsigned(8))

        sample_tick = Signal()

        record_toggle_evts = [Signal() for _ in range(self.NUM_CH)]
        playback_evts = [Signal() for _ in range(self.NUM_CH)]

        record_toggle_evt0 = record_toggle_evts[0]
        playback_evt0 = playback_evts[0]
        record_toggle_evt1 = record_toggle_evts[1]
        playback_evt1 = playback_evts[1]
        record_toggle_evt2 = record_toggle_evts[2]
        playback_evt2 = playback_evts[2]

        reset_audx_evt = Signal()
        reset_atxxx_evt = Signal()

        sample_width = ASQ.as_shape().width
        in_shift = max(sample_width - 8, 0)

        raw_ins = [Signal(signed(sample_width)) for _ in range(self.NUM_CH)]
        raw_in0 = raw_ins[0]
        raw_in1 = raw_ins[1]
        raw_in2 = raw_ins[2]

        inN_s8 = [Signal(signed(8)) for _ in range(self.NUM_CH)]
        in0_s8 = inN_s8[0]
        in1_s8 = inN_s8[1]
        in2_s8 = inN_s8[2]

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
            record_toggle_evt2.eq(
                midi_proc.o_note_on_valid & (midi_proc.o_note == record_note2)
            ),
            playback_evt2.eq(
                midi_proc.o_note_on_valid & (midi_proc.o_note == play_note2)
            ),
            reset_audx_evt.eq(
                C(0, 1)
                if reset_audx_note is None
                else (
                    midi_proc.o_note_on_valid
                    & (midi_proc.o_note == int(reset_audx_note))
                )
            ),
            reset_atxxx_evt.eq(
                C(0, 1)
                if reset_atxxx_note is None
                else (
                    midi_proc.o_note_on_valid
                    & (midi_proc.o_note == int(reset_atxxx_note))
                )
            ),
            self.register_mappings["AUD0PER"].reset_to_init.eq(reset_audx_evt),
            self.register_mappings["AUD1PER"].reset_to_init.eq(reset_audx_evt),
            self.register_mappings["AUD2PER"].reset_to_init.eq(reset_audx_evt),
            self.register_mappings["AUD0VOL"].reset_to_init.eq(reset_audx_evt),
            self.register_mappings["AUD1VOL"].reset_to_init.eq(reset_audx_evt),
            self.register_mappings["AUD2VOL"].reset_to_init.eq(reset_audx_evt),
            self.register_mappings["AUD0LEN"].reset_to_init.eq(reset_audx_evt),
            self.register_mappings["AUD1LEN"].reset_to_init.eq(reset_audx_evt),
            self.register_mappings["AUD2LEN"].reset_to_init.eq(reset_audx_evt),
            raw_in0.eq(pmod0.o_cal.payload[0].as_value()),
            raw_in1.eq(pmod0.o_cal.payload[1].as_value()),
            raw_in2.eq(pmod0.o_cal.payload[2].as_value()),
            in0_s8.eq(raw_in0 >> in_shift),
            in1_s8.eq(raw_in1 >> in_shift),
            in2_s8.eq(raw_in2 >> in_shift),
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
                    C(self.PAULA_DMA_ENABLE_MASK, 4),
                )
            ),
            paudio.audpen.eq(0),
        ]

        m.d.comb += [
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
            fake_agnus2.i_reset.eq(reset_ctr != 0),
            fake_agnus2.i_audio_dmal.eq(paudio.dmal[2]),
            fake_agnus2.i_audio_dmas.eq(paudio.dmas[2]),
            fake_agnus2.i_record_toggle_evt.eq(record_toggle_evt2),
            fake_agnus2.i_playback_evt.eq(playback_evt2),
            fake_agnus2.i_sample_tick.eq(sample_tick),
            fake_agnus2.i_sample_in.eq(in2_s8.as_unsigned()),
            fake_agnus2.i_reg_write.eq(reg_write),
            fake_agnus2.i_reg_addr.eq(reg_addr),
            fake_agnus2.i_reg_data.eq(reg_data),
        ]

        m.d.comb += [
            adkcon_bits_to_set.eq(adkcon_target_bits & ~adkcon_written_bits),
            adkcon_bits_to_clear.eq(adkcon_written_bits & ~adkcon_target_bits),
            strhor_pulse.eq(clk7_en_pulse & (strhor_div == 479)),
        ]
        toggle_mask_val = 0
        if atvol0_note is not None:
            toggle_mask_val = toggle_mask_val | (
                Mux(
                    midi_proc.o_note_on_valid & (midi_proc.o_note == int(atvol0_note)),
                    C(0x01, 8),
                    C(0x00, 8),
                )
            )
        if atper0_note is not None:
            toggle_mask_val = toggle_mask_val | (
                Mux(
                    midi_proc.o_note_on_valid & (midi_proc.o_note == int(atper0_note)),
                    C(0x10, 8),
                    C(0x00, 8),
                )
            )
        if atvol1_note is not None:
            toggle_mask_val = toggle_mask_val | (
                Mux(
                    midi_proc.o_note_on_valid & (midi_proc.o_note == int(atvol1_note)),
                    C(0x02, 8),
                    C(0x00, 8),
                )
            )
        if atper1_note is not None:
            toggle_mask_val = toggle_mask_val | (
                Mux(
                    midi_proc.o_note_on_valid & (midi_proc.o_note == int(atper1_note)),
                    C(0x20, 8),
                    C(0x00, 8),
                )
            )
        m.d.comb += adkcon_toggle_mask.eq(toggle_mask_val)

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
                dma_feed_sel.eq(0),
                dma_req_pending.eq(0),
                adkcon_target_bits.eq(adkcon_audio_set_bits),
                adkcon_written_bits.eq(0),
            ]
        with m.Elif(reset_atxxx_evt):
            m.d.sync += adkcon_target_bits.eq(0)
        with m.Elif(adkcon_toggle_mask != 0):
            m.d.sync += adkcon_target_bits.eq(adkcon_target_bits ^ adkcon_toggle_mask)

        with m.If(clk7_en_pulse & ~pa_rst):
            with m.If(strhor_pulse):
                m.d.sync += dma_req_pending.eq(
                    dma_req_pending
                    | Cat(paudio.dmal[0], paudio.dmal[1], paudio.dmal[2])
                )

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
                            adkcon_written_bits.eq(adkcon_audio_set_bits),
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
                            dma_feed_sel.eq(0),
                            write_hold.eq(1),
                        ]
                    with m.Case(8):
                        m.d.sync += [
                            reg_addr.eq(self.AUD2PER),
                            reg_data.eq(self.PAULA_BASE_PERIOD),
                            reg_write.eq(1),
                            config_state.eq(9),
                            write_hold.eq(1),
                        ]
                    with m.Case(9):
                        m.d.sync += [
                            reg_addr.eq(self.AUD2VOL),
                            reg_data.eq(64),
                            reg_write.eq(1),
                            config_state.eq(10),
                            write_hold.eq(1),
                        ]
                    with m.Case(10):
                        m.d.sync += [
                            reg_addr.eq(self.AUD2LEN),
                            reg_data.eq(self.PAULA_LENGTH_WORDS),
                            reg_write.eq(1),
                            config_state.eq(11),
                            dma_prime_writes.eq(18),
                            dma_feed_sel.eq(0),
                            write_hold.eq(1),
                        ]
                    with m.Case(11):
                        aud0per = self.register_mappings["AUD0PER"]
                        aud1per = self.register_mappings["AUD1PER"]
                        aud2per = self.register_mappings["AUD2PER"]
                        aud0vol = self.register_mappings["AUD0VOL"]
                        aud1vol = self.register_mappings["AUD1VOL"]
                        aud2vol = self.register_mappings["AUD2VOL"]
                        aud0len = self.register_mappings["AUD0LEN"]
                        aud1len = self.register_mappings["AUD1LEN"]
                        aud2len = self.register_mappings["AUD2LEN"]

                        with m.If(adkcon_bits_to_set != 0):
                            m.d.sync += [
                                reg_addr.eq(self.ADKCON),
                                reg_data.eq(0x8000 | adkcon_bits_to_set),
                                reg_write.eq(1),
                                adkcon_written_bits.eq(
                                    adkcon_written_bits | adkcon_bits_to_set
                                ),
                                write_hold.eq(1),
                            ]
                        with m.Elif(adkcon_bits_to_clear != 0):
                            m.d.sync += [
                                reg_addr.eq(self.ADKCON),
                                reg_data.eq(adkcon_bits_to_clear),
                                reg_write.eq(1),
                                adkcon_written_bits.eq(
                                    adkcon_written_bits & (~adkcon_bits_to_clear)
                                ),
                                write_hold.eq(1),
                            ]
                        with m.Elif(aud0per.target != aud0per.written):
                            m.d.sync += [
                                reg_addr.eq(self.AUD0PER),
                                reg_data.eq(aud0per.target),
                                reg_write.eq(1),
                                aud0per.written.eq(aud0per.target),
                                write_hold.eq(1),
                            ]
                        with m.Elif(aud1per.target != aud1per.written):
                            m.d.sync += [
                                reg_addr.eq(self.AUD1PER),
                                reg_data.eq(aud1per.target),
                                reg_write.eq(1),
                                aud1per.written.eq(aud1per.target),
                                write_hold.eq(1),
                            ]
                        with m.Elif(aud2per.target != aud2per.written):
                            m.d.sync += [
                                reg_addr.eq(self.AUD2PER),
                                reg_data.eq(aud2per.target),
                                reg_write.eq(1),
                                aud2per.written.eq(aud2per.target),
                                write_hold.eq(1),
                            ]
                        with m.Elif(aud0vol.target != aud0vol.written):
                            m.d.sync += [
                                reg_addr.eq(self.AUD0VOL),
                                reg_data.eq(aud0vol.target),
                                reg_write.eq(1),
                                aud0vol.written.eq(aud0vol.target),
                                write_hold.eq(1),
                            ]
                        with m.Elif(aud1vol.target != aud1vol.written):
                            m.d.sync += [
                                reg_addr.eq(self.AUD1VOL),
                                reg_data.eq(aud1vol.target),
                                reg_write.eq(1),
                                aud1vol.written.eq(aud1vol.target),
                                write_hold.eq(1),
                            ]
                        with m.Elif(aud2vol.target != aud2vol.written):
                            m.d.sync += [
                                reg_addr.eq(self.AUD2VOL),
                                reg_data.eq(aud2vol.target),
                                reg_write.eq(1),
                                aud2vol.written.eq(aud2vol.target),
                                write_hold.eq(1),
                            ]
                        with m.Elif(aud0len.target != aud0len.written):
                            m.d.sync += [
                                reg_addr.eq(self.AUD0LEN),
                                reg_data.eq(aud0len.target),
                                reg_write.eq(1),
                                aud0len.written.eq(aud0len.target),
                                write_hold.eq(1),
                            ]
                        with m.Elif(aud1len.target != aud1len.written):
                            m.d.sync += [
                                reg_addr.eq(self.AUD1LEN),
                                reg_data.eq(aud1len.target),
                                reg_write.eq(1),
                                aud1len.written.eq(aud1len.target),
                                write_hold.eq(1),
                            ]
                        with m.Elif(aud2len.target != aud2len.written):
                            m.d.sync += [
                                reg_addr.eq(self.AUD2LEN),
                                reg_data.eq(aud2len.target),
                                reg_write.eq(1),
                                aud2len.written.eq(aud2len.target),
                                write_hold.eq(1),
                            ]
                        with m.Elif(dma_prime_writes != 0):
                            with m.If(dma_feed_sel == 0):
                                m.d.sync += [
                                    reg_addr.eq(self.AUD0DAT),
                                    reg_data.eq(fake_agnus0.o_audio_data0),
                                    reg_write.eq(1),
                                    dma_prime_writes.eq(dma_prime_writes - 1),
                                    dma_feed_sel.eq(1),
                                    write_hold.eq(1),
                                ]
                            with m.Elif(dma_feed_sel == 1):
                                m.d.sync += [
                                    reg_addr.eq(self.AUD1DAT),
                                    reg_data.eq(fake_agnus1.o_audio_data0),
                                    reg_write.eq(1),
                                    dma_prime_writes.eq(dma_prime_writes - 1),
                                    dma_feed_sel.eq(2),
                                    write_hold.eq(1),
                                ]
                            with m.Else():
                                m.d.sync += [
                                    reg_addr.eq(self.AUD2DAT),
                                    reg_data.eq(fake_agnus2.o_audio_data0),
                                    reg_write.eq(1),
                                    dma_prime_writes.eq(dma_prime_writes - 1),
                                    dma_feed_sel.eq(0),
                                    write_hold.eq(1),
                                ]
                        with m.Elif(dma_req_pending != 0):
                            with m.Switch(dma_feed_sel):
                                with m.Case(0):
                                    with m.If(dma_req_pending[0] == 1):
                                        m.d.sync += [
                                            reg_addr.eq(self.AUD0DAT),
                                            reg_data.eq(fake_agnus0.o_audio_data0),
                                            reg_write.eq(1),
                                            dma_req_pending[0].eq(0),
                                            dma_feed_sel.eq(1),
                                            write_hold.eq(1),
                                        ]
                                    with m.Elif(dma_req_pending[1] == 1):
                                        m.d.sync += [
                                            reg_addr.eq(self.AUD1DAT),
                                            reg_data.eq(fake_agnus1.o_audio_data0),
                                            reg_write.eq(1),
                                            dma_req_pending[1].eq(0),
                                            dma_feed_sel.eq(2),
                                            write_hold.eq(1),
                                        ]
                                    with m.Elif(dma_req_pending[2] == 1):
                                        m.d.sync += [
                                            reg_addr.eq(self.AUD2DAT),
                                            reg_data.eq(fake_agnus2.o_audio_data0),
                                            reg_write.eq(1),
                                            dma_req_pending[2].eq(0),
                                            dma_feed_sel.eq(0),
                                            write_hold.eq(1),
                                        ]
                                with m.Case(1):
                                    with m.If(dma_req_pending[1] == 1):
                                        m.d.sync += [
                                            reg_addr.eq(self.AUD1DAT),
                                            reg_data.eq(fake_agnus1.o_audio_data0),
                                            reg_write.eq(1),
                                            dma_req_pending[1].eq(0),
                                            dma_feed_sel.eq(2),
                                            write_hold.eq(1),
                                        ]
                                    with m.Elif(dma_req_pending[2] == 1):
                                        m.d.sync += [
                                            reg_addr.eq(self.AUD2DAT),
                                            reg_data.eq(fake_agnus2.o_audio_data0),
                                            reg_write.eq(1),
                                            dma_req_pending[2].eq(0),
                                            dma_feed_sel.eq(0),
                                            write_hold.eq(1),
                                        ]
                                    with m.Elif(dma_req_pending[0] == 1):
                                        m.d.sync += [
                                            reg_addr.eq(self.AUD0DAT),
                                            reg_data.eq(fake_agnus0.o_audio_data0),
                                            reg_write.eq(1),
                                            dma_req_pending[0].eq(0),
                                            dma_feed_sel.eq(1),
                                            write_hold.eq(1),
                                        ]
                                with m.Default():
                                    with m.If(dma_req_pending[2] == 1):
                                        m.d.sync += [
                                            reg_addr.eq(self.AUD2DAT),
                                            reg_data.eq(fake_agnus2.o_audio_data0),
                                            reg_write.eq(1),
                                            dma_req_pending[2].eq(0),
                                            dma_feed_sel.eq(0),
                                            write_hold.eq(1),
                                        ]
                                    with m.Elif(dma_req_pending[0] == 1):
                                        m.d.sync += [
                                            reg_addr.eq(self.AUD0DAT),
                                            reg_data.eq(fake_agnus0.o_audio_data0),
                                            reg_write.eq(1),
                                            dma_req_pending[0].eq(0),
                                            dma_feed_sel.eq(1),
                                            write_hold.eq(1),
                                        ]
                                    with m.Elif(dma_req_pending[1] == 1):
                                        m.d.sync += [
                                            reg_addr.eq(self.AUD1DAT),
                                            reg_data.eq(fake_agnus1.o_audio_data0),
                                            reg_write.eq(1),
                                            dma_req_pending[1].eq(0),
                                            dma_feed_sel.eq(2),
                                            write_hold.eq(1),
                                        ]

        return m


if __name__ == "__main__":
    top_level_cli(Paula2Top, video_core=False)
