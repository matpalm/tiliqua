"""
Neural waveshaper
"""

import math
import sys
import os
from pathlib import Path

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


def _resolve_phase_h_payload_path(weights_pkl_path: str) -> str | None:
    """Find phase_h payload generated from qkeras run artifacts."""
    env_path = os.getenv("PHASE_H_BIN")
    if env_path:
        if os.path.exists(env_path):
            return env_path
        print(f"warning: PHASE_H_BIN set but file not found: {env_path}")

    try:
        w = Path(weights_pkl_path).resolve()
        # Expected layout: .../runs/<run>/weights/qkeras/latest.pkl
        run_dir = w.parent.parent.parent
        candidate = run_dir / "phase_h.bin"
        if candidate.exists():
            return str(candidate)
    except Exception as ex:
        print(f"warning: failed to derive phase_h.bin path: {ex}")
    return None


# from amaranth_v.rff_concat_network import RffNetwork, load_weights_and_config
from amaranth_v.rff_film_network import RffNetwork
from amaranth_v import NNQ
from amaranth_v.ramp_v_oct import RampVOct

class INRWaveshaper(wiring.Component):

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
        self.net = RffNetwork.build(WEIGHTS_PKL)
        super().__init__(
            {
                "i": In(stream.Signature(data.ArrayLayout(ASQ, 4))),
                "o": Out(stream.Signature(data.ArrayLayout(ASQ, 4))),
                # Jack detection (directly from pmod hardware)
                "jack": In(unsigned(8)),
                # 32-bit wishbone master to PSRAM for the phase->h table. Built
                # once at startup, then read-only during inference.
                "bus_h": Out(self.net.bus_signature),
            }
        )

    def elaborate(self, platform):
        m = Module()
        m.submodules.net = net = self.net
        # phase->h table lives in PSRAM; expose the net's wishbone master.
        wiring.connect(m, net.bus_h, wiring.flipped(self.bus_h))
        # ASQ 1.0 == 8.192V.
        ramp = RampVOct()
        ramp.V_MIN = RampVOct.V_MIN / 8.192
        ramp.V_MAX = RampVOct.V_MAX / 8.192
        ramp.F0_HZ = RampVOct.F0_HZ
        ramp.OCTAVES = RampVOct.OCTAVES
        ramp.RAMP_V = 0.5  # training data is +- 0.5
        m.submodules.ramp = ramp

        m.submodules.post_lpf = post_lpf = dsp.OnePole()

        # ASQ (audio) and the network's io fixed-point shape differ in scale;
        # convert by aligning fractional bits (preserving the real value) on the
        # way in, and saturating back into ASQ on the way out.
        io_f = net.io_shape.f_bits
        io_i = net.io_shape.i_bits
        IOQ = fixed.SQ(io_i, io_f)  # tiliqua-side view of the network io shape

        # Feed ASQ in0 directly into RampVOct after an f_bits align
        # (ASQ/NNQ both use signed 16-bit storage, different f_bits).
        in0_for_ramp = ASQ(self.i.payload[0].as_value()).reshape(NNQ.f_bits)
        m.d.comb += [
            ramp.i.payload.eq(in0_for_ramp),
            ramp.i.valid.eq(self.i.valid),
            ramp.o.ready.eq(net.i.ready),
        ]

        phase_for_net = NNQ(ramp.o.payload.as_value())

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

        # mirror the phase ramp on out3 at +/-5V in ASQ units.
        # phase_for_net is +/-0.5; scale by (5/8.192)/0.5 = 625/512
        ramp_code = Signal(signed(NNQ.width))
        ramp_scaled_num = Signal(signed(NNQ.width + 10))
        ramp_asq_code = Signal(signed(ASQ.width))
        m.d.comb += [
            ramp_code.eq(phase_for_net.as_value()),
            ramp_scaled_num.eq(
                (ramp_code << 9)
                + (ramp_code << 6)
                + (ramp_code << 5)
                + (ramp_code << 4)
                + ramp_code
            ),
            ramp_asq_code.eq(ramp_scaled_num >> 6),
        ]
        m.d.comb += [
            post_lpf.i.payload.eq(o_io.saturate(ASQ)),
            post_lpf.shift.eq(1),
            self.o.payload[2].eq(0),
            self.o.payload[3].as_value().eq(ramp_asq_code),
        ]
        # mute the waveshaped outputs until the startup phase->h build completes.
        with m.If(net.ready):
            m.d.comb += [
                self.o.payload[0].eq(o_io.saturate(ASQ)),
                self.o.payload[1].eq(post_lpf.o.payload),
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

        # One PSRAM peripheral backing the phase->h table (built once at startup,
        # read-only during inference).
        self.psram_periph = psram.Peripheral(size=16 * 1024 * 1024)
        self.psram_periph.add_master(self.core.bus_h)

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
    weights_pkl = os.getenv("WEIGHTS_PKL")
    phase_h_payload = (
        _resolve_phase_h_payload_path(weights_pkl) if weights_pkl else None
    )
    if not phase_h_payload:
        raise FileNotFoundError(
            "phase_h.bin payload is required for preload-only PhaseHLutPS. "
            "Generate it with: "
            "uv run -m qkeras_v.export_phase_h_table --weights-pkl <...>/latest.pkl --out <run>/phase_h.bin "
            "then set PHASE_H_BIN or ensure it is alongside WEIGHTS_PKL run artifacts."
        )

    def _archiver_cb(archiver):
        archiver.with_option_storage()
        print(f"adding phase_h payload to archive: {phase_h_payload}")
        archiver.with_ramload_file(
            file_path=phase_h_payload,
            psram_dst=0,
            filename="phase_h.bin",
        )

    top_level_cli(
        CoreTop,
        video_core=False,
        path=this_path,
        archiver_callback=_archiver_cb,
    )
