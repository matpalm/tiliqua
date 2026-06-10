"""Minimal Paula bring-up with live in0 source for AUD0DAT."""

from amaranth import *
from amaranth.lib import cdc, wiring

from tiliqua.build.cli import top_level_cli
from tiliqua.build.types import BitstreamHelp
from tiliqua.dsp import ASQ
from tiliqua.periph import eurorack_pmod
from tiliqua.platform import RebootProvider

from paula_audio_wrapper import PaulaAudioWrapper

class PaulaTop(Elaboratable):

    SAMPLE_COUNT = 256
    PAULA_PERIOD = 124
    BYPASS_PAULA_AUDIO = False
    USE_DMA_MODE = False

    AUD0LEN = 0x52
    AUD0PER = 0x53
    AUD0VOL = 0x54
    AUD0DAT = 0x55

    bitstream_help = BitstreamHelp(
        brief="Paula live in0-source bring-up",
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

        m.submodules.car = car = platform.clock_domain_generator(self.clock_settings)
        m.submodules.reboot = reboot = RebootProvider(car.settings.frequencies.sync)
        m.submodules.btn = cdc.FFSynchronizer(
            platform.request("encoder").s.i, reboot.button
        )

        m.submodules.pmod0_provider = pmod0_provider = eurorack_pmod.FFCProvider()
        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
            self.clock_settings.audio_clock
        )
        wiring.connect(m, pmod0.pins, pmod0_provider.pins)
        # Keep codec forced unmuted during Paula bring-up.
        m.d.comb += pmod0.codec_mute.eq(0)

        m.submodules.paudio = paudio = PaulaAudioWrapper()

        config_state = Signal(unsigned(3), init=0)
        reg_addr = Signal(unsigned(8), init=0)
        reg_data = Signal(unsigned(16), init=0)
        reset_ctr = Signal(range(64), init=63)
        pa_rst = Signal()

        # Paula timing strobes in sync domain.
        clk7_div = Signal(range(8), init=0)
        clk7_en_pulse = Signal(init=0)
        strhor_div = Signal(range(480), init=0)
        strhor_pulse = Signal(init=0)
        write_hold = Signal(range(2), init=0)

        sample_tick = Signal()
        in0_sample = Signal(signed(ASQ.as_shape().width))
        sample_shift = max(ASQ.as_shape().width - 8, 0)
        in0_s8 = Signal(signed(8))
        tri_direct = Signal(signed(ASQ.as_shape().width))
        sample_word = Signal(unsigned(16))
        pa_l = Signal(signed(15))
        out_shift = max(ASQ.as_shape().width - 15, 0)
        direct_shift = max(ASQ.as_shape().width - 8, 0)
        dbg_phase = Signal(unsigned(24), init=0)
        dbg_tone = Signal(signed(ASQ.as_shape().width))

        m.d.comb += [
            pmod0.o_cal.ready.eq(1),
            pmod0.i_cal.valid.eq(1),
            sample_tick.eq(pmod0.o_cal.valid),
            in0_sample.eq(pmod0.o_cal.payload[0].as_value()),
            in0_s8.eq(in0_sample >> sample_shift),
            sample_word.eq(Cat(in0_s8.as_unsigned(), in0_s8.as_unsigned())),
            tri_direct.eq(in0_s8 << direct_shift),
            pa_l.eq(paudio.ldata.as_signed()),
            pmod0.i_cal.payload[0]
            .as_value()
            .eq(Mux(self.BYPASS_PAULA_AUDIO, dbg_tone, pa_l << out_shift)),
            pmod0.i_cal.payload[1].as_value().eq(tri_direct),
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
                Mux(pa_rst, C(0, 4), Mux(self.USE_DMA_MODE, C(0b0001, 4), C(0, 4)))
            ),
            paudio.audpen.eq(0),
        ]

        m.d.sync += dbg_phase.eq(dbg_phase + 15000)
        m.d.comb += dbg_tone.eq(Mux(dbg_phase[-1], 12000, -12000))

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
                            reg_data.eq(self.SAMPLE_COUNT),
                            config_state.eq(4),
                            write_hold.eq(1),
                        ]
                    with m.Case(4):
                        # Stream live in0-derived waveform continuously (CPU mode).
                        with m.If(~pa_rst):
                            m.d.sync += [
                                reg_addr.eq(self.AUD0DAT),
                                reg_data.eq(sample_word),
                                write_hold.eq(1),
                            ]

        return m


if __name__ == "__main__":
    top_level_cli(PaulaTop, video_core=False)
