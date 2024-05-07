# Copyright (c) 2024 Seb Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: BSD--3-Clause

import os

from amaranth              import *
from amaranth.build        import *
from amaranth.lib          import wiring, data
from amaranth.lib.wiring   import In, Out

from amaranth.lib.fifo     import AsyncFIFO

from amaranth_future       import stream, fixed

from tiliqua.tiliqua_platform import TiliquaPlatform
from tiliqua                  import eurorack_pmod
from tiliqua.eurorack_pmod    import ASQ

class AudioStream(wiring.Component):

    """
    Domain crossing logic to move samples from `eurorack-pmod` logic in the audio domain
    to logic in a different (faster) domain using a stream interface.
    """

    istream: Out(stream.Signature(data.ArrayLayout(ASQ, 4)))
    ostream: In(stream.Signature(data.ArrayLayout(ASQ, 4)))

    def __init__(self, eurorack_pmod, stream_domain="sync", fifo_depth=8):

        self.eurorack_pmod = eurorack_pmod
        self.stream_domain = stream_domain
        self.fifo_depth = fifo_depth

        super().__init__()

    def elaborate(self, platform) -> Module:

        m = Module()

        m.submodules.adc_fifo = adc_fifo = AsyncFIFO(
                width=self.eurorack_pmod.sample_i.shape().size, depth=self.fifo_depth,
                w_domain="audio", r_domain=self.stream_domain)
        m.submodules.dac_fifo = dac_fifo = AsyncFIFO(
                width=self.eurorack_pmod.sample_o.shape().size, depth=self.fifo_depth,
                w_domain=self.stream_domain, r_domain="audio")

        adc_stream = stream.fifo_r_stream(adc_fifo)
        dac_stream = wiring.flipped(stream.fifo_w_stream(dac_fifo))

        wiring.connect(m, adc_stream, wiring.flipped(self.istream))
        wiring.connect(m, wiring.flipped(self.ostream), dac_stream)

        eurorack_pmod = self.eurorack_pmod

        # below is synchronous logic in the *audio domain*

        # On every fs_strobe, latch and write all channels concatenated
        # into one entry of adc_fifo.

        m.d.audio += [
            # WARN: ignoring rdy in write domain. Mostly fine as long as
            # stream_domain is faster than audio_domain.
            adc_fifo.w_en.eq(eurorack_pmod.fs_strobe),
            adc_fifo.w_data.eq(self.eurorack_pmod.sample_i),
        ]


        # Once fs_strobe hits, write the next pending samples to CODEC

        with m.FSM(domain="audio") as fsm:
            with m.State('READ'):
                with m.If(eurorack_pmod.fs_strobe & dac_fifo.r_rdy):
                    m.d.audio += dac_fifo.r_en.eq(1)
                    m.next = 'SEND'
            with m.State('SEND'):
                m.d.audio += [
                    dac_fifo.r_en.eq(0),
                    self.eurorack_pmod.sample_o.eq(dac_fifo.r_data),
                ]
                m.next = 'READ'

        return m

class Split(wiring.Component):

    def __init__(self, n_channels):
        self.n_channels = n_channels
        super().__init__({
            "i": In(stream.Signature(data.ArrayLayout(ASQ, n_channels))),
            "o": Out(stream.Signature(ASQ)).array(n_channels),
        })

    def elaborate(self, platform):
        m = Module()

        done = Signal(self.n_channels)

        m.d.comb += self.i.ready.eq(Cat([self.o[n].ready | done[n] for n in range(self.n_channels)]).all())
        m.d.comb += [self.o[n].payload.eq(self.i.payload[n]) for n in range(self.n_channels)]
        m.d.comb += [self.o[n].valid.eq(self.i.valid & ~done[n]) for n in range(self.n_channels)]

        flow = [self.o[n].valid & self.o[n].ready
                for n in range(self.n_channels)]
        end  = Cat([flow[n] | done[n]
                    for n in range(self.n_channels)]).all()
        with m.If(end):
            m.d.sync += done.eq(0)
        with m.Else():
            for n in range(self.n_channels):
                with m.If(flow[n]):
                    m.d.sync += done[n].eq(1)

        return m

class Merge(wiring.Component):

    def __init__(self, n_channels):
        self.n_channels = n_channels
        super().__init__({
            "i": In(stream.Signature(ASQ)).array(n_channels),
            "o": Out(stream.Signature(data.ArrayLayout(ASQ, n_channels))),
        })

    def elaborate(self, platform):
        m = Module()

        m.d.comb += [self.i[n].ready.eq(self.o.ready & self.o.valid) for n in range(self.n_channels)]
        m.d.comb += [self.o.payload[n].eq(self.i[n].payload) for n in range(self.n_channels)]
        m.d.comb += self.o.valid.eq(Cat([self.i[n].valid for n in range(self.n_channels)]).all())

        return m

class VCA(wiring.Component):

    i: In(stream.Signature(data.ArrayLayout(ASQ, 2)))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 1)))

    def elaborate(self, platform):
        m = Module()

        m.d.comb += [
            self.o.payload[0].eq(self.i.payload[0] * self.i.payload[1]),
            self.o.valid.eq(self.i.valid),
            self.i.ready.eq(self.o.ready),
        ]

        return m

class NCO(wiring.Component):

    i: In(stream.Signature(ASQ))
    o: Out(stream.Signature(ASQ))

    def elaborate(self, platform):
        m = Module()

        s = Signal(fixed.SQ(16, eurorack_pmod.WIDTH-1))

        m.d.comb += [
            self.o.valid.eq(self.i.valid),
            self.i.ready.eq(self.o.ready),
        ]

        with m.If(self.i.valid):
            m.d.sync += [
                s.eq(s + self.i.payload),
                self.o.payload.eq(s.round() >> 6),
            ]

        return m

class SVF(wiring.Component):

    i: In(stream.Signature(ASQ))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 3)))

    def elaborate(self, platform):
        m = Module()

        # is this stable with only 18 bits? (native multiplier width)
        dtype = fixed.SQ(2, eurorack_pmod.WIDTH-1)

        abp   = Signal(dtype)
        alp   = Signal(dtype)
        ahp   = Signal(dtype)
        x     = Signal(dtype)
        kK    = fixed.Const(0.3, dtype)
        kQinv = fixed.Const(0.1, dtype)

        with m.FSM() as fsm:
            with m.State('WAIT-VALID'):
                m.d.comb += self.i.ready.eq(1),
                with m.If(self.i.valid):
                   m.d.sync += x.eq(self.i.payload)
                   m.next = 'MAC0'
            with m.State('MAC0'):
                m.d.sync += alp.eq(abp*kK + alp)
                m.next = 'MAC1'
            with m.State('MAC1'):
                m.d.sync += ahp.eq(x - alp - kQinv*abp)
                m.next = 'MAC2'
            with m.State('MAC2'):
                m.d.sync += abp.eq(ahp*kK + abp)
                m.next = 'WAIT-READY'
            with m.State('WAIT-READY'):
                m.d.comb += [
                    self.o.valid.eq(1),
                    self.o.payload[0].eq(ahp),
                    self.o.payload[1].eq(alp),
                    self.o.payload[2].eq(abp),
                ]
                with m.If(self.o.ready):
                    m.next = 'WAIT-VALID'

        return m

class MirrorTop(Elaboratable):
    """Route audio inputs straight to outputs (in the audio domain)."""

    def elaborate(self, platform):
        m = Module()

        m.submodules.car = platform.clock_domain_generator()

        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
                pmod_pins=platform.request("audio_ffc"),
                hardware_r33=True)

        m.submodules.audio_stream = audio_stream = AudioStream(pmod0)

        wiring.connect(m, audio_stream.istream, audio_stream.ostream)

        return m

class VCATop(Elaboratable):

    def elaborate(self, platform):
        m = Module()

        m.submodules.car = platform.clock_domain_generator()

        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
                pmod_pins=platform.request("audio_ffc"),
                hardware_r33=True)

        m.submodules.audio_stream = audio_stream = AudioStream(pmod0)

        m.submodules.split4 = split4 = Split(n_channels=4)
        m.submodules.merge4 = merge4 = Merge(n_channels=4)
        m.submodules.split3 = split3 = Split(n_channels=3)

        #m.submodules.vca0 = vca0 = VCA()
        #m.submodules.nco0 = nco0 = NCO()
        m.submodules.svf0 = svf0 = SVF()

        ready_stub = stream.Signature(ASQ, always_ready=True).flip().create()
        valid_stub = stream.Signature(ASQ, always_valid=True).create()

        wiring.connect(m, audio_stream.istream, split4.i)

        wiring.connect(m, split4.o[0], svf0.i)
        wiring.connect(m, split4.o[1], ready_stub)
        wiring.connect(m, split4.o[2], ready_stub)
        wiring.connect(m, split4.o[3], ready_stub)

        wiring.connect(m, svf0.o, split3.i)

        wiring.connect(m, split3.o[0], merge4.i[0])
        wiring.connect(m, split3.o[1], merge4.i[1])
        wiring.connect(m, split3.o[2], merge4.i[2])
        wiring.connect(m, valid_stub,  merge4.i[3])
        wiring.connect(m, merge4.o, audio_stream.ostream)

        return m

def build_mirror():
    os.environ["AMARANTH_verbose"] = "1"
    os.environ["AMARANTH_debug_verilog"] = "1"
    TiliquaPlatform().build(MirrorTop())

def build_vca():
    os.environ["AMARANTH_verbose"] = "1"
    os.environ["AMARANTH_debug_verilog"] = "1"
    TiliquaPlatform().build(VCATop())
