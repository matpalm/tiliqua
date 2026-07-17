"""
Neural waveshaper
"""

import math
import sys
import pickle
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

INR_ROOT = "/home/mat/dev/inr_waveshaper/"
sys.path.insert(0, INR_ROOT)

from amaranth_v.rff import RandomFourierFeaturesLUT


class NeuralWaveshaper(wiring.Component):

    i: In(stream.Signature(data.ArrayLayout(ASQ, 4)))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 4)))

    # Jack detection (directly from pmod hardware)
    jack: In(unsigned(8))

    bitstream_help = BitstreamHelp(
        brief="Neural waveshaper.",
        io_left=[
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ],
        io_right=["", "", "", "", "", ""],
    )

    def __init__(self):
        WEIGHTS_PKL = os.getenv("WEIGHTS_PKL")
        if not os.path.exists(WEIGHTS_PKL):
            raise Exception(f"failed to load weights for WEIGHTS_PKL=[{WEIGHTS_PKL}]")
        print(f"loading weights from {WEIGHTS_PKL}")
        with open(WEIGHTS_PKL, "rb") as f:
            weights = pickle.load(f)  # don't shadow amaranth.lib.data
        lut_size = int(os.getenv("LUT_SIZE"))
        self.rff_lut = RandomFourierFeaturesLUT.from_rff(
            weights["rff"], lut_size=lut_size
        )
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        # --- SIZING-ONLY WIRING -------------------------------------------------
        # This connects the RandomFourierFeaturesLUT into the datapath purely so
        # yosys elaborates it and reports its device utilisation. The signal path
        # is NOT correct in any way: the RFF input is taken from audio ch0 and all
        # RFF outputs are summed back into the audio outputs just to keep every
        # LUT / multiplier / register alive (unused logic would be optimised away).
        m.submodules.rff_lut = rff_lut = self.rff_lut

        io_bits = rff_lut._io_bits
        n_out = 2 * rff_lut._num_features

        in0 = Value.cast(self.i.payload[0])
        m.d.comb += [
            rff_lut.i.payload.eq(in0),  # auto-resizes to signed(io_bits)
            rff_lut.i.valid.eq(self.i.valid),
            self.i.ready.eq(rff_lut.i.ready),
            rff_lut.o.ready.eq(self.o.ready),
            self.o.valid.eq(rff_lut.o.valid),
        ]

        # sum every RFF output so none of the datapath is pruned
        acc = Signal(signed(io_bits + (n_out - 1).bit_length() + 1))
        m.d.comb += acc.eq(sum((rff_lut.o.payload[j] for j in range(n_out)), Const(0)))

        for ch in range(4):
            m.d.comb += Value.cast(self.o.payload[ch]).eq(acc)

        return m


class CoreTop(Elaboratable):

    def __init__(self, clock_settings):
        self.core = NeuralWaveshaper()
        self.core.audio_clock = clock_settings.audio_clock
        self.touch = False
        self.clock_settings = clock_settings
        self.pmod0 = eurorack_pmod.EurorackPmod(clock_settings.audio_clock)

        # One PSRAM peripheral shared across the PSRAM backed activation caches.
        # self.psram_periph = psram.Peripheral(size=16 * 1024 * 1024)
        # for i in self.core.qb_model.psram_cache_indices:
        #    self.psram_periph.add_master(getattr(self.core.qb_model, f"bus_act{i}"))

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
        m.d.comb += self.core.jack.eq(pmod0.jack)

        #        m.submodules.psram_periph = self.psram_periph

        return m


if __name__ == "__main__":
    this_path = os.path.dirname(os.path.realpath(__file__))
    top_level_cli(
        CoreTop,
        video_core=False,
        path=this_path,
        archiver_callback=lambda archiver: archiver.with_option_storage(),
    )
