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

CDCC_ROOT = "/home/mat/dev/cached_dilated_causal_convolutions/"
RUN = os.getenv("RUN")
if RUN is None:
    raise Exception("$RUN not set")
sys.path.insert(0, f"{CDCC_ROOT}/amaranth_version/src")
from cdcc import NNQ
from cdcc.qb_network import QbNetwork
from cdcc.cosine_estimator import CosineEstimator

class NeuralWaveshaper(wiring.Component):

    i: In(stream.Signature(data.ArrayLayout(ASQ, 4)))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 4)))

    # Jack detection (directly from pmod hardware)
    jack: In(unsigned(8))

    bitstream_help = BitstreamHelp(
        brief="Neural waveshaper.",
        io_left=[
            "core sine wave",
            "core cosine wave",
            "embedding x",
            "embedding y",
            "waveshaped with lpf",
            "waveshaped",
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
        self.qb_model = QbNetwork.build(WEIGHTS_PKL)
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        # network mapping 4 in to 1 out
        m.submodules.qb_model = self.qb_model

        # post network low pass filter
        m.submodules.post_lpf = post_lpf = dsp.OnePole()

        # a cosine estimator driven from in0 sine wave
        # fed to network if in1 is not patched.
        m.submodules.ce = ce = CosineEstimator(shape=ASQ)
        estimated_cosine_nnq = Signal(NNQ)
        m.d.comb += estimated_cosine_nnq.eq(ce.o.payload)

        # delay model input a bit since ce is delayed one.
        accepted = Signal()
        input_d = Array(Signal(NNQ, name=f"input_d_k{k}") for k in range(4))

        m.d.comb += accepted.eq(self.i.valid & self.qb_model.i.ready)
        with m.If(accepted):
            for c in range(4):
                m.d.sync += input_d[c].eq(self.i.payload[c])

        # connect in0 (sin) to the in1 (cos) estimator
        m.d.comb += [
            ce.i.payload.eq(self.i.payload[0]),
            ce.i.valid.eq(accepted),
            ce.o.ready.eq(1),
        ]

        # map inputs from ASQ input to NNQ for model
        # quadrature model is (sin x, cos x, e0, e1)
        # ( in1 normalled to the estimated cosine from in0 )
        model_input = Array(Signal(NNQ, name=f"model_input_k{k}") for k in range(4))
        for c in range(4):
            if c == 1:
                m.d.comb += model_input[1].eq(
                    Mux(self.jack[1], input_d[1], estimated_cosine_nnq)
                )
            else:
                m.d.comb += model_input[c].eq(input_d[c])
            m.d.comb += self.qb_model.i.payload[c].eq(model_input[c])

        # The model only outputs one value (on out0),
        # saturate to ASQ and output a filtered and unfiltered version
        # TODO: should this be saturate? or just comb eq ?
        waveshaped_out = Signal(NNQ)
        saturated_waveshaped_out = waveshaped_out.saturate(ASQ)
        m.d.comb += [
            waveshaped_out.eq(self.qb_model.o.payload),
            post_lpf.i.payload.eq(saturated_waveshaped_out),
            post_lpf.shift.eq(1),
            self.o.payload[0].eq(post_lpf.o.payload),
            self.o.payload[1].eq(saturated_waveshaped_out),
            self.o.payload[2].eq(0),
            self.o.payload[3].eq(estimated_cosine_nnq),
        ]

        # wire up ready and valid
        # TODO: could wiring.connect do all this?
        m.d.comb += [
            self.qb_model.i.valid.eq(ce.o.valid),
            self.i.ready.eq(self.qb_model.i.ready),
            post_lpf.i.valid.eq(self.qb_model.o.valid),
            self.qb_model.o.ready.eq(post_lpf.i.ready),
            post_lpf.o.ready.eq(self.o.ready),
            self.o.valid.eq(post_lpf.o.valid),
        ]

        return m


class CoreTop(Elaboratable):

    def __init__(self, clock_settings):
        self.core = NeuralWaveshaper()
        self.core.audio_clock = clock_settings.audio_clock
        self.touch = False
        self.clock_settings = clock_settings
        self.pmod0 = eurorack_pmod.EurorackPmod(clock_settings.audio_clock)

        # One PSRAM peripheral shared across the PSRAM backed activation caches.
        self.psram_periph = psram.Peripheral(size=16 * 1024 * 1024)
        for i in self.core.qb_model.psram_cache_indices:
            self.psram_periph.add_master(getattr(self.core.qb_model, f"bus_act{i}"))

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

        m.submodules.psram_periph = self.psram_periph

        return m


if __name__ == "__main__":
    this_path = os.path.dirname(os.path.realpath(__file__))
    top_level_cli(
        CoreTop,
        video_core=False,
        path=this_path,
        archiver_callback=lambda archiver: archiver.with_option_storage(),
    )
