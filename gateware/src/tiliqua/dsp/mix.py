# Copyright (c) 2024 S. Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0

import enum

from amaranth import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out
from amaranth.utils import exact_log2

from amaranth_future import fixed

from . import ASQ, mac
from .block import Block


class CoeffUpdate(enum.Enum):
    NONE  = "none"
    XY    = "xy"
    BLOCK = "block"


class MatrixMix(wiring.Component):

    """
    Matrix mixer with tunable coefficients and configurable
    input & output channel count. Uses a single multiplier.

    Coefficients must fit inside the self.ctype declared below.
    Coefficient update mode is selected by ``coeff_update``:

    - ``CoeffUpdate.NONE``:  No update port.
    - ``CoeffUpdate.XY``:    Stream of ``(o_x, i_y, v)`` updates.
    - ``CoeffUpdate.BLOCK``: Block stream, mapped row-major to coefficients.
    """

    def __init__(self, i_channels, o_channels, coefficients,
                 coeff_update=CoeffUpdate.XY):

        assert(len(coefficients)       == i_channels)
        assert(len(coefficients[0])    == o_channels)

        self.i_channels = i_channels
        self.o_channels = o_channels
        self.coeff_update = coeff_update

        self.ctype = mac.SQNative

        coefficients_flat = [
            fixed.Const(x, shape=self.ctype)
            for xs in coefficients
            for x in xs
        ]

        assert(len(coefficients_flat) == i_channels*o_channels)

        # matrix coefficient memory
        self.mem = Memory(
            shape=self.ctype,
            depth=i_channels*o_channels, init=coefficients_flat)

        ports = {
            "i": In(stream.Signature(data.ArrayLayout(ASQ, i_channels))),
            "o": Out(stream.Signature(data.ArrayLayout(ASQ, o_channels))),
        }

        if coeff_update == CoeffUpdate.XY:
            ports["c"] = In(stream.Signature(data.StructLayout({
                "o_x": unsigned(exact_log2(self.o_channels)),
                "i_y": unsigned(exact_log2(self.i_channels)),
                "v":   self.ctype
            })))
        elif coeff_update == CoeffUpdate.BLOCK:
            ports["c"] = In(stream.Signature(Block(ASQ)))

        super().__init__(ports)

    def elaborate(self, platform):
        m = Module()

        m.submodules.mem = self.mem
        wport = self.mem.write_port()
        rport = self.mem.read_port(transparent_for=(wport,))

        i_latch = Signal(data.ArrayLayout(self.ctype, self.i_channels))
        o_accum = Signal(data.ArrayLayout(
            mac.SQRNative, self.o_channels))

        i_ch   = Signal(exact_log2(self.i_channels))
        o_ch   = Signal(exact_log2(self.o_channels))
        # i/o channel index, one cycle behind.
        l_i_ch = Signal(exact_log2(self.i_channels))
        o_ch_l = Signal(exact_log2(self.o_channels))
        # we've finished all accumulation steps.
        done = Signal(1)

        m.d.comb += [
            rport.en.eq(1),
            rport.addr.eq(Cat(o_ch, i_ch)),
        ]

        read0 = Signal(self.ctype)

        # coefficient update logic

        if self.coeff_update == CoeffUpdate.XY:
            m.d.comb += [
                self.c.ready.eq(1),
                wport.addr.eq(Cat(self.c.payload.o_x, self.c.payload.i_y)),
                wport.en.eq(self.c.valid),
                wport.data.eq(self.c.payload.v),
            ]
        elif self.coeff_update == CoeffUpdate.BLOCK:
            blk_ix_reg = Signal(range(self.i_channels * self.o_channels))
            blk_ix = Signal.like(blk_ix_reg)
            m.d.comb += blk_ix.eq(
                Mux(self.c.payload.first, 0, blk_ix_reg))
            m.d.comb += [
                self.c.ready.eq(1),
                wport.addr.eq(blk_ix),
                wport.en.eq(self.c.valid),
                wport.data.eq(self.c.payload.sample),
            ]
            with m.If(self.c.valid):
                m.d.sync += blk_ix_reg.eq(blk_ix + 1)

        # main multiplications state machine

        with m.FSM() as fsm:
            with m.State('WAIT-VALID'):
                m.d.comb += self.i.ready.eq(1),
                with m.If(self.i.valid):
                    m.d.sync += [
                        o_accum.eq(0),
                        i_ch.eq(0),
                        o_ch.eq(0),
                        done.eq(0),
                    ]
                    # FIXME: assigning each element of the payload is necessary
                    # because assignment of a data.ArrayLayout ignores the
                    # underlying fixed-point types. This should be cleaner!
                    m.d.sync += [
                        i_latch[n].eq(self.i.payload[n])
                        for n in range(self.i_channels)
                    ]
                    m.next = 'NEXT'
            with m.State('NEXT'):
                m.next = 'MAC'
                m.d.sync += [
                    o_ch_l.eq(o_ch),
                    l_i_ch.eq(i_ch),
                ]
                with m.If(o_ch == (self.o_channels - 1)):
                    m.d.sync += o_ch.eq(0)
                    with m.If(i_ch == (self.i_channels - 1)):
                        m.d.sync += done.eq(1)
                    with m.Else():
                        m.d.sync += i_ch.eq(i_ch+1)
                with m.Else():
                    m.d.sync += o_ch.eq(o_ch+1)
            with m.State('MAC'):
                m.next = 'NEXT'
                m.d.sync += [
                    o_accum[o_ch_l].eq(o_accum[o_ch_l] +
                                       (rport.data *
                                        i_latch[l_i_ch]))
                ]
                with m.If(done):
                    m.next = 'LATCH'
            with m.State('LATCH'):
                m.d.sync += [
                    self.o.payload[n].eq(o_accum[n].saturate(ASQ))
                    for n in range(self.o_channels)
                ]
                m.next = 'WAIT-READY'
            with m.State('WAIT-READY'):
                m.d.comb += [
                    self.o.valid.eq(1),
                ]
                with m.If(self.o.ready):
                    m.next = 'WAIT-VALID'

        return m
