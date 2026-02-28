# Copyright (c) 2024 Seb Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0

"""Utility for playing back sections from audio delay lines."""

from amaranth import *
from amaranth.lib import stream, wiring, enum
from amaranth.lib.wiring import In, Out

from amaranth_future import fixed
from amaranth_soc import csr

from ..dsp import ASQ, delay_line
from ..dsp.stream_util import Merge, Split, connect_feedback_kick

class Peripheral(wiring.Component):

    """
    SoC peripheral for `GrainPlayer`, a DMA core which reads samples from
    sections of a `DelayLine`, with adjustable start, stop, speed and
    loop settings. Playback can be triggered with CSR writes or connected
    to a hardware gate trigger with `hw_gate_enable`.
    """

    class ControlReg(csr.Register, access="rw"):
        gate: csr.Field(csr.action.RW, unsigned(1))
        mode: csr.Field(csr.action.RW, unsigned(3))
        reverse: csr.Field(csr.action.RW, unsigned(1))
        hw_gate_enable: csr.Field(csr.action.RW, unsigned(1))

    class SpeedReg(csr.Register, access="rw"):
        speed: csr.Field(csr.action.RW, unsigned(16))

    class StartReg(csr.Register, access="rw"):
        start: csr.Field(csr.action.RW, unsigned(32))

    class LengthReg(csr.Register, access="rw"):
        length: csr.Field(csr.action.RW, unsigned(32))

    class StatusReg(csr.Register, access="r"):
        position: csr.Field(csr.action.R, unsigned(32))

    def __init__(self, delayln):
        self._delayln = delayln
        self._grain_player = GrainPlayer(delayln)

        regs = csr.Builder(addr_width=5, data_width=8)
        self._control = regs.add("control", self.ControlReg(), offset=0x00)
        self._speed = regs.add("speed", self.SpeedReg(), offset=0x04)
        self._start = regs.add("start", self.StartReg(), offset=0x08)
        self._length = regs.add("length", self.LengthReg(), offset=0x0C)
        self._status = regs.add("status", self.StatusReg(), offset=0x10)
        self._bridge = csr.Bridge(regs.as_memory_map())

        super().__init__({
            "csr_bus": In(csr.Signature(addr_width=regs.addr_width, data_width=regs.data_width)),
            # Hardware gate used for triggering if `control.hw_gate_enable` is asserted.
            "hw_gate": In(1),
            # Scrub CV (only used by GrainPlayer in SCRUB mode)
            "scrub": In(stream.Signature(ASQ)),
            "o": Out(stream.Signature(ASQ)),
        })

        self.csr_bus.memory_map = self._bridge.bus.memory_map

    def elaborate(self, platform) -> Module:
        m = Module()

        m.submodules.bridge = self._bridge
        m.submodules.grain_player = grain_player = self._grain_player

        wiring.connect(m, wiring.flipped(self.csr_bus), self._bridge.bus)

        # Select between hw/rtl gate and CSR (cpu)-triggered gate.
        effective_gate = Mux(self._control.f.hw_gate_enable.data,
                             self.hw_gate,
                             self._control.f.gate.data)

        m.d.comb += [
            grain_player.gate.eq(effective_gate),
            grain_player.mode.eq(self._control.f.mode.data),
            grain_player.reverse.eq(self._control.f.reverse.data),
            grain_player.start.eq(self._start.f.start.data),
            grain_player.length.eq(self._length.f.length.data),
            grain_player.speed.as_value().eq(self._speed.f.speed.data),
            self._status.f.position.r_data.eq(grain_player.position),
        ]

        wiring.connect(m, wiring.flipped(self.scrub), grain_player.scrub)
        wiring.connect(m, grain_player.o, wiring.flipped(self.o))

        return m

class GrainPlayer(wiring.Component):

    class Mode(enum.Enum, shape=unsigned(3)):
        GATE    = 0  # Play while gate high, stop when low, reset on rising edge
        ONESHOT = 1  # Play full length ignoring gate, restart on rising edge
        LOOP    = 2  # Loop while gate high, stop when low, reset on rising edge
        BOUNCE  = 3  # Loop, swap direction at boundaries
        SCRUB   = 4  # CV input directly controls playback position

    def __init__(self, delayln, sq=ASQ):
        self.delayln = delayln
        self.tap = self.delayln.add_tap()
        self.sq = sq
        assert not self.delayln.write_triggers_read
        super().__init__({
            # Control
            "gate": In(unsigned(1)),
            "mode": In(GrainPlayer.Mode),
            "reverse": In(unsigned(1)),
            "speed": In(fixed.UQ(8, 8)),
            "start": In(unsigned(self.delayln.address_width)),
            "length": In(unsigned(self.delayln.address_width)),
            "scrub": In(stream.Signature(sq)), # Scrub CV (only used in SCRUB mode)
            # Status
            "position": Out(unsigned(self.delayln.address_width)),
            # Outgoing samples
            "o": Out(stream.Signature(sq)),
        })

    def elaborate(self, platform):

        m = Module()

        start    = Signal.like(self.start)
        length   = Signal.like(self.length)
        mode     = Signal.like(self.mode)
        reverse  = Signal.like(self.reverse)
        bouncing = Signal()  # Toggles fwd/rev in BOUNCE mode
        pos      = Signal(fixed.UQ(len(self.length), 8))

        # Linear interpolation
        sample0   = Signal(self.sq)  # sample at integer position
        tap_addr0 = Signal.like(self.start)  # tap address for first fetch
        tap_addr1 = Signal.like(self.start)  # tap address for adjacent sample

        # Gate state tracking
        l_gate = Signal()
        gate_rising_latch = Signal()
        m.d.sync += l_gate.eq(self.gate)
        m.d.sync += gate_rising_latch.eq(gate_rising_latch | (~l_gate & self.gate))

        # Keep scrub stream drained, as we aren't consuming it outside of SCRUB mode.
        m.d.comb += self.scrub.ready.eq(mode != GrainPlayer.Mode.SCRUB)

        with m.FSM():
            with m.State('WAIT-READY-ZERO'):
                with m.If(self.mode == GrainPlayer.Mode.SCRUB):
                    # don't wait on gate in scrub mode
                    m.next = 'START-GATE'
                with m.Else():
                    # no active gate: output stream of zero samples
                    m.d.comb += self.o.valid.eq(1)
                    m.d.sync += self.o.payload.eq(0)
                    with m.If(gate_rising_latch):
                        m.d.sync += gate_rising_latch.eq(0)
                        m.next = 'START-GATE'
            with m.State('START-GATE'):
                m.d.sync += [
                    reverse.eq(self.reverse),
                    mode.eq(self.mode),
                    start.eq(self.start),
                    length.eq(self.length),
                    pos.eq(0),
                    bouncing.eq(0),
                ]
                m.next = 'COMPUTE-ADDR'
            with m.State('COMPUTE-ADDR'):
                with m.If(mode == GrainPlayer.Mode.SCRUB):
                    m.next = 'COMPUTE-ADDR-SCRUB'
                with m.Elif(reverse ^ bouncing): # toggles fwd/rev in BOUNCE mode
                    m.next = 'COMPUTE-ADDR-REVERSE'
                with m.Else():
                    m.next = 'COMPUTE-ADDR-FORWARD'
            with m.State('COMPUTE-ADDR-SCRUB'):
                # Map scrub CV from -1..1 to 0..grain_length.
                # Also consume a single scrub sample.
                m.d.comb += self.scrub.ready.eq(1)
                scrub_norm = (self.scrub.payload + fixed.Const(1.0)) >> 1
                m.d.sync += pos.eq(length * scrub_norm)
                with m.If(self.scrub.valid):
                    m.next = 'COMPUTE-ADDR-FORWARD'
            with m.State('COMPUTE-ADDR-REVERSE'):
                delay = start - length + pos.truncate().as_value() + 1
                m.d.sync += [
                    tap_addr0.eq(delay),
                    tap_addr1.eq(delay + 1),
                    self.position.eq(delay),
                ]
                m.next = 'TAP0-ADDR'
            with m.State('COMPUTE-ADDR-FORWARD'):
                delay = start - pos.truncate().as_value()
                m.d.sync += [
                    tap_addr0.eq(delay),
                    tap_addr1.eq(delay - 1),
                    self.position.eq(delay),
                ]
                m.next = 'TAP0-ADDR'
            with m.State('TAP0-ADDR'):
                m.d.comb += [
                    self.tap.i.valid.eq(1),
                    self.tap.i.payload.eq(tap_addr0),
                ]
                with m.If(self.tap.i.ready):
                    m.next = 'TAP0-READ'
            with m.State('TAP0-READ'):
                m.d.comb += self.tap.o.ready.eq(1)
                with m.If(self.tap.o.valid):
                    m.d.sync += sample0.eq(self.tap.o.payload)
                    m.next = 'TAP1-ADDR'
            with m.State('TAP1-ADDR'):
                m.d.comb += [
                    self.tap.i.valid.eq(1),
                    self.tap.i.payload.eq(tap_addr1),
                ]
                with m.If(self.tap.i.ready):
                    m.next = 'TAP1-READ-INTERP'
            with m.State('TAP1-READ-INTERP'):
                m.d.comb += self.tap.o.ready.eq(1)
                # Linear interpolation: output = sample0 + frac * (sample1 - sample0)
                frac = pos - pos.truncate()
                diff = self.tap.o.payload - sample0
                m.d.sync += self.o.payload.eq(sample0 + diff * frac)
                with m.If(self.tap.o.valid):
                    m.next = 'OUT'
            with m.State('OUT'):
                m.d.comb += self.o.valid.eq(1)
                with m.If(self.o.ready):
                    # Always restart in SCRUB
                    with m.If(mode == GrainPlayer.Mode.SCRUB):
                        m.next = 'START-GATE'
                    # Retrigger on rising edge
                    with m.Elif(gate_rising_latch):
                        m.d.sync += gate_rising_latch.eq(0)
                        m.next = 'START-GATE'
                    # ONESHOT ignores gate-low; all others stop.
                    with m.Elif(~self.gate & (mode != GrainPlayer.Mode.ONESHOT)):
                        m.next = 'WAIT-READY-ZERO'
                    # End of grain
                    with m.Elif(pos.truncate() >= (length-1)):
                        with m.If((mode == GrainPlayer.Mode.GATE) | (mode == GrainPlayer.Mode.ONESHOT)):
                            m.next = 'WAIT-READY-ZERO'
                        with m.Elif(mode == GrainPlayer.Mode.LOOP):
                            m.next = 'START-GATE'
                        with m.Elif(mode == GrainPlayer.Mode.BOUNCE):
                            m.d.sync += bouncing.eq(~bouncing)
                            with m.If(bouncing):
                                # Back to start: re-latch parameters
                                m.next = 'START-GATE'
                            with m.Else():
                                m.d.sync += pos.eq(0)
                                m.next = 'COMPUTE-ADDR'
                    with m.Else():
                        m.d.sync += pos.eq(pos+self.speed)
                        m.next = 'COMPUTE-ADDR'

        return m
