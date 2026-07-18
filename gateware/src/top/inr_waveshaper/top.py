"""
Neural waveshaper
"""

import math
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

INR_ROOT = "/home/mat/dev/inr_waveshaper/"
sys.path.insert(0, INR_ROOT)

from amaranth_v.rff_network import RffNetwork, load_weights


class INRWaveshaper(wiring.Component):

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
        if not WEIGHTS_PKL or not os.path.exists(WEIGHTS_PKL):
            raise Exception(f"failed to load weights for WEIGHTS_PKL=[{WEIGHTS_PKL}]")
        print(f"loading weights from {WEIGHTS_PKL}")
        weights = load_weights(WEIGHTS_PKL)
        lut_size = int(os.getenv("LUT_SIZE"))

        self.net = RffNetwork(weights, lut_size=lut_size)
        super().__init__()

    def elaborate(self, platform):
        m = Module()
        m.submodules.net = net = self.net

        # ASQ (audio) and the network's io fixed-point shape differ in scale;
        # convert by aligning fractional bits (preserving the real value) on the
        # way in, and saturating back into ASQ on the way out.
        io_f = net.io_shape.f_bits
        io_i = net.io_shape.i_bits
        IOQ = fixed.SQ(io_i, io_f)  # tiliqua-side view of the network io shape

        # internal phase ramp: just generate a -1,1 ramp running at A4.
        # TODO: hook up v/oct
        SAMPLE_RATE_HZ = 192_000
        PHASE_FREQ_HZ = 440  # A4
        io_w = io_i + io_f
        phase_one = 1 << io_f  # fixed-point code for +1.0
        phase_step = round(PHASE_FREQ_HZ / SAMPLE_RATE_HZ * (1 << (io_f + 1)))
        phase = Signal(signed(io_w))
        phase_next = Signal(signed(io_w))
        m.d.comb += phase_next.eq(phase + phase_step)
        with m.If(net.i.valid & net.i.ready):
            with m.If(phase_next >= phase_one):
                m.d.sync += phase.eq(phase_next - (phase_one << 1))
            with m.Else():
                m.d.sync += phase.eq(phase_next)

        # net in0 comes from the phase
        # net in1 and in2 come from tiliqua in1 and in2
        m.d.comb += net.i.payload[0].as_value().eq(phase)
        m.d.comb += (
            net.i.payload[1].as_value().eq(self.i.payload[1].reshape(io_f).as_value())
        )
        m.d.comb += (
            net.i.payload[2].as_value().eq(self.i.payload[2].reshape(io_f).as_value())
        )

        # pass net out0 -> tiliqua out0 ( as ASQ )
        # set other outs to 0
        o_io = IOQ(net.o.payload[0].as_value())
        m.d.comb += self.o.payload[0].eq(o_io.saturate(ASQ))
        for ch in [1, 2, 3]:
            m.d.comb += self.o.payload[ch].eq(0)

        # stream handshake: one audio frame in -> one network eval -> one out.
        m.d.comb += [
            net.i.valid.eq(self.i.valid),
            self.i.ready.eq(net.i.ready),
            net.o.ready.eq(self.o.ready),
            self.o.valid.eq(net.o.valid),
        ]
        return m


class CoreTop(Elaboratable):

    def __init__(self, clock_settings):
        self.core = INRWaveshaper()
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
