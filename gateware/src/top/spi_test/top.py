"""
Neural waveshaper
"""

import math
from scipy.interpolate import CubicHermiteSpline
import sys
import os

from amaranth_future import fixed

from amaranth import *
from amaranth.build import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.cdc import FFSynchronizer
from amaranth.lib.wiring import In, Out
from amaranth_soc import wishbone

from tiliqua import dsp, midi
from tiliqua.build import sim
from tiliqua.build.cli import top_level_cli
from tiliqua.build.types import BitstreamHelp
from tiliqua.dsp import ASQ, block, spectral
from tiliqua.dsp.mix import CoeffUpdate
from tiliqua.periph import eurorack_pmod, psram
from tiliqua.platform import RebootProvider

# complete WIP hack :/ should be plumbed through from CLI
import os

# CDCC_ROOT = "/home/mat/dev/cached_dilated_causal_convolutions/"
# RUN = os.getenv("RUN")
# if RUN is None:
#     raise Exception("$RUN not set")
# sys.path.insert(0, f"{CDCC_ROOT}/amaranth_version/src")
# from cdcc import NNQ
# from cdcc.qb_network import QbNetwork


class SpiTest(wiring.Component):

    i: In(stream.Signature(data.ArrayLayout(ASQ, 4)))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 4)))

    bitstream_help = BitstreamHelp(
        brief="SpiTest.",
        io_left=[
            "core wave",
            "embedding x",
            "embedding y",
            "",
            "waveshaped",
            "",
            "",
            "",
        ],
        io_right=["", "", "", "", "", ""],
    )

    def elaborate(self, platform):
        m = Module()

        # drive out0 based on whether we are processing SPI
        # drive out1 based on whether we are returning 0x11 or 0x22
        out0_value = Signal(shape=ASQ)
        out1_value = Signal(shape=ASQ)
        m.d.comb += [
            self.o.payload[0].eq(out0_value),
            self.o.payload[1].eq(out1_value),
            self.o.payload[2].eq(0),
            self.o.payload[3].eq(0),
            self.o.valid.eq(self.i.valid),
            self.i.ready.eq(self.o.ready),
        ]

        # connected to ex1, and we are only running one spi
        ex_idx = 1
        spi_idx = 0
        platform.add_resources(
            [
                Resource(
                    "spi",
                    spi_idx,
                    Subsignal("cs", Pins("1", dir="i", conn=("pmod", ex_idx))),
                    Subsignal("sck", Pins("7", dir="i", conn=("pmod", ex_idx))),
                    Subsignal("mosi", Pins("9", dir="i", conn=("pmod", ex_idx))),
                    Subsignal("miso", Pins("8", dir="o", conn=("pmod", ex_idx))),
                    Attrs(IO_TYPE="LVCMOS33", DRIVE="8"),
                )
            ]
        )
        spi = platform.request("spi", spi_idx)

        cs_sync = Signal()
        sck_sync = Signal()
        mosi_sync = Signal()

        m.submodules += [
            FFSynchronizer(spi.cs.i, cs_sync),
            FFSynchronizer(spi.sck.i, sck_sync),
            FFSynchronizer(spi.mosi.i, mosi_sync),
        ]

        prev_cs = Signal(reset=1)
        prev_sck = Signal()

        bit_count = Signal(range(25))
        rx_shift = Signal(24)

        tx_shift = Signal(8)
        tx_active = Signal()
        tx_armed = Signal()

        m.d.comb += spi.miso.o.eq(Mux(tx_active, tx_shift[7], 0))

        with m.If(cs_sync):
            m.d.sync += [
                bit_count.eq(0),
                tx_active.eq(0),
                tx_armed.eq(0),
                out0_value.eq(fixed.Const(-0.5, shape=ASQ)),
            ]
        with m.Else():
            with m.If((~prev_sck) & sck_sync):
                # SPI is MSB-first here, so shift left and insert new bit at LSB.
                next_rx = Cat(mosi_sync, rx_shift[:-1])
                m.d.sync += [
                    rx_shift.eq(next_rx),
                    bit_count.eq(bit_count + 1),
                    out0_value.eq(fixed.Const(0.5, shape=ASQ)),
                ]

                # After exactly 3 received bytes, choose response byte.
                with m.If(bit_count == 23):
                    with m.If(next_rx == 0x010203):
                        m.d.sync += [
                            tx_shift.eq(0x11),
                            tx_armed.eq(1),
                            out1_value.eq(fixed.Const(0.5, shape=ASQ)),
                        ]
                    with m.Else():
                        m.d.sync += [
                            tx_shift.eq(0x22),
                            tx_armed.eq(1),
                            out1_value.eq(fixed.Const(-0.5, shape=ASQ)),
                        ]
                    m.d.sync += tx_active.eq(1)

            # Shift response on SCK falling edges (SPI mode 0 style).
            with m.If(prev_sck & (~sck_sync) & tx_active):
                with m.If(tx_armed):
                    # First falling edge after loading keeps MSB stable for first sample.
                    m.d.sync += tx_armed.eq(0)
                with m.Else():
                    m.d.sync += tx_shift.eq((tx_shift << 1)[:8])

        m.d.sync += [
            prev_cs.eq(cs_sync),
            prev_sck.eq(sck_sync),
        ]

        return m


class CoreTop(Elaboratable):

    def __init__(self, clock_settings):
        self.core = SpiTest()
        self.core.audio_clock = clock_settings.audio_clock
        self.touch = False
        self.clock_settings = clock_settings
        self.pmod0 = eurorack_pmod.EurorackPmod(clock_settings.audio_clock)

        # Only if this core uses PSRAM
        if hasattr(self.core, "bus"):
            self.psram_periph = psram.Peripheral(size=16 * 1024 * 1024)

        # Forward bitstream_help from the core if it exists
        if hasattr(self.core, "bitstream_help"):
            self.bitstream_help = self.core.bitstream_help

        super().__init__()

    def elaborate(self, platform):
        m = Module()
        m.submodules.pmod0 = pmod0 = self.pmod0
        if sim.is_hw(platform):
            m.submodules.car = car = platform.clock_domain_generator(
                self.clock_settings
            )
            m.submodules.provider = provider = eurorack_pmod.FFCProvider()
            wiring.connect(m, pmod0.pins, provider.pins)
            m.submodules.reboot = reboot = RebootProvider(
                self.clock_settings.frequencies.sync
            )
            m.submodules.btn = FFSynchronizer(
                platform.request("encoder").s.i, reboot.button
            )
            m.d.comb += pmod0.codec_mute.eq(reboot.mute)
        else:
            m.submodules.car = sim.FakeTiliquaDomainGenerator()

        m.submodules.core = self.core
        wiring.connect(m, pmod0.o_cal, self.core.i)
        wiring.connect(m, self.core.o, pmod0.i_cal)

        # if hasattr(self.core, "i_midi") and sim.is_hw(platform):
        #     # For now, if a core requests midi input, we connect it up
        #     # to the type-A serial MIDI RX input. In theory this bytestream
        #     # could also come from LUNA in host or device mode.
        #     midi_pins = platform.request("midi")
        #     m.submodules.serialrx = serialrx = midi.SerialRx(
        #         system_clk_hz=60e6, pins=midi_pins
        #     )
        #     m.submodules.midi_decode = midi_decode = midi.MidiDecodeSerial()
        #     wiring.connect(m, serialrx.o, midi_decode.i)
        #     wiring.connect(m, midi_decode.o, self.core.i_midi)

        if hasattr(self.core, "bus"):
            m.submodules.psram_periph = self.psram_periph
            wiring.connect(m, self.core.bus, self.psram_periph.bus)

        return m


if __name__ == "__main__":
    this_path = os.path.dirname(os.path.realpath(__file__))
    top_level_cli(
        CoreTop,
        video_core=False,
        path=this_path,
        archiver_callback=lambda archiver: archiver.with_option_storage(),
    )
