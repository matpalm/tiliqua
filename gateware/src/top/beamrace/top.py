# Copyright (c) 2024 Seb Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
"""
Simple video generation cores 'racing the beam', where the color of every pixel
is calculated right before it is sent to the screen.

Every 'pattern core' takes the signals in ``BeamRaceInputs`` (current pixel, current
audio samples), and emits the signals in ``BeamRaceOutputs`` (output pixel color).

Each 'pattern core' is wrapped by ``BeamRaceTop`` depending on which one is selected
via the CLI, for example ``pdm beamracer build --core=stripes`` will build a
``BeamRaceTop`` that contains the ``Stripes`` pattern core. The mapping is in ``CORES``
below.

Inside each 'pattern core', signals can be considered already synchronized into the 'dvi'
domain - a ``DomainRenamer`` maps this to the ``sync`` domain in each pattern core. So,
inside the pattern cores, you can assume everything is in the ``sync`` domain, which is
at the pixel clock.

A simulation testbench ``sim.cpp`` is provided, so you can simulate new cores by using
``pdm beamrace sim --core=<my_core>``, which will emit bitmaps for the simulated frames.
In the simulation testbench, sine and cosine waves are sent into the 'fake' audio inputs.
"""

import os
import math
import shutil
import subprocess
import random

from amaranth                 import *
from amaranth.build           import *
from amaranth.lib             import wiring, data, stream
from amaranth.lib.wiring      import In, Out
from amaranth.lib.fifo        import AsyncFIFO, SyncFIFO
from amaranth.lib.cdc         import FFSynchronizer
from amaranth.utils           import log2_int
from amaranth.back            import verilog

from amaranth_future          import fixed
from amaranth_soc             import wishbone

from tiliqua.periph           import eurorack_pmod
from tiliqua                  import dsp
from tiliqua.dsp              import ASQ
from tiliqua.build.cli        import top_level_cli
from tiliqua.build            import sim
from tiliqua.platform         import RebootProvider
from tiliqua.video            import dvi
from tiliqua.build.types      import BitstreamHelp

class BeamRaceInputs(wiring.Signature):
    """
    Inputs into a beamracing core, all in the 'dvi' domain (at the pixel clock).
    """
    def __init__(self):
        super().__init__({
            # Video timing inputs
            "hsync":     Out(1),
            "vsync":     Out(1),
            "de":        Out(1),
            "x":         Out(signed(12)),
            "y":         Out(signed(12)),
            "h_active":  Out(12),
            "v_active":  Out(12),
            # Audio samples (already synchronized to DVI domain)
            "audio_in0": Out(signed(16)),
            "audio_in1": Out(signed(16)),
            "audio_in2": Out(signed(16)),
            "audio_in3": Out(signed(16)),
        })

class BeamRaceOutputs(wiring.Signature):
    """
    Outputs from a beamracing core, all in the 'dvi' domain (at the pixel clock).
    """
    def __init__(self):
        super().__init__({
            "r":     Out(8),
            "g":     Out(8),
            "b":     Out(8),
        })

class AboveZero(wiring.Component):

    i: In(BeamRaceInputs())
    o: Out(BeamRaceOutputs())

    bitstream_help = BitstreamHelp(
        brief="Beamracing 'Stripes' pattern",
        io_left=['', '', '', '', 'in0 (copy)', 'in1 (copy)', 'in2 (copy)', 'in3 (copy)'],
        io_right=['', '', 'video (fixed)', '', '', '']
    )

    def elaborate(self, platform):

        m = Module()

        flash = Signal()
        l_vsync = Signal()
        above_zero = Signal()

        m.d.comb += above_zero.eq(self.i.audio_in0 > 0)

        m.d.sync += [
            l_vsync.eq(self.i.vsync),
            #flash.eq(self.i.vsync & ~l_vsync & above_zero),
            flash.eq(above_zero),
        ]

        with m.If(self.i.de):
            with m.If(flash):
                m.d.comb += [
                    self.o.r.eq(0xFF),
                    self.o.g.eq(0x00),
                    self.o.b.eq(0x00),
                ]
            with m.Else():
                m.d.comb += [
                    self.o.r.eq(0x00),
                    self.o.g.eq(0xFF),
                    self.o.b.eq(0x00),
                ]
        with m.Else():
            # do we really need this?
            m.d.comb += [
                self.o.r.eq(0x00),
                self.o.g.eq(0x00),
                self.o.b.eq(0x00),
            ]

        return m

class Stripes(wiring.Component):

    """
    Beamracing pattern core.
    Translated from 'Stripes' from https://vga-playground.com

    Original attribution:
     Copyright (c) 2024 Uri Shaked
     SPDX-License-Identifier: Apache-2.0
    """

    i: In(BeamRaceInputs())
    o: Out(BeamRaceOutputs())

    bitstream_help = BitstreamHelp(
        brief="Beamracing 'Stripes' pattern",
        io_left=['', '', '', '', 'in0 (copy)', 'in1 (copy)', 'in2 (copy)', 'in3 (copy)'],
        io_right=['', '', 'video (fixed)', '', '', '']
    )

    def elaborate(self, platform):

        m = Module()

        counter  = Signal(10)
        moving_x = Signal(10)

        l_vsync = Signal()
        m.d.sync += l_vsync.eq(self.i.vsync)
        with m.If(self.i.vsync & ~l_vsync):
            m.d.sync += counter.eq(counter + 1)

        m.d.comb += moving_x.eq(self.i.x + counter + self.i.audio_in0)

        with m.If(self.i.de):
            m.d.comb += [
                self.o.r.eq(Cat(C(0, 6), self.i.y[2], moving_x[5])),
                self.o.g.eq(Cat(C(0, 6), self.i.y[2], moving_x[6])),
                self.o.b.eq(Cat(C(0, 6), self.i.y[5], moving_x[7])),
            ]

        return m


class LifeGrid(Elaboratable):

    def __init__(self, width, height, audio_in0):
        self.width = width
        self.height = height

        # Seed with a single glider at (5, 5).
        # Relative glider cells: (1,0), (2,1), (0,2), (1, 2), (2, 2)
        gx, gy = 5, 5
        init_cells = 0
        for dx, dy in [(1, 0), (2, 1), (0, 2), (1, 2), (2, 2)]:
            x = (gx + dx) % width
            y = (gy + dy) % height
            init_cells |= 1 << (y * width + x)

        self.cells = Signal(width * height, reset=init_cells)
        self.next_cells = Signal(width * height)
        self.audio_in0 = audio_in0

    def elaborate(self, platform):
        m = Module()

        # TODO: building signals etc for _every_ cell is overkill; how to
        #       use RAM?

        # for each cell...
        for y in range(self.height):
            for x in range(self.width):

                # calculate neighbour idxs
                neighbours = []
                for dy in [-1, 0, 1]:
                    for dx in [-1, 0, 1]:
                        if dx == 0 and dy == 0:
                            continue
                        nx = (x + dx) % self.width
                        ny = (y + dy) % self.height
                        neighbours.append(self.cells[ny * self.width + nx])

                # count the number of alive neighbours
                num_alive_neighbours = Signal(4)
                m.d.comb += num_alive_neighbours.eq(sum(neighbours))

                # apply alive / dead rules
                idx = y * self.width + x
                with m.If(num_alive_neighbours == 3):
                    # new
                    m.d.comb += self.next_cells[idx].eq(1)
                with m.Elif(num_alive_neighbours == 2):
                    # keep alive ( if alive )
                    m.d.comb += self.next_cells[idx].eq(self.cells[idx])
                with m.Else():
                    # die
                    m.d.comb += self.next_cells[idx].eq(0)

        counter = Signal(24)
        next_counter = Signal.like(counter)
        crossed_zero = Signal()

        step = Mux(self.audio_in0 <= 0, 0, self.audio_in0.as_unsigned() >> 6)

        m.d.comb += [
            next_counter.eq(counter + step),
            crossed_zero.eq(next_counter < counter),
        ]

        m.d.sync += counter.eq(next_counter)

        with m.If(crossed_zero):
            m.d.sync += self.cells.eq(self.next_cells)

        return m

class GameOfLife(wiring.Component):

    i: In(BeamRaceInputs())
    o: Out(BeamRaceOutputs())

    bitstream_help = BitstreamHelp(
        brief="Beamracing 'GameOfLife' pattern",
        io_left=['', '', '', '', 'in0 (copy)', 'in1 (copy)', 'in2 (copy)', 'in3 (copy)'],
        io_right=['', '', 'video (fixed)', '', '', '']
    )

    def elaborate(self, platform):

        m = Module()

        W, H = 25, 25  # game of life grid size
        P = 20

        m.submodules.grid = grid = LifeGrid(
            width=W,
            height=H,
            audio_in0=self.i.audio_in0,
        )

        cell_x   = Signal(range(W))
        cell_y   = Signal(range(H))
        cell_idx = Signal(range(W*H))
        in_grid  = Signal()
        alive    = Signal()

        m.d.comb += [
            in_grid.eq((self.i.x < W*P) & (self.i.y < H*P)),
            cell_x.eq(self.i.x // P),
            cell_y.eq(self.i.y // P),
            cell_idx.eq(cell_y * W + cell_x),
            alive.eq(grid.cells.bit_select(cell_idx, 1)),
        ]

        with m.If(self.i.de & in_grid):
            with m.If(alive):
                m.d.comb += [
                    self.o.r.eq(0xFF),
                    self.o.g.eq(0x00),
                    self.o.b.eq(0x00),
                ]
            with m.Else():
                m.d.comb += [
                    self.o.r.eq(0x00),
                    self.o.g.eq(0xFF),
                    self.o.b.eq(0x00),
                ]
        with m.Else():
            m.d.comb += [
                self.o.r.eq(0x00),
                self.o.g.eq(0x00),
                self.o.b.eq(0xFF),
            ]

        return m

class Balls(wiring.Component):

    """
    Beamracing pattern core.
    Translated from 'Balls' from vga-playground.com

    Edits: some added registers to make timing more FPGA friendly.

    Original attribution:
     Copyright (c) 2024 Renaldas Zioma
     based on the VGA examples by Uri Shaked
     SPDX-License-Identifier: Apache-2.0
    """

    i: In(BeamRaceInputs())
    o: Out(BeamRaceOutputs())

    bitstream_help = BitstreamHelp(
        brief="Beamracing 'Balls' pattern",
        io_left=['', '', '', '', 'in0 (copy)', 'in1 (copy)', 'in2 (copy)', 'in3 (copy)'],
        io_right=['', '', 'video (fixed)', '', '', '']
    )

    def elaborate(self, platform):

        m = Module()

        # Time counter for animation
        counter = Signal(20)

        # Update animation counter on vsync
        l_vsync = Signal()
        m.d.sync += l_vsync.eq(self.i.vsync)
        with m.If(self.i.vsync & ~l_vsync):
            m.d.sync += counter.eq(counter + 1)

        # Points for Worley noise
        points_x = [Signal(signed(10)) for _ in range(4)]
        points_y = [Signal(signed(10)) for _ in range(4)]

        # Calculate point positions with animation
        m.d.comb += [
            points_x[0].eq(100 + counter),
            points_y[0].eq(100 - counter),
            points_x[1].eq(300 - (counter >> 1)),
            points_y[1].eq(200 + (counter >> 1)),
            points_x[2].eq(500 + (counter >> 1)),
            points_y[2].eq(400 - (counter >> 4)),
            points_x[3].eq(100 - (counter >> 3)),
            points_y[3].eq(500 - (counter >> 2))
        ]

        distance1 = Signal(16)
        distance2 = Signal(16)
        distance3 = Signal(16)
        distance4 = Signal(16)
        min_dist = Signal(16)

        # Calculate squared distances to each point
        m.d.sync += [
            distance1.eq((self.i.x - points_x[0]) * (self.i.x - points_x[0]) +
                        (self.i.y - points_y[0]) * (self.i.y - points_y[0])),
            distance2.eq((self.i.x - points_x[1]) * (self.i.x - points_x[1]) +
                        (self.i.y - points_y[1]) * (self.i.y - points_y[1])),
            distance3.eq((self.i.x - points_x[2]) * (self.i.x - points_x[2]) +
                        (self.i.y - points_y[2]) * (self.i.y - points_y[2])),
            distance4.eq((self.i.x - points_x[3]) * (self.i.x - points_x[3]) +
                        (self.i.y - points_y[3]) * (self.i.y - points_y[3]))
        ]

        # Find minimum distance (simplified approach)
        min1 = Signal(16)
        min2 = Signal(16)

        m.d.comb += [
            min1.eq(Mux(distance1 < distance2, distance1, distance2)),
            min2.eq(Mux(distance3 < distance4, distance3, distance4)),
            min_dist.eq(Mux(min1 < min2, min1, min2))
        ]

        # Generate noise value from minimum distance
        noise_value = Signal(8)
        m.d.comb += noise_value.eq(~min_dist[8:15])  # Scale down to 8-bit and invert

        # Set RGB output based on noise value when display is enabled
        with m.If(self.i.de):
            m.d.comb += [
                self.o.r.eq(Cat(C(0, 6), noise_value[7], noise_value[2])),
                self.o.g.eq(Cat(C(0, 6), noise_value[6], noise_value[3])),
                self.o.b.eq(Cat(C(0, 6), noise_value[5], noise_value[4]))
            ]

        return m

class Checkers(wiring.Component):

    """
    Beamracing pattern core.
    Translated from 'Checkers' from vga-playground.com

    Edits: 1 layer removed, some added registers for friendlier timing.

    Original attribution:
     Copyright (c) 2024 Renaldas Zioma
     based on the VGA examples by Uri Shaked
     SPDX-License-Identifier: Apache-2.0
    """

    i: In(BeamRaceInputs())
    o: Out(BeamRaceOutputs())

    bitstream_help = BitstreamHelp(
        brief="Beamracing 'Checkers' pattern",
        io_left=['position', 'color1', 'color2', 'color3', 'in0 (copy)', 'in1 (copy)', 'in2 (copy)', 'in3 (copy)'],
        io_right=['', '', 'video (fixed)', '', '', '']
    )

    def elaborate(self, platform):

        m = Module()

        # Animation counter that increments on vsync
        counter = Signal(10)
        l_vsync = Signal()

        # Detect rising edge of vsync
        m.d.sync += l_vsync.eq(self.i.vsync)
        with m.If(self.i.vsync & ~l_vsync):
            m.d.sync += counter.eq(counter + (self.i.audio_in0 >> 10))

        # Animated layer positions
        layer_a_x = Signal(10)
        layer_a_y = Signal(10)
        layer_b_x = Signal(10)
        layer_b_y = Signal(10)
        layer_c_x = Signal(10)
        layer_c_y = Signal(10)
        layer_d_x = Signal(10)
        layer_d_y = Signal(10)
        layer_e_x = Signal(10)
        layer_e_y = Signal(10)

        # Calculate animated positions for each layer
        m.d.sync += [
            layer_a_x.eq(self.i.x + counter * 16),
            layer_a_y.eq(self.i.y + counter * 2),
            layer_b_x.eq(self.i.x + counter * 7),
            layer_b_y.eq(self.i.y + counter + (counter >> 1)),
            layer_c_x.eq(self.i.x + counter * 4),
            layer_c_y.eq(self.i.y + (counter >> 1)),
            layer_d_x.eq(self.i.x + counter * 2),
            layer_d_y.eq(self.i.y + (counter >> 2)),
        ]

        # Layer patterns with transparency using dithering
        layer_a = Signal()
        layer_b = Signal()
        layer_c = Signal()
        layer_d = Signal()

        m.d.sync += [
            layer_a.eq((layer_a_x[8] ^ layer_a_y[8]) & (self.i.y[1] ^ self.i.x[0])),
            layer_b.eq((layer_b_x[7] ^ layer_b_y[7]) & (~self.i.y[0] ^ self.i.x[1])),
            layer_c.eq(layer_c_x[6] ^ layer_c_y[6]),
            layer_d.eq(layer_d_x[5] ^ layer_d_y[5]),
        ]

        # Define layer colors
        # For simplicity, use a constant color for color_a
        # This could be made configurable similar to ui_in in the original
        color_a = Signal(6)
        color_b = Signal(6)
        color_c = Signal(6)
        color_de = Signal(6)

        m.d.sync += [
            color_a.eq(0x3F + (self.i.audio_in1>>8)),  # Example color 0x3F = 0b111111
            color_b.eq(color_a ^ 0b001010 ^ (self.i.audio_in2>>8)),
            color_c.eq(color_b & 0b101010 + (self.i.audio_in3>>8)),
            color_de.eq(color_c >> 1)
        ]

        # Output color selection based on layers
        with m.If(layer_a):
            m.d.sync += [
                self.o.r.eq(Cat(C(0, 6), color_a[1], color_a[0])),
                self.o.g.eq(Cat(C(0, 6), color_a[3], color_a[2])),
                self.o.b.eq(Cat(C(0, 6), color_a[5], color_a[4]))
            ]
        with m.Elif(layer_b):
            m.d.sync += [
                self.o.r.eq(Cat(C(0, 6), color_b[1], color_b[0])),
                self.o.g.eq((self.i.audio_in1>>8)),
                self.o.b.eq(Cat(C(0, 6), color_b[5], color_b[4]))
            ]
        with m.Elif(layer_c):
            m.d.sync += [
                self.o.r.eq(Cat(C(0, 6), color_c[1], color_c[0])),
                self.o.g.eq(Cat(C(0, 6), color_c[3], color_c[2])),
                self.o.b.eq(Cat(C(0, 6), color_c[5], color_c[4]))
            ]
        with m.Elif(layer_d):
            m.d.sync += [
                self.o.r.eq(Cat(C(0, 6), color_de[1], color_de[0])),
                self.o.g.eq(Cat(C(0, 6), color_de[3], color_de[2])),
                self.o.b.eq(Cat(C(0, 6), color_de[5], color_de[4]))
            ]
        with m.Else():
            m.d.sync += [
                self.o.r.eq(0),
                self.o.g.eq(0),
                self.o.b.eq(0)
            ]

        return m

class BeamRaceTop(Elaboratable):

    """
    Wrapper structure around beamracing cores.

    Provides the clock, DVI timing generation and PHY, and interface to the audio IOs
    (synchronized to the video domain), as well as 'hold to enter bootloader' logic.
    """

    def __init__(self, clock_settings, beamrace_core: wiring.Component):

        # This core only works with static modelines
        assert clock_settings.modeline is not None

        self.clock_settings = clock_settings
        self.pmod0 = eurorack_pmod.EurorackPmod(self.clock_settings.audio_clock)
        self.dvi_tgen = dvi.DVITimingGen()

        # Instantiate the provided beamracing core, for us to wrap it
        self.core = DomainRenamer("dvi")(beamrace_core())

        # Forward bitstream_help from the core if it exists
        if hasattr(self.core, "bitstream_help"):
            self.bitstream_help = self.core.bitstream_help

        super().__init__()

    def elaborate(self, platform):

        m = Module()

        if sim.is_hw(platform):
            m.submodules.car = car = platform.clock_domain_generator(self.clock_settings)
            m.submodules.reboot = reboot = RebootProvider(self.clock_settings.frequencies.sync)
            m.submodules.btn = FFSynchronizer(
                    platform.request("encoder").s.i, reboot.button)
            m.submodules.pmod0_provider = pmod0_provider = eurorack_pmod.FFCProvider()
            wiring.connect(m, self.pmod0.pins, pmod0_provider.pins)
            m.d.comb += self.pmod0.codec_mute.eq(reboot.mute)
        else:
            m.submodules.car = sim.FakeTiliquaDomainGenerator()

        m.submodules.pmod0 = pmod0 = self.pmod0

        # Mirror audio inputs to audio outputs
        wiring.connect(m, pmod0.o_cal, pmod0.i_cal)

        m.submodules.dvi_tgen = dvi_tgen = self.dvi_tgen

        # Configure the DVI timing generator to match the selected resolution
        for member in dvi_tgen.timings.signature.members:
            m.d.comb += getattr(dvi_tgen.timings, member).eq(getattr(self.clock_settings.modeline, member))

        # Beamracer core itself
        m.submodules.core = core = self.core

        # Synchronize audio inputs into DVI domain and provide them to the beamracer core.
        for ch in range(4):
            m.submodules += FFSynchronizer(
                    i=pmod0.o_cal.payload[ch].as_value(), o=getattr(core.i, f"audio_in{ch}"), o_domain="dvi")

        # Hook up the remaining beamracer inputs (already in DVI domain)
        m.d.comb += [
            core.i.vsync.eq(dvi_tgen.ctrl.vsync),
            core.i.hsync.eq(dvi_tgen.ctrl.hsync),
            core.i.de.eq(dvi_tgen.ctrl.de),
            core.i.x.eq(dvi_tgen.x),
            core.i.y.eq(dvi_tgen.y),
            core.i.h_active.eq(dvi_tgen.timings.h_active),
            core.i.v_active.eq(dvi_tgen.timings.v_active),
        ]

        # Hook up DVI PHY to the beamracer outputs
        if sim.is_hw(platform):
            m.submodules.dvi_gen = dvi_gen = dvi.DVIPHY()
            m.d.dvi += [
                dvi_gen.i.de.eq(dvi_tgen.ctrl_phy.de),
                dvi_gen.i.b.eq(core.o.b),
                dvi_gen.i.g.eq(core.o.g),
                dvi_gen.i.r.eq(core.o.r),
                dvi_gen.i.hsync.eq(dvi_tgen.ctrl_phy.hsync),
                dvi_gen.i.vsync.eq(dvi_tgen.ctrl_phy.vsync),
            ]

        return m

# Different beamrace cores that can be selected using e.g. `pdm beamracer build --core=stripes`.
CORES = {
    "above_zero": AboveZero,
    "stripes":   Stripes,
    "balls":     Balls,
    "checkers":  Checkers,
    "game_of_life": GameOfLife,
}

def simulation_ports(fragment):
    # Ports required by `sim.cpp` for end-to-end simulation of these cores.
    return {
        "clk_sync":       (ClockSignal("sync"),              None),
        "rst_sync":       (ResetSignal("sync"),              None),
        "clk_dvi":        (ClockSignal("dvi"),               None),
        "rst_dvi":        (ResetSignal("dvi"),               None),
        "clk_audio":      (ClockSignal("audio"),             None),
        "rst_audio":      (ResetSignal("audio"),             None),
        "i2s_sdin1":      (fragment.pmod0.pins.i2s.sdin1,    None),
        "i2s_sdout1":     (fragment.pmod0.pins.i2s.sdout1,   None),
        "i2s_lrck":       (fragment.pmod0.pins.i2s.lrck,     None),
        "i2s_bick":       (fragment.pmod0.pins.i2s.bick,     None),
        "dvi_de":         (fragment.dvi_tgen.ctrl_phy.de,    None),
        "dvi_vsync":      (fragment.dvi_tgen.ctrl_phy.vsync, None),
        "dvi_hsync":      (fragment.dvi_tgen.ctrl_phy.hsync, None),
        "dvi_r":          (fragment.core.o.r,                None),
        "dvi_g":          (fragment.core.o.g,                None),
        "dvi_b":          (fragment.core.o.b,                None),
    }

def argparse_callback(parser):
    parser.add_argument('--core', type=str, default="checkers",
                        help=f"One of {list(CORES)}")

def argparse_fragment(args):
    # Additional arguments to be provided to BeamRaceTop
    if args.core not in CORES:
        print(f"provided '--core {args.core}' is not one of {list(CORES)}")
        import sys
        sys.exit(-1)

    cls_name = CORES[args.core]
    if args.name == 'BEAMRACE':
        args.name = 'BR-' + args.core.upper().replace('_','-')
    return {
        "beamrace_core": cls_name,
    }

if __name__ == "__main__":
    this_path = os.path.dirname(os.path.realpath(__file__))
    top_level_cli(
        BeamRaceTop,
        sim_ports=simulation_ports,
        sim_harness="../../src/top/beamrace/sim.cpp",
        argparse_callback=argparse_callback,
        argparse_fragment=argparse_fragment,
    )
