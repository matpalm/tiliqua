# Copyright (c) 2024 S. Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#


import math
from scipy.interpolate import CubicHermiteSpline
import sys

import os

from amaranth import *
from amaranth.build import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.cdc import FFSynchronizer
from amaranth.lib.wiring import In, Out
from amaranth_soc import wishbone
from amaranth_future import fixed

from tiliqua import dsp, midi
from tiliqua.build import sim
from tiliqua.build.cli import top_level_cli
from tiliqua.build.types import BitstreamHelp
from tiliqua.dsp import ASQ, block
from tiliqua.dsp.complex import CQ
from tiliqua.dsp.mix import CoeffUpdate
from tiliqua.periph import eurorack_pmod, psram
from tiliqua.platform import RebootProvider

from spectral_morph import SpectralMagnitudeMorph


class STFTMirror(wiring.Component):
    """
    STFT-based cross-morph.

    Channel 0 is analyzed as the carrier source, channel 1 provides the
    modulator magnitudes, channel 2 is mapped to the crossfade alpha,
    and the morphed signal is emitted on channel 0 only.
    """

    i: In(stream.Signature(data.ArrayLayout(ASQ, 4)))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 4)))

    bitstream_help = BitstreamHelp(
        brief="STFT spectral cross-morph",
        io_left=["in0 carrier", "in1 mod", "in2 xfade", "", "out0 morph", "", "", ""],
        io_right=["", "", "", "", "", ""],
    )

    def elaborate(self, platform):
        m = Module()

        stft_sz = 512

        m.submodules.analyzer0 = analyzer0 = dsp.fft.STFTAnalyzer(
            sz=stft_sz,
            shape=ASQ,
            window_function=dsp.fft.Window.Function.SQRT_HANN,
        )
        m.submodules.analyzer1 = analyzer1 = dsp.fft.STFTAnalyzer(
            sz=stft_sz,
            shape=ASQ,
            window_function=dsp.fft.Window.Function.SQRT_HANN,
        )
        m.submodules.morph = morph = SpectralMagnitudeMorph(
            shape=ASQ,
            sz=stft_sz,
        )
        m.submodules.synth = synth = dsp.fft.STFTSynthesizer(
            sz=stft_sz,
            shape=ASQ,
            window_function=dsp.fft.Window.Function.SQRT_HANN,
        )

        m.submodules.split4 = split4 = dsp.Split(n_channels=4)
        m.submodules.merge4 = merge4 = dsp.Merge(n_channels=4)
        wiring.connect(m, wiring.flipped(self.i), split4.i)
        wiring.connect(m, merge4.o, wiring.flipped(self.o))

        alpha = Signal(ASQ, init=fixed.Const(0.5, shape=ASQ))
        alpha_target = Signal(ASQ, init=fixed.Const(0.5, shape=ASQ))
        alpha_hs = Signal()
        m.d.comb += [
            alpha_hs.eq(split4.o[2].valid & split4.o[2].ready),
            split4.o[2].ready.eq(1),
            alpha_target.eq(
                (split4.o[2].payload >> 1) + fixed.Const(0.5, shape=ASQ, clamp=True)
            ),
            morph.alpha.eq(alpha),
        ]
        with m.If(alpha_hs):
            m.d.sync += alpha.eq(alpha + ((alpha_target - alpha) >> 3))

        wiring.connect(m, split4.o[0], analyzer0.i)
        wiring.connect(m, split4.o[1], analyzer1.i)
        wiring.connect(m, analyzer0.o, morph.i_carrier)
        wiring.connect(m, analyzer1.o, morph.i_modulator)
        wiring.connect(m, morph.o, synth.i)
        wiring.connect(m, synth.o, merge4.i[0])

        split4.wire_ready(m, [3])
        merge4.wire_valid(m, [1, 2, 3])

        return m


class CoreTop(Elaboratable):

    def __init__(self, clock_settings):
        self.core = STFTMirror()
        self.core.audio_clock = clock_settings.audio_clock
        self.touch = False
        self.clock_settings = clock_settings
        self.pmod0 = eurorack_pmod.EurorackPmod(clock_settings.audio_clock)

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
        # m.d.comb += self.core.jack.eq(pmod0.jack)

        # m.submodules.psram_periph = self.psram_periph

        return m


if __name__ == "__main__":
    this_path = os.path.dirname(os.path.realpath(__file__))

    # def _archiver_callback(archiver):
    #     # Bundle phase_h_lut.bin so the bootloader copies it SPIFlash->PSRAM at
    #     # PHASE_H_PSRAM_DST before this bitstream starts; PhaseHLutPS then reads
    #     # it directly (preloaded mode) instead of rebuilding the table on-device.
    #     phase_h_bin = os.getenv("PHASE_H_BIN")
    #     if not phase_h_bin:
    #         raise RuntimeError(
    #             "PHASE_H_BIN must point to phase_h_lut.bin so it can be bundled "
    #             "as a RamLoad region"
    #         )
    #     phase_h_bin = os.path.abspath(os.path.expanduser(phase_h_bin))
    #     if not os.path.exists(phase_h_bin):
    #         raise FileNotFoundError(f"PHASE_H_BIN not found: {phase_h_bin}")

    #     phase_h_psram_dst = int(os.getenv("PHASE_H_PSRAM_DST", "0x800000"), 0)
    #     archiver.with_firmware(
    #         firmware_bin_path=phase_h_bin,
    #         fw_location=FirmwareLocation.PSRAM,
    #         fw_offset=phase_h_psram_dst,
    #     )
    #     # don't need option_storage ( more room for phase_h.bin! )
    #     return archiver

    top_level_cli(
        CoreTop,
        video_core=False,
        path=this_path,
        # archiver_callback=_archiver_callback,
    )
