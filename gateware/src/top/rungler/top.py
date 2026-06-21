import sys

from amaranth import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.cdc import FFSynchronizer
from amaranth.lib.wiring import In, Out

from tiliqua.build import sim
from tiliqua.build.cli import top_level_cli
from tiliqua.periph import eurorack_pmod
from tiliqua.platform import RebootProvider

from benjolin import Benjolin

class CoreTop(Elaboratable):
    def __init__(self, clock_settings):
        self.core = Benjolin()
        self.core.audio_clock = clock_settings.audio_clock
        self.clock_settings = clock_settings
        self.pmod0 = eurorack_pmod.EurorackPmod(clock_settings.audio_clock)
        self.bitstream_help = self.core.bitstream_help
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.pmod0 = pmod0 = self.pmod0
        if sim.is_hw(platform):
            m.submodules.car = platform.clock_domain_generator(self.clock_settings)
            m.submodules.provider = provider = eurorack_pmod.FFCProvider()
            wiring.connect(m, pmod0.pins, provider.pins)

            m.submodules.reboot = reboot = RebootProvider(
                self.clock_settings.frequencies.sync
            )
            m.submodules.btn = FFSynchronizer(
                platform.request("encoder").s.i,
                reboot.button,
            )
            m.d.comb += pmod0.codec_mute.eq(reboot.mute)
        else:
            m.submodules.car = sim.FakeTiliquaDomainGenerator()

        m.submodules.core = self.core
        wiring.connect(m, pmod0.o_cal, self.core.i)
        wiring.connect(m, self.core.o, pmod0.i_cal)

        return m


if __name__ == "__main__":
    top_level_cli(CoreTop, video_core=False)
