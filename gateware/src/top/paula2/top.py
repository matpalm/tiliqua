"""Paula standalone bring-up with startup capture and timed playback rates.

Capture 1 second from input 0 on startup, then replay with 1-second segments:
1x, 1/4x, 1x, 4x, repeating.
"""

from amaranth import *
from amaranth.lib import wiring

from tiliqua.build.cli import top_level_cli
from tiliqua.build.types import BitstreamHelp
from tiliqua.dsp import ASQ
from tiliqua.periph import eurorack_pmod
from tiliqua.platform import RebootProvider

from fake_agnus import FakeAgnus
from paula_audio_wrapper import PaulaAudioWrapper


class Paula2Top(Elaboratable):

    PAULA_CCK_HZ = 60_000_000 // 8
    PAULA_MIN_PERIOD = 121

    # Keep transport cadence near the known-good 1x region.
    PAULA_BASE_PERIOD = 240
    PAULA_LENGTH_WORDS = FakeAgnus.CAPTURE_WORDS
    PAULA_DMA_ENABLE_MASK = 0b0001

    AUD0LEN = 0x52
    AUD0PER = 0x53
    AUD0VOL = 0x54
    AUD0DAT = 0x55

    bitstream_help = BitstreamHelp(
        brief="Paula standalone (startup capture + 1x/0.25x/1x/4x replay)",
        io_left=[
            "audio in 0",
            "audio in 1",
            "cv reserve 0",
            "",
            "paula out L",
            "paula out R",
            "",
            "",
        ],
        io_right=["", "", "", "", "", ""],
    )

    def __init__(self, clock_settings):
        self.clock_settings = clock_settings
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.car = car = platform.clock_domain_generator(self.clock_settings)
        m.submodules.reboot = reboot = RebootProvider(car.settings.frequencies.sync)

        m.submodules.pmod0_provider = pmod0_provider = eurorack_pmod.FFCProvider()
        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
            self.clock_settings.audio_clock
        )
        wiring.connect(m, pmod0.pins, pmod0_provider.pins)

        m.submodules.paudio = paudio = PaulaAudioWrapper()
        m.submodules.fake_agnus = fake_agnus = FakeAgnus()

        config_state = Signal(unsigned(3), init=0)
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

        dma_prime_writes = Signal(range(32), init=0)
        dma_req_pending = Signal(init=0)
        dma_idle_ctr = Signal(range(32), init=0)
        dma_idle_kick_done = Signal(init=0)

        dmal0_prev = Signal(init=0)
        sample_tick = Signal()

        sample_width = ASQ.as_shape().width
        in_shift = max(sample_width - 8, 0)
        raw_in0 = Signal(signed(sample_width))
        in0_s8 = Signal(signed(8))

        pa_l = Signal(signed(15))
        pa_r = Signal(signed(15))
        out_shift = max(ASQ.as_shape().width - 15, 0)

        m.d.comb += [
            pmod0.o_cal.ready.eq(1),
            pmod0.i_cal.valid.eq(1),
            pmod0.codec_mute.eq(0),
            sample_tick.eq(pmod0.o_cal.valid),
            raw_in0.eq(pmod0.o_cal.payload[0].as_value()),
            in0_s8.eq(raw_in0 >> in_shift),
            pa_l.eq(paudio.ldata.as_signed()),
            pa_r.eq(paudio.rdata.as_signed()),
            pa_rst.eq((reset_ctr != 0) | (~fake_agnus.o_capture_done)),
            pmod0.i_cal.payload[0].as_value().eq(pa_l << out_shift),
            pmod0.i_cal.payload[1].as_value().eq(pa_r << out_shift),
            pmod0.i_cal.payload[2].as_value().eq(0),
            pmod0.i_cal.payload[3].as_value().eq(0),
            paudio.clk7_en.eq(clk7_en_pulse),
            paudio.cck.eq(clk7_en_pulse),
            paudio.rst.eq(pa_rst),
            paudio.strhor.eq(strhor_pulse),
            paudio.reg_address_in.eq(reg_addr),
            paudio.data_in.eq(reg_data),
            paudio.dmaena.eq(Mux(pa_rst, C(0, 4), C(self.PAULA_DMA_ENABLE_MASK, 4))),
            paudio.audpen.eq(0),
            fake_agnus.i_reset.eq(reset_ctr != 0),
            fake_agnus.i_audio_dmal.eq(paudio.dmal[0]),
            fake_agnus.i_audio_dmas.eq(paudio.dmas[0]),
            fake_agnus.i_sample_tick.eq(sample_tick),
            fake_agnus.i_sample_in.eq(in0_s8.as_unsigned()),
            fake_agnus.i_reg_write.eq(reg_write),
            fake_agnus.i_reg_addr.eq(reg_addr),
            fake_agnus.i_reg_data.eq(reg_data),
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

        with m.If(clk7_en_pulse):
            m.d.sync += [
                reg_addr.eq(0),
                reg_data.eq(0),
                reg_write.eq(0),
            ]

            with m.If(pa_rst):
                m.d.sync += [
                    dma_req_pending.eq(0),
                    dma_idle_ctr.eq(0),
                    dma_idle_kick_done.eq(0),
                    dmal0_prev.eq(0),
                ]
            with m.Else():
                # Latch DMA request on dmal rising edge and keep a strhor fallback.
                with m.If((paudio.dmal[0] == 1) & (dmal0_prev == 0)):
                    m.d.sync += dma_req_pending.eq(1)
                with m.Elif(strhor_pulse & paudio.dmal[0]):
                    m.d.sync += dma_req_pending.eq(1)
                m.d.sync += dmal0_prev.eq(paudio.dmal[0])

            with m.If(write_hold != 0):
                m.d.sync += write_hold.eq(write_hold - 1)
            with m.Else():
                with m.Switch(config_state):
                    with m.Case(1):
                        m.d.sync += [
                            reg_addr.eq(self.AUD0PER),
                            reg_data.eq(self.PAULA_BASE_PERIOD),
                            reg_write.eq(1),
                            config_state.eq(2),
                            write_hold.eq(1),
                        ]
                    with m.Case(2):
                        m.d.sync += [
                            reg_addr.eq(self.AUD0VOL),
                            reg_data.eq(64),
                            reg_write.eq(1),
                            config_state.eq(3),
                            write_hold.eq(1),
                        ]
                    with m.Case(3):
                        m.d.sync += [
                            reg_addr.eq(self.AUD0LEN),
                            reg_data.eq(self.PAULA_LENGTH_WORDS),
                            reg_write.eq(1),
                            config_state.eq(4),
                            dma_prime_writes.eq(12),
                            write_hold.eq(1),
                        ]
                    with m.Case(4):
                        with m.If(dma_prime_writes != 0):
                            m.d.sync += [
                                reg_addr.eq(self.AUD0DAT),
                                reg_data.eq(fake_agnus.o_audio_data0),
                                reg_write.eq(1),
                                dma_prime_writes.eq(dma_prime_writes - 1),
                                dma_idle_ctr.eq(0),
                                write_hold.eq(1),
                            ]
                        with m.Elif(dma_req_pending):
                            m.d.sync += [
                                reg_addr.eq(self.AUD0DAT),
                                reg_data.eq(fake_agnus.o_audio_data0),
                                reg_write.eq(1),
                                dma_req_pending.eq(0),
                                dma_idle_ctr.eq(0),
                                write_hold.eq(1),
                            ]
                        with m.Elif((dma_idle_ctr == 31) & (~dma_idle_kick_done)):
                            m.d.sync += [
                                reg_addr.eq(self.AUD0DAT),
                                reg_data.eq(fake_agnus.o_audio_data0),
                                reg_write.eq(1),
                                dma_idle_kick_done.eq(1),
                                dma_idle_ctr.eq(0),
                                write_hold.eq(1),
                            ]
                        with m.Elif(dma_idle_ctr == 31):
                            m.d.sync += dma_idle_ctr.eq(0)
                        with m.Else():
                            m.d.sync += dma_idle_ctr.eq(dma_idle_ctr + 1)

        return m


if __name__ == "__main__":
    top_level_cli(Paula2Top, video_core=False)
