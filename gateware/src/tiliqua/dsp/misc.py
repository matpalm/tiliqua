# Copyright (c) 2024 S. Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0

from amaranth import *
from amaranth.lib import stream, wiring
from amaranth.lib.wiring import In, Out

from . import ASQ, asq_from_volts


def named_submodules(m_submodules, elaboratables, override_name=None):
    """
    Normally, using constructs like:

    .. code-block:: python

        m.submodules += delaylines

    You get generated code with names like U$14 ... as Amaranth's
    namer doesn't give such modules a readable name.

    Instead, you can do:

    .. code-block:: python

        named_submodules(m.submodules, delaylines)

    And this helper will give each instance a name.

    TODO: is there an idiomatic way of doing this?
    """
    if override_name is None:
        [setattr(m_submodules, f"{type(e).__name__.lower()}{i}", e) for i, e in enumerate(elaboratables)]
    else:
        [setattr(m_submodules, f"{override_name}{i}", e) for i, e in enumerate(elaboratables)]


class GateDetector(wiring.Component):
    """
    Detect gate transitions from a CV input with hysteresis.

    Output goes high when :py:`i` exceeds :py:`threshold_on` and low
    when it falls below :py:`threshold_off`.
    """

    def __init__(self, threshold_on=asq_from_volts(4.0), threshold_off=asq_from_volts(2.0)):
        self.threshold_on = threshold_on
        self.threshold_off = threshold_off
        super().__init__({
            "i": In(stream.Signature(ASQ)),
            "o": Out(stream.Signature(unsigned(1))),
        })

    def elaborate(self, platform):
        m = Module()

        gate_reg = Signal(init=0)

        m.d.comb += [
            self.o.payload.eq(gate_reg),
            self.o.valid.eq(self.i.valid),
            self.i.ready.eq(self.o.ready),
        ]

        with m.If(self.i.valid & self.o.ready):
            with m.If(gate_reg):
                with m.If(self.i.payload < self.threshold_off):
                    m.d.sync += gate_reg.eq(0)
            with m.Else():
                with m.If(self.i.payload > self.threshold_on):
                    m.d.sync += gate_reg.eq(1)

        return m


class CountingFollower(wiring.Component):
    """
    Simple unsigned counting follower.

    Output follows the input, getting closer to it by 1 count per :py:`valid` strobe.

    This is quite a cheap way to avoid pops on envelopes.
    """

    def __init__(self, bits=8):
        super().__init__({
            "i": In(stream.Signature(unsigned(bits))),
            "o": Out(stream.Signature(unsigned(bits))),
        })

    def elaborate(self, platform):
        m = Module()
        m.d.comb += self.i.ready.eq(self.o.ready)
        m.d.comb += self.o.valid.eq(self.i.valid)
        with m.If(self.i.valid & self.o.ready):
            with m.If(self.o.payload < self.i.payload):
                m.d.sync += self.o.payload.eq(self.o.payload + 1)
            with m.Elif(self.o.payload > self.i.payload):
                m.d.sync += self.o.payload.eq(self.o.payload - 1)
        return m


class Duplicate(wiring.Component):
    """
    Simple 'upsampler' that duplicates each input sample N times.

    No filtering is performed - each input sample is simply repeated N times
    in the output stream.

    Members
    -------
    i : :py:`In(stream.Signature(ASQ))`
        Input stream for samples to be upsampled.
    o : :py:`Out(stream.Signature(ASQ))`
        Output stream producing each input sample N times.
    """

    i: In(stream.Signature(ASQ))
    o: Out(stream.Signature(ASQ))

    def __init__(self, n: int):
        self.n = n
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        if self.n == 1:
            wiring.connect(m, wiring.flipped(self.i), self.o)
        else:
            output_count = Signal(range(self.n + 1), init=0)
            current_sample = Signal(ASQ)
            m.d.comb += self.i.ready.eq(output_count == 0)
            m.d.comb += self.o.valid.eq(output_count > 0)
            m.d.comb += self.o.payload.eq(current_sample)
            with m.If(self.i.valid & self.i.ready):
                m.d.sync += [
                    current_sample.eq(self.i.payload),
                    output_count.eq(self.n),
                ]
            with m.If(self.o.valid & self.o.ready):
                m.d.sync += output_count.eq(output_count - 1)

        return m
