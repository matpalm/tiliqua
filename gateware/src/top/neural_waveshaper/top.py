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

CDCC_ROOT = "/home/mat/dev/cached_dilated_causal_convolutions/"
RUN = os.getenv("RUN", "46_tiliqua_2layer_4d_retest")
sys.path.insert(0, f"{CDCC_ROOT}/amaranth_version/src")
from cdcc import NNQ
from cdcc.qb_network import QbNetwork

class NeuralWaveshaper(wiring.Component):
    """
    Route audio inputs straight to outputs (in the audio domain).
    This is the simplest possible core, useful for basic tests.
    """

    i: In(stream.Signature(data.ArrayLayout(ASQ, 4)))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 4)))

    bitstream_help = BitstreamHelp(
        brief="Neural waveshaper.",
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

    def __init__(self):
        trained_weights = f"{CDCC_ROOT}/runs/{RUN}/weights/qkeras/latest.pkl"
        if not os.path.exists(trained_weights):
            raise Exception(
                f"failed to load weights for CDCC_ROOT=[{CDCC_ROOT}] with $RUN=[{RUN}]"
            )
        print(f"loading weights from {trained_weights}")
        self.qb_model = QbNetwork.build(trained_weights)
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.qb_model = self.qb_model
        m.submodules.post_lpf = post_lpf = dsp.OnePole()

        # map inputs.
        # note: model is currently (x, e0, e1, 0)
        # ( with an expected 0 values for in3 )
        model_input = Array(Signal(NNQ, name=f"model_input_k{k}") for k in range(4))
        for c in range(3):
            m.d.comb += [
                model_input[c].eq(self.i.payload[c]),
                self.qb_model.i.payload[c].eq(model_input[c]),
            ]
        m.d.comb += [
            self.qb_model.i.payload[3].eq(0),
        ]

        # The model only outputs one value (on out0),
        # saturate to ASQ and output a filtered and unfiltered version
        waveshaped_out = Signal(NNQ)
        saturated_waveshaped_out = waveshaped_out.saturate(ASQ)
        m.d.comb += [
            waveshaped_out.eq(self.qb_model.o.payload),
            post_lpf.i.payload.eq(saturated_waveshaped_out),
            post_lpf.shift.eq(1),
            self.o.payload[0].eq(post_lpf.o.payload),
            self.o.payload[1].eq(saturated_waveshaped_out),
            self.o.payload[2].eq(0),
            self.o.payload[3].eq(0),
        ]

        # wire up ready and valid
        m.d.comb += [
            self.qb_model.i.valid.eq(self.i.valid),
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
