"""
Paula recorder/playback scaffold using on-chip EBR.

Implements two independent mono sampler paths:

Recording is deterministically decimated from 48 kHz input to 16726 Hz
capture rate using a fixed phase accumulator.
"""

from amaranth import *
from amaranth.lib import cdc, wiring

from tiliqua import midi
from tiliqua.build.cli import top_level_cli
from tiliqua.build.types import BitstreamHelp
from tiliqua.periph import eurorack_pmod
from tiliqua.platform import RebootProvider

from core import PaulaRecorderCore

class PaulaTop(Elaboratable):

    bitstream_help = BitstreamHelp(
        brief="Paula record/playback (EBR, 2s max)",
        io_left=[
            "sample0 in",
            "sample1 in",
            "cv reserve 0",
            "",
            "sample0 out",
            "sample1 out",
            "sample1 lp out",
            "",
        ],
        io_right=["", "", "", "", "", "TRS MIDI in"],
    )

    def __init__(self, clock_settings):
        self.clock_settings = clock_settings
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.car = car = platform.clock_domain_generator(self.clock_settings)
        m.submodules.reboot = reboot = RebootProvider(car.settings.frequencies.sync)
        m.submodules.btn = cdc.FFSynchronizer(
            platform.request("encoder").s.i, reboot.button
        )

        m.submodules.pmod0_provider = pmod0_provider = eurorack_pmod.FFCProvider()
        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
            self.clock_settings.audio_clock
        )
        wiring.connect(m, pmod0.pins, pmod0_provider.pins)
        m.d.comb += pmod0.codec_mute.eq(reboot.mute)

        m.submodules.core = core = PaulaRecorderCore()
        wiring.connect(m, pmod0.o_cal, core.i)
        wiring.connect(m, core.o, pmod0.i_cal)

        midi_pins = platform.request("midi")
        m.submodules.serialrx = serialrx = midi.SerialRx(
            system_clk_hz=60e6, pins=midi_pins
        )
        m.submodules.midi_decode = midi_decode = midi.MidiDecodeSerial()
        wiring.connect(m, serialrx.o, midi_decode.i)
        wiring.connect(m, midi_decode.o, core.i_midi)

        return m


if __name__ == "__main__":
    top_level_cli(PaulaTop, video_core=False)
