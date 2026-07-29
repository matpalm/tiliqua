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
from tiliqua.build.types import BitstreamHelp, FirmwareLocation
from tiliqua.dsp import ASQ, block, spectral
from tiliqua.dsp.mix import CoeffUpdate
from tiliqua.periph import eurorack_pmod, psram
from tiliqua.platform import RebootProvider

# complete WIP hack :/ should be plumbed through from CLI
import os

INR_ROOT = "/home/mat/dev/inr_waveshaper/"
sys.path.insert(0, INR_ROOT)

# from amaranth_v.rff_concat_network import RffNetwork, load_weights_and_config
from amaranth_v.rff_film_network import RffNetwork
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
        # The phase->h table is preloaded into PSRAM from phase_h_lut.bin by the
        # bootloader (RamLoad region registered in the CLI below).
        # NOTE: base must stay clear of the bootloader framebuffer at PSRAM
        # offset 0 -- offset 0 is overwritten by the bootloader's video/persist
        # DMA before this bitstream boots. 0x800000 (8MiB) sits above firmware
        # and below the end-of-PSRAM bootinfo, with room for the 256KiB table.
        phase_h_psram_dst = int(os.getenv("PHASE_H_PSRAM_DST", "0x800000"), 0)
        phase_h_index_bits = int(os.getenv("PHASE_H_INDEX_BITS", "13"), 0)
        self.net = RffNetwork.build(
            WEIGHTS_PKL,
            psram_base=phase_h_psram_dst,
            index_bits=phase_h_index_bits,
        )
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
        # ASQ 1.0 == 8.192V.  RampVOct consumes the ASQ v/oct directly and
        # emits the phase ramp already in the network's io fixed-point shape, so
        # no intermediate re-quantisation is needed.
        ramp = RampVOct(i_shape=ASQ, o_shape=net.io_shape)
        ramp.V_MIN = RampVOct.V_MIN / 8.192
        ramp.V_MAX = RampVOct.V_MAX / 8.192
        ramp.F0_HZ = RampVOct.F0_HZ
        ramp.OCTAVES = RampVOct.OCTAVES
        ramp.RAMP_V = 0.5  # training data is +- 0.5
        m.submodules.ramp = ramp

        m.submodules.post_lpf = post_lpf = dsp.OnePole()

        # ASQ (audio) and the network's io fixed-point shape differ in scale;
        # non-phase inputs are aligned by fractional bits on the way in, and the
        # network output is saturated back into ASQ on the way out.
        io_f = net.io_shape.f_bits
        io_i = net.io_shape.i_bits
        IOQ = fixed.SQ(io_i, io_f)  # tiliqua-side view of the network io shape

        # Feed ASQ in0 straight into RampVOct; its phase ramp comes out already
        # in the network io shape.
        m.d.comb += [
            ramp.i.payload.eq(self.i.payload[0]),
            ramp.i.valid.eq(self.i.valid),
            ramp.o.ready.eq(net.i.ready),
        ]

        # map tiliqua inputs to network as required
        print("net in_d", self.net.in_d, "out_d", self.net.out_d)

        for ch in range(self.net.in_d):
            if ch == 0:
                m.d.comb += net.i.payload[ch].as_value().eq(ramp.o.payload.as_value())
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
        # ramp phase is +/-0.5 in the io shape; scale by (5/8.192)/0.5 = 625/512
        # then correct for the io/ASQ fractional-bit difference:
        #   ASQ_code = phase_code * 625 >> (9 + io_f - ASQ.f_bits)
        ramp_code = Signal(signed(net.io_shape.width))
        ramp_scaled_num = Signal(signed(net.io_shape.width + 10))
        ramp_asq_code = Signal(signed(ASQ.width))
        m.d.comb += [
            ramp_code.eq(ramp.o.payload.as_value()),
            ramp_scaled_num.eq(
                (ramp_code << 9)
                + (ramp_code << 6)
                + (ramp_code << 5)
                + (ramp_code << 4)
                + ramp_code
            ),
            ramp_asq_code.eq(ramp_scaled_num >> (9 + io_f - ASQ.f_bits)),
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

    def _archiver_callback(archiver):
        # Bundle phase_h_lut.bin so the bootloader copies it SPIFlash->PSRAM at
        # PHASE_H_PSRAM_DST before this bitstream starts; PhaseHLutPS then reads
        # it directly (preloaded mode) instead of rebuilding the table on-device.
        phase_h_bin = os.getenv("PHASE_H_BIN")
        if not phase_h_bin:
            raise RuntimeError(
                "PHASE_H_BIN must point to phase_h_lut.bin so it can be bundled "
                "as a RamLoad region"
            )
        phase_h_bin = os.path.abspath(os.path.expanduser(phase_h_bin))
        if not os.path.exists(phase_h_bin):
            raise FileNotFoundError(f"PHASE_H_BIN not found: {phase_h_bin}")

        phase_h_psram_dst = int(os.getenv("PHASE_H_PSRAM_DST", "0x800000"), 0)
        archiver.with_firmware(
            firmware_bin_path=phase_h_bin,
            fw_location=FirmwareLocation.PSRAM,
            fw_offset=phase_h_psram_dst,
        )
        return archiver.with_option_storage()

    top_level_cli(
        CoreTop,
        video_core=False,
        path=this_path,
        archiver_callback=_archiver_callback,
    )
