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

# from amaranth_v.rff_concat_network import RffNetwork, load_weights_and_config
# from amaranth_v.rff_film_network import RffNetwork, load_weights_and_config
from amaranth_v.rff_network_builder import load_and_build_network
from amaranth_v import NNQ
from amaranth_v.ramp_v_oct import RampVOct

class INRWaveshaper(wiring.Component):

    i: In(stream.Signature(data.ArrayLayout(ASQ, 4)))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 4)))

    # Jack detection (directly from pmod hardware)
    jack: In(unsigned(8))

    bitstream_help = BitstreamHelp(
        brief="Neural waveshaper.",
        io_left=[
            "phase",
            "e0",
            "e1",
            "-",
            "waveshaped",
            "waveshaped lpf",
            "",
            "phase",
        ],
        io_right=["", "", "", "", "", ""],
    )

    def __init__(self):
        WEIGHTS_PKL = os.getenv("WEIGHTS_PKL")
        if not WEIGHTS_PKL or not os.path.exists(WEIGHTS_PKL):
            raise Exception(f"failed to load weights for WEIGHTS_PKL=[{WEIGHTS_PKL}]")
        print(f"loading weights from {WEIGHTS_PKL}")
        self.net = load_and_build_network(WEIGHTS_PKL)
        super().__init__()

    def elaborate(self, platform):
        m = Module()
        m.submodules.net = net = self.net
        m.submodules.ramp = ramp = RampVOct()

        m.submodules.post_lpf = post_lpf = dsp.OnePole()

        # ASQ (audio) and the network's io fixed-point shape differ in scale;
        # convert by aligning fractional bits (preserving the real value) on the
        # way in, and saturating back into ASQ on the way out.
        io_f = net.io_shape.f_bits
        io_i = net.io_shape.i_bits
        IOQ = fixed.SQ(io_i, io_f)  # tiliqua-side view of the network io shape

        # convert ASQ in0 to volts for RampVOct (ASQ 1.0 == 8.192V),
        # then feed ramp phase into network input 0.
        in0_volts = (ASQ(self.i.payload[0].as_value()) * fixed.Const(8.192)).saturate(
            NNQ
        )
        m.d.comb += [
            ramp.i.payload.eq(in0_volts),
            ramp.i.valid.eq(self.i.valid),
            ramp.o.ready.eq(net.i.ready),
        ]

        # Training used phase in [-0.5, +0.5] where +/-5V corresponds to
        # +/-0.5. RampVOct emits volts, so scale by 0.1 before feeding net0.
        phase_for_net = (
            NNQ(ramp.o.payload.as_value()) * fixed.Const(0.1, shape=NNQ)
        ).saturate(NNQ)

        # map tiliqua inputs to network as required
        print("net in_d", self.net.in_d, "out_d", self.net.out_d)

        for ch in range(self.net.in_d):
            if ch == 0:
                m.d.comb += (
                    net.i.payload[ch]
                    .as_value()
                    .eq(phase_for_net.reshape(io_f).as_value())
                )
            else:
                m.d.comb += (
                    net.i.payload[ch]
                    .as_value()
                    .eq(self.i.payload[ch].reshape(io_f).as_value())
                )

        # set waveshaped output net out0 -> tiliqua out0 ( as ASQ )
        # set lowpassed version on out1 ( delayed one cycle )
        o_io = IOQ(net.o.payload[0].as_value())
        # RampVOct outputs NNQ values in *volts*; convert volts -> ASQ units
        # (ASQ 1.0 == 8.192V) before driving the codec output.
        ramp_asq = (
            NNQ(ramp.o.payload.as_value()) * fixed.Const(0.1220703125, shape=NNQ)
        ).saturate(ASQ)
        m.d.comb += [
            post_lpf.i.payload.eq(o_io.saturate(ASQ)),
            post_lpf.shift.eq(1),
            self.o.payload[0].eq(o_io.saturate(ASQ)),
            self.o.payload[1].eq(post_lpf.o.payload),
            self.o.payload[2].eq(0),
            self.o.payload[3].eq(ramp_asq),
        ]

        # stream handshake: one audio frame in -> one network eval -> one out.
        m.d.comb += [
            net.i.valid.eq(ramp.o.valid),
            self.i.ready.eq(ramp.i.ready),
            post_lpf.i.valid.eq(net.o.valid),
            post_lpf.o.ready.eq(self.o.ready),
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
