# Copyright (c) 2024 Seb Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: BSD--3-Clause

import os

from amaranth              import *
from amaranth.build        import *
from amaranth.lib          import wiring, data
from amaranth.lib.wiring   import In, Out


from amaranth_future       import stream, fixed

from tiliqua.tiliqua_platform import TiliquaPlatform
from tiliqua                  import eurorack_pmod, dsp
from tiliqua.eurorack_pmod    import ASQ

class MirrorTop(Elaboratable):
    """Route audio inputs straight to outputs (in the audio domain)."""

    def elaborate(self, platform):
        m = Module()

        m.submodules.car = platform.clock_domain_generator()

        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
                pmod_pins=platform.request("audio_ffc"),
                hardware_r33=True)

        m.submodules.audio_stream = audio_stream = eurorack_pmod.AudioStream(pmod0)

        wiring.connect(m, audio_stream.istream, audio_stream.ostream)

        return m

class SVFTop(Elaboratable):

    """High-, Low-, Bandpass with cutoff & resonance control."""

    def elaborate(self, platform):

        m = Module()

        m.submodules.car = platform.clock_domain_generator()

        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
                pmod_pins=platform.request("audio_ffc"),
                hardware_r33=True)

        m.submodules.audio_stream = audio_stream = eurorack_pmod.AudioStream(pmod0)

        m.submodules.svf0 = svf0 = dsp.SVF()

        # connect without 'wiring.connect' so we can see the payload field names.

        m.d.comb += [
            svf0.i.valid.eq(audio_stream.istream.valid),
            audio_stream.istream.ready.eq(svf0.i.ready),

            svf0.i.payload.x.eq(audio_stream.istream.payload[0]),
            svf0.i.payload.cutoff.eq(audio_stream.istream.payload[1]),
            svf0.i.payload.resonance.eq(audio_stream.istream.payload[2]),
        ]

        m.d.comb += [
            svf0.o.ready.eq(audio_stream.ostream.ready),
            audio_stream.ostream.valid.eq(svf0.o.valid),

            audio_stream.ostream.payload[0].eq(svf0.o.payload.lp),
            audio_stream.ostream.payload[1].eq(svf0.o.payload.hp),
            audio_stream.ostream.payload[2].eq(svf0.o.payload.bp),
        ]

        return m

class VCATop(Elaboratable):

    def elaborate(self, platform):
        m = Module()

        m.submodules.car = platform.clock_domain_generator()

        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
                pmod_pins=platform.request("audio_ffc"),
                hardware_r33=True)

        m.submodules.audio_stream = audio_stream = eurorack_pmod.AudioStream(pmod0)

        m.submodules.split4 = split4 = dsp.Split(n_channels=4)
        m.submodules.merge4 = merge4 = dsp.Merge(n_channels=4)

        m.submodules.merge2 = merge2 = dsp.Merge(n_channels=2)

        m.submodules.vca0 = vca0 = dsp.VCA()

        # connect with 'wiring.connect' to show how this works.

        wiring.connect(m, audio_stream.istream, split4.i)

        wiring.connect(m, split4.o[0], merge2.i[0])
        wiring.connect(m, split4.o[1], merge2.i[1])
        wiring.connect(m, split4.o[2], dsp.ASQ_READY)
        wiring.connect(m, split4.o[3], dsp.ASQ_READY)

        wiring.connect(m, merge2.o, vca0.i)
        wiring.connect(m, vca0.o, merge4.i[0])

        wiring.connect(m, dsp.ASQ_VALID, merge4.i[1])
        wiring.connect(m, dsp.ASQ_VALID, merge4.i[2])
        wiring.connect(m, dsp.ASQ_VALID, merge4.i[3])
        wiring.connect(m, merge4.o, audio_stream.ostream)

        return m

class DelayTop(Elaboratable):

    def elaborate(self, platform):
        m = Module()

        m.submodules.car = platform.clock_domain_generator()

        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
                pmod_pins=platform.request("audio_ffc"),
                hardware_r33=True)

        m.submodules.audio_stream = audio_stream = eurorack_pmod.AudioStream(pmod0)

        m.submodules.split4 = split4 = dsp.Split(n_channels=4)
        m.submodules.merge4 = merge4 = dsp.Merge(n_channels=4)

        m.submodules.mult2  = mult2  = dsp.Split(n_channels=2, replicate=True)
        m.submodules.mix2   = mix2   = dsp.Mix2()
        m.submodules.merge2 = merge2 = dsp.Merge(n_channels=2)

        m.submodules.delay_line = delay_line = dsp.DelayLine(max_delay=8192)

        wiring.connect(m, audio_stream.istream, split4.i)

        wiring.connect(m, split4.o[0], mult2.i)
        wiring.connect(m, split4.o[1], dsp.ASQ_READY)
        wiring.connect(m, split4.o[2], dsp.ASQ_READY)
        wiring.connect(m, split4.o[3], dsp.ASQ_READY)

        wiring.connect(m, mult2.o[0], delay_line.sw)

        m.d.comb += [
            delay_line.da.valid.eq(audio_stream.istream.valid),
            delay_line.da.payload.eq(delay_line.max_delay - 1),
        ]

        wiring.connect(m, mult2.o[1],    merge2.i[0])
        wiring.connect(m, delay_line.ds, merge2.i[1])

        wiring.connect(m, merge2.o, mix2.i)

        wiring.connect(m, mix2.o,        merge4.i[0])
        wiring.connect(m, dsp.ASQ_VALID, merge4.i[1])
        wiring.connect(m, dsp.ASQ_VALID, merge4.i[2])
        wiring.connect(m, dsp.ASQ_VALID, merge4.i[3])

        wiring.connect(m, merge4.o, audio_stream.ostream)

        return m

class PitchTop(Elaboratable):

    def elaborate(self, platform):
        m = Module()

        m.submodules.car = platform.clock_domain_generator()

        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
                pmod_pins=platform.request("audio_ffc"),
                hardware_r33=True)

        m.submodules.audio_stream = audio_stream = eurorack_pmod.AudioStream(pmod0)

        m.submodules.split4 = split4 = dsp.Split(n_channels=4)
        m.submodules.merge4 = merge4 = dsp.Merge(n_channels=4)

        m.submodules.delay_line = delay_line = dsp.DelayLine(max_delay=8192)
        m.submodules.pitch_shift = pitch_shift = dsp.PitchShift(
            delayln=delay_line, xfade=delay_line.max_delay//4)

        wiring.connect(m, audio_stream.istream, split4.i)

        # write audio samples to delay line
        wiring.connect(m, split4.o[0], delay_line.sw)

        # hook up 2nd input channel as pitch control, use fixed grain_sz
        m.d.comb += [
            split4.o[1].ready.eq(pitch_shift.i.ready),
            pitch_shift.i.valid.eq(split4.o[1].valid),
            pitch_shift.i.payload.pitch.eq(split4.o[1].payload.sas_value() >> 8),
            pitch_shift.i.payload.grain_sz.eq(delay_line.max_delay//2),
        ]

        wiring.connect(m, split4.o[2], dsp.ASQ_READY)
        wiring.connect(m, split4.o[3], dsp.ASQ_READY)

        # first channel is pitch shift output
        wiring.connect(m, pitch_shift.o, merge4.i[0])
        wiring.connect(m, dsp.ASQ_VALID, merge4.i[1])
        wiring.connect(m, dsp.ASQ_VALID, merge4.i[2])
        wiring.connect(m, dsp.ASQ_VALID, merge4.i[3])

        wiring.connect(m, merge4.o, audio_stream.ostream)

        return m

class MatrixTop(Elaboratable):

    def elaborate(self, platform):
        m = Module()

        m.submodules.car = platform.clock_domain_generator()

        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
                pmod_pins=platform.request("audio_ffc"),
                hardware_r33=True)

        m.submodules.audio_stream = audio_stream = eurorack_pmod.AudioStream(pmod0)

        m.submodules.matrix_mix = matrix_mix = dsp.MatrixMix(
            i_channels=4, o_channels=4,
            coefficients=[[0.4, 0.3, 0.2, 0.1],
                          [0.1, 0.4, 0.3, 0.2],
                          [0.2, 0.1, 0.4, 0.3],
                          [0.3, 0.2, 0.1, 0.4]])

        wiring.connect(m, audio_stream.istream, matrix_mix.i)
        wiring.connect(m, matrix_mix.o, audio_stream.ostream)

        return m

class DiffuserTop(Elaboratable):

    def elaborate(self, platform):
        m = Module()

        m.submodules.car = platform.clock_domain_generator()

        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
                pmod_pins=platform.request("audio_ffc"),
                hardware_r33=True)

        m.submodules.audio_stream = audio_stream = eurorack_pmod.AudioStream(pmod0)

        m.submodules.matrix_mix = matrix_mix = dsp.MatrixMix(
            i_channels=8, o_channels=8,
            coefficients=[[0.1, 0.0, 0.0, 0.0, 0.1, 0.2, 0.0, 0.0], # in0
                          [0.0, 0.1, 0.0, 0.0, 0.0, 0.1, 0.0, 0.0], #  |
                          [0.0, 0.0, 0.1, 0.0, 0.2, 0.0, 0.1, 0.2], #  |
                          [0.0, 0.0, 0.0, 0.1, 0.0, 0.0, 0.2, 0.1], # in3
                          [2.9, 0.0, 0.0, 0.0, 0.2, 0.1, 0.2, 0.2], # ds0
                          [0.0, 2.9, 0.0, 0.0, 0.4, 0.2, 0.4, 0.2], #  |
                          [0.0, 0.0, 2.9, 0.0, 0.2, 0.4, 0.1, 0.4], #  |
                          [0.0, 0.0, 0.0, 2.9, 0.1, 0.2, 0.2, 0.1]])# ds3
                          # out0 ------- out3  sw0 ---------- sw3

        delay_lines = [dsp.DelayLine(max_delay=8192) for n in range(4)]
        m.submodules += delay_lines

        m.d.comb += [delay_lines[n].da.valid.eq(1) for n in range(4)]
        m.d.comb += [
            delay_lines[0].da.payload.eq(8000),
            delay_lines[1].da.payload.eq(6666),
            delay_lines[2].da.payload.eq(7000),
            delay_lines[3].da.payload.eq(7200),
        ]

        m.submodules.split4 = split4 = dsp.Split(n_channels=4)
        m.submodules.merge4 = merge4 = dsp.Merge(n_channels=4)

        m.submodules.split8 = split8 = dsp.Split(n_channels=8)
        m.submodules.merge8 = merge8 = dsp.Merge(n_channels=8)

        wiring.connect(m, audio_stream.istream, split4.i)

        # matrix <-> independent streams
        wiring.connect(m, matrix_mix.o, split8.i)
        wiring.connect(m, merge8.o, matrix_mix.i)

        for n in range(4):
            # audio -> matrix [0-3]
            wiring.connect(m, split4.o[n], merge8.i[n])
            # delay -> matrix [4-7]
            wiring.connect(m, delay_lines[n].ds, merge8.i[4+n])

        for n in range(4):
            # matrix -> audio [0-3]
            wiring.connect(m, split8.o[n], merge4.i[n])
            # matrix -> delay [4-7]
            wiring.connect(m, split8.o[4+n], delay_lines[n].sw)

        wiring.connect(m, merge4.o, audio_stream.ostream)

        return m

def build_mirror():
    os.environ["AMARANTH_verbose"] = "1"
    os.environ["AMARANTH_debug_verilog"] = "1"
    TiliquaPlatform().build(MirrorTop())

def build_svf():
    os.environ["AMARANTH_verbose"] = "1"
    os.environ["AMARANTH_debug_verilog"] = "1"
    TiliquaPlatform().build(SVFTop())

def build_vca():
    os.environ["AMARANTH_verbose"] = "1"
    os.environ["AMARANTH_debug_verilog"] = "1"
    TiliquaPlatform().build(VCATop())

def build_delay():
    os.environ["AMARANTH_verbose"] = "1"
    os.environ["AMARANTH_debug_verilog"] = "1"
    TiliquaPlatform().build(DelayTop())

def build_pitch():
    os.environ["AMARANTH_verbose"] = "1"
    os.environ["AMARANTH_debug_verilog"] = "1"
    TiliquaPlatform().build(PitchTop())

def build_matrix():
    os.environ["AMARANTH_verbose"] = "1"
    os.environ["AMARANTH_debug_verilog"] = "1"
    TiliquaPlatform().build(MatrixTop())

def build_diffuser():
    os.environ["AMARANTH_verbose"] = "1"
    os.environ["AMARANTH_debug_verilog"] = "1"
    TiliquaPlatform().build(DiffuserTop())
