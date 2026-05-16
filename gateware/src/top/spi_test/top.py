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

    # def __init__(self):
    #     trained_weights = f"{CDCC_ROOT}/runs/{RUN}/weights/qkeras/latest.pkl"
    #     if not os.path.exists(trained_weights):
    #         raise Exception(
    #             f"failed to load weights for CDCC_ROOT=[{CDCC_ROOT}] with $RUN=[{RUN}]"
    #         )
    #     print(f"loading weights from {trained_weights}")
    #     self.qb_model = QbNetwork.build(trained_weights)
    #     super().__init__()

    def elaborate(self, platform):
        m = Module()
        wiring.connect(m, wiring.flipped(self.i), wiring.flipped(self.o))
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
