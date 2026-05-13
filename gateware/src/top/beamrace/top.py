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

from PIL import Image
import numpy as np

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
        super().__init__(
            {
                # Video timing inputs
                "hsync": Out(1),
                "vsync": Out(1),
                "de": Out(1),
                "x": Out(signed(12)),
                "y": Out(signed(12)),
                # Pulses (via toggle edge detect) once per audio sample handshake.
                "audio_tick": Out(1),
                # Audio samples (already synchronized to DVI domain)
                "audio_in0": Out(signed(16)),
                "audio_in1": Out(signed(16)),
                "audio_in2": Out(signed(16)),
                "audio_in3": Out(signed(16)),
            }
        )

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


class EuroVidRack(wiring.Component):

    """
    Beamracing pattern core.
    Displays a static 128x75 RGB image from euro_128.png, scaled 8x to 1024x600.
    """

    i: In(BeamRaceInputs())
    o: Out(BeamRaceOutputs())

    bitstream_help = BitstreamHelp(
        brief="Beamracing static EuroVidRack image",
        io_left=[
            "",
            "",
            "",
            "",
            "in0 (copy)",
            "in1 (copy)",
            "in2 (copy)",
            "in3 (copy)",
        ],
        io_right=["", "", "video (fixed)", "", "", ""],
    )

    IMAGE_W = 128
    IMAGE_H = 75
    SCALE = 8
    OUT_W = IMAGE_W * SCALE
    OUT_H = IMAGE_H * SCALE
    SERIAL_SYNC_MARKER = 0

    def __init__(self):
        super().__init__()
        self.original_r = Signal(8)

        image_path = os.path.join(os.path.dirname(__file__), "euro_128.png")
        img = Image.open(image_path).convert("RGB")
        if img.size != (self.IMAGE_W, self.IMAGE_H):
            raise ValueError(
                f"expected {image_path} to be {self.IMAGE_W}x{self.IMAGE_H}, got {img.size}"
            )

        pixels = np.asarray(img, dtype=np.uint8).reshape(-1, 3)
        packed_pixels = [
            (int(px[0]) << 16) | (int(px[1]) << 8) | int(px[2]) for px in pixels
        ]
        self.original_img = Memory(
            width=24,
            depth=self.IMAGE_W * self.IMAGE_H,
            init=packed_pixels,
        )
        self.feedback_img = Memory(
            width=24,
            depth=self.IMAGE_W * self.IMAGE_H,
            init=[0 for _ in range(self.IMAGE_W * self.IMAGE_H)],
        )

    def elaborate(self, platform):

        m = Module()

        # Keep synchronized copies of audio channels available in this core.
        audio_sync = [Signal(signed(16), name=f"audio_sync_{ch}") for ch in range(4)]
        for ch in range(4):
            m.d.sync += audio_sync[ch].eq(getattr(self.i, f"audio_in{ch}"))

        src_x = Signal(range(self.IMAGE_W))
        src_y = Signal(range(self.IMAGE_H))
        pix_idx = Signal(range(self.IMAGE_W * self.IMAGE_H))
        fb_idx = Signal(range(self.IMAGE_W * self.IMAGE_H))
        in_range = Signal()
        in_range_d = Signal()
        show_original = Signal()
        show_original_d = Signal()
        in2_unsigned = Signal(unsigned(ASQ.width))
        feedback_px = Signal(8)
        fb_r = Signal(8)
        fb_g = Signal(8)
        rx_phase = Signal(range(3), init=0)
        audio_tick_d = Signal()
        audio_tick_rise = Signal()
        rx_sync = Signal()
        rx_locked = Signal(init=0)

        m.submodules.original_rp = original_rp = self.original_img.read_port(
            domain="sync"
        )
        m.submodules.feedback_rp = feedback_rp = self.feedback_img.read_port(
            domain="sync"
        )
        m.submodules.feedback_wp = feedback_wp = self.feedback_img.write_port(
            domain="sync"
        )

        m.d.comb += [
            src_x.eq(self.i.x[3:10]),
            src_y.eq(self.i.y[3:10]),
            pix_idx.eq((src_y << 7) + src_x),
            in_range.eq(
                self.i.de
                & (self.i.x >= 0)
                & (self.i.y >= 0)
                & (self.i.x < self.OUT_W)
                & (self.i.y < self.OUT_H)
            ),
            show_original.eq(audio_sync[1] > 0),
            # Inverse of BeamRaceTop's image-byte serialization on channel 3:
            # out = (pixel * 257) - 32768  =>  pixel = ((in + 32768) >> 8).
            in2_unsigned.eq(audio_sync[2] + (1 << (ASQ.width - 1))),
            feedback_px.eq(in2_unsigned[8:16]),
            # Sync channel is input channel 4 (index 3): positive pulse marks frame start.
            rx_sync.eq(audio_sync[3] > 0),
            audio_tick_rise.eq(self.i.audio_tick ^ audio_tick_d),
            original_rp.addr.eq(pix_idx),
            feedback_rp.addr.eq(pix_idx),
            feedback_wp.addr.eq(fb_idx),
            feedback_wp.data.eq(Cat(feedback_px, fb_g, fb_r)),
            feedback_wp.en.eq(audio_tick_rise & rx_locked & ~rx_sync & (rx_phase == 2)),
        ]

        m.d.sync += [
            in_range_d.eq(in_range),
            show_original_d.eq(show_original),
            audio_tick_d.eq(self.i.audio_tick),
        ]

        with m.If(audio_tick_rise):
            with m.If(rx_sync):
                m.d.sync += [
                    rx_locked.eq(1),
                    fb_idx.eq(0),
                    rx_phase.eq(0),
                ]
            with m.Elif(rx_locked):
                with m.If(rx_phase == 0):
                    m.d.sync += [
                        fb_r.eq(feedback_px),
                        rx_phase.eq(1),
                    ]
                with m.Elif(rx_phase == 1):
                    m.d.sync += [
                        fb_g.eq(feedback_px),
                        rx_phase.eq(2),
                    ]
                with m.Else():
                    m.d.sync += rx_phase.eq(0)
                    with m.If(fb_idx == (self.IMAGE_W * self.IMAGE_H - 1)):
                        m.d.sync += fb_idx.eq(0)
                    with m.Else():
                        m.d.sync += fb_idx.eq(fb_idx + 1)

        with m.If(in_range_d):
            m.d.sync += self.original_r.eq(original_rp.data[16:24])
            m.d.sync += [
                self.o.r.eq(
                    Mux(
                        show_original_d,
                        original_rp.data[16:24],
                        feedback_rp.data[16:24],
                    )
                ),
                self.o.g.eq(
                    Mux(show_original_d, original_rp.data[8:16], feedback_rp.data[8:16])
                ),
                self.o.b.eq(
                    Mux(show_original_d, original_rp.data[0:8], feedback_rp.data[0:8])
                ),
            ]
        with m.Else():
            m.d.sync += [
                self.original_r.eq(0),
                self.o.r.eq(0),
                self.o.g.eq(0),
                self.o.b.eq(0),
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
        self._beamrace_core_cls = beamrace_core

        self._serial_depth = None
        self.serial_original_rgb = None
        if beamrace_core is EuroVidRack:
            image_path = os.path.join(os.path.dirname(__file__), "euro_128.png")
            img = Image.open(image_path).convert("RGB")
            arr = np.asarray(img, dtype=np.uint8)
            self._serial_depth = arr.shape[0] * arr.shape[1]
            packed_pixels = [
                (int(px[0]) << 16) | (int(px[1]) << 8) | int(px[2])
                for px in arr.reshape(-1, 3)
            ]
            self.serial_original_rgb = Memory(
                width=24,
                depth=self._serial_depth,
                init=packed_pixels,
            )

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

        m.submodules.dvi_tgen = dvi_tgen = self.dvi_tgen

        # Configure the DVI timing generator to match the selected resolution
        for member in dvi_tgen.timings.signature.members:
            m.d.comb += getattr(dvi_tgen.timings, member).eq(getattr(self.clock_settings.modeline, member))

        # Beamracer core itself
        m.submodules.core = core = self.core

        # Audio routing: channel 0 serializes original image red channel in
        # audio-sample cadence (RGB time-multiplexed); channels 1-3 are passthrough.
        sample_stb = Signal()
        audio_tick_toggle = Signal()
        tx_byte = Signal(8)
        tx_audio = Signal(signed(ASQ.width))
        tx_sample = Signal(signed(ASQ.width))
        tx_sync = Signal(signed(ASQ.width))
        tx_send_sync = Signal(init=1)
        tx_phase = Signal(range(3), init=0)
        tx_idx = Signal(
            range(self._serial_depth if self._serial_depth is not None else 2), init=0
        )

        if self.serial_original_rgb is not None:
            m.submodules.serial_rgb_rp = serial_rgb_rp = (
                self.serial_original_rgb.read_port(domain="sync")
            )
            m.d.comb += [
                serial_rgb_rp.addr.eq(tx_idx),
                tx_byte.eq(
                    Mux(
                        tx_phase == 0,
                        serial_rgb_rp.data[16:24],
                        Mux(
                            tx_phase == 1,
                            serial_rgb_rp.data[8:16],
                            serial_rgb_rp.data[0:8],
                        ),
                    )
                ),
            ]
        elif hasattr(core, "original_r"):
            m.d.comb += tx_byte.eq(core.original_r)
        else:
            m.d.comb += tx_byte.eq(core.o.r)

        m.d.comb += sample_stb.eq(pmod0.o_cal.valid & pmod0.i_cal.ready)

        with m.If(sample_stb):
            m.d.sync += audio_tick_toggle.eq(~audio_tick_toggle)
            with m.If(tx_send_sync):
                m.d.sync += [
                    tx_send_sync.eq(0),
                    tx_phase.eq(0),
                ]
            with m.Else():
                if self._serial_depth is not None:
                    with m.If(tx_phase == 0):
                        m.d.sync += tx_phase.eq(1)
                    with m.Elif(tx_phase == 1):
                        m.d.sync += tx_phase.eq(2)
                    with m.Else():
                        m.d.sync += tx_phase.eq(0)
                        with m.If(tx_idx == (self._serial_depth - 1)):
                            m.d.sync += [
                                tx_idx.eq(0),
                                tx_send_sync.eq(1),
                            ]
                        with m.Else():
                            m.d.sync += tx_idx.eq(tx_idx + 1)

        m.d.comb += [
            # 0 -> -32768, 255 -> +32767 (for ASQ.width=16)
            tx_audio.eq((tx_byte * 257) - (1 << (ASQ.width - 1))),
            tx_sample.eq(tx_audio),
            # Sync stream on output channel 4 (index 3):
            # +FS for sync sample, -FS otherwise.
            tx_sync.eq(
                Mux(tx_send_sync, (1 << (ASQ.width - 1)) - 1, -(1 << (ASQ.width - 1)))
            ),
            pmod0.i_cal.valid.eq(pmod0.o_cal.valid),
            pmod0.o_cal.ready.eq(pmod0.i_cal.ready),
            # out1/out2 passthrough, out3 image data, out4 sync framing.
            pmod0.i_cal.payload[0].eq(pmod0.o_cal.payload[0]),
            pmod0.i_cal.payload[1].eq(pmod0.o_cal.payload[1]),
            pmod0.i_cal.payload[2].eq(tx_sample),
            pmod0.i_cal.payload[3].eq(tx_sync),
        ]

        # Synchronize audio inputs into DVI domain and provide them to the beamracer core.
        for ch in range(4):
            m.submodules += FFSynchronizer(
                    i=pmod0.o_cal.payload[ch].as_value(), o=getattr(core.i, f"audio_in{ch}"), o_domain="dvi")
        m.submodules += FFSynchronizer(
            i=audio_tick_toggle,
            o=core.i.audio_tick,
            o_domain="dvi",
        )

        # Hook up the remaining beamracer inputs (already in DVI domain)
        m.d.comb += [
            core.i.vsync.eq(dvi_tgen.ctrl.vsync),
            core.i.hsync.eq(dvi_tgen.ctrl.hsync),
            core.i.de.eq(dvi_tgen.ctrl.de),
            core.i.x.eq(dvi_tgen.x),
            core.i.y.eq(dvi_tgen.y),
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
    "stripes": Stripes,
    "balls": Balls,
    "checkers": Checkers,
    "euro_vid_rack": EuroVidRack,
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
