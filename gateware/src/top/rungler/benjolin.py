from amaranth import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.wiring import In, Out

from tiliqua.build.types import BitstreamHelp
from tiliqua.dsp import ASQ

from rungler import Rungler


class Benjolin(wiring.Component):
    """
    Benjolin chaotic oscillator cross-modulated by a rungler.

    in0: osc1 pitch CV
    in1: osc2 pitch CV
    out0: osc1 square wave
    out1: osc2 square wave
    out2: rungler 3-bit DAC output
    """

    i: In(stream.Signature(data.ArrayLayout(ASQ, 4)))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 4)))

    bitstream_help = BitstreamHelp(
        brief="Benjolin rungler oscillator",
        io_left=[
            "osc1 pitch",
            "osc2 pitch",
            "",
            "",
            "osc1 out",
            "osc2 out",
            "rungler out",
            "",
        ],
        io_right=["", "", "", "", "", ""],
    )

    def __init__(self):
        self.phase_width = 16
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.rungler = rungler = Rungler()

        phase_acc1 = Signal(self.phase_width)
        phase_acc2 = Signal(self.phase_width)

        # default pitch to ~440hz and ~528hz ( 600 and 720 at 48kHz)
        osc1_pitch = Signal(self.phase_width, reset=600)
        osc2_pitch = Signal(self.phase_width, reset=720)

        # update oscs on audio tick
        # TODO: is there a better way to do this w.r.t. ADC clock?
        audio_strobe = Signal()
        m.d.comb += audio_strobe.eq(self.i.valid & self.i.ready)

        # latch pitch CV from input stream. ( treat as unsigned so we can handle both +/- values )
        with m.If(audio_strobe):
            m.d.sync += [
                osc1_pitch.eq(self.i.payload[0].as_value()[1:]),
                osc2_pitch.eq(self.i.payload[1].as_value()[1:]),
            ]

        # cross mod; rungler directs both oscillator frequencies
        freq_word1 = Signal(self.phase_width)
        freq_word2 = Signal(self.phase_width)
        m.d.comb += [
            freq_word1.eq(osc1_pitch + (rungler.rungler_out << (self.phase_width - 4))),
            freq_word2.eq(osc2_pitch + (rungler.rungler_out << (self.phase_width - 4))),
        ]

        # update accumulators once per audio sample
        with m.If(audio_strobe):
            m.d.sync += [
                phase_acc1.eq(phase_acc1 + freq_word1),
                phase_acc2.eq(phase_acc2 + freq_word2),
            ]

        # capture output square waves as MSB from accumulators (registered to avoid glitches)
        osc1_wave = Signal()
        osc2_wave = Signal()
        m.d.sync += [
            osc1_wave.eq(phase_acc1[-1]),
            osc2_wave.eq(phase_acc2[-1]),
        ]
        m.d.comb += [
            rungler.osc1_trigger.eq(osc1_wave),
            rungler.osc2_data.eq(osc2_wave),
        ]

        # handshake
        m.d.comb += [
            self.o.valid.eq(self.i.valid),
            self.i.ready.eq(self.o.ready),
        ]

        asq_w = ASQ.as_shape().width  # 16

        # map square wave 1 => +ve, 0 => -ve
        m.d.comb += [
            self.o.payload[0]
            .as_value()
            .eq(Cat(osc1_wave.replicate(asq_w - 1), ~osc1_wave)),
            self.o.payload[1]
            .as_value()
            .eq(Cat(osc2_wave.replicate(asq_w - 1), ~osc2_wave)),
            # 3-bit rungler: center bits
            self.o.payload[2]
            .as_value()
            .eq(Cat(Const(0, asq_w - 4), rungler.rungler_out) - (4 << (asq_w - 4))),
            self.o.payload[3].as_value().eq(0),
        ]

        return m
