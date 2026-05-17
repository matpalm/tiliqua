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
                # Pulse once per audio sample handshake.
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


class EuroVidRackCore(wiring.Component):

    i: In(BeamRaceInputs())
    o: Out(BeamRaceOutputs())
    spi_cs: In(1)
    spi_sck: In(1)
    spi_mosi: In(1)
    spi_miso: Out(1)

    bitstream_help = BitstreamHelp(
        brief="Beamracing static EuroVidRack image",
        io_left=[
            "sync delay control",
            "original vs feedback",
            "img feedback",
            "sync feedback",
            "",
            "in1 (copy)",
            "img send",
            "sycn send",
        ],
        io_right=["", "", "video (fixed)", "", "", ""],
    )

    IMAGE_W = 128
    IMAGE_H = 75
    SCALE = 8
    OUT_W = IMAGE_W * SCALE
    OUT_H = IMAGE_H * SCALE
    SERIAL_SYNC_BURST = 8
    N_PIXELS = IMAGE_W * IMAGE_H
    # SERIAL_SYNC_MARKER = 0
    # FEEDBACK_COLOUR_SPACE = "YUV"
    # FEEDBACK_COLOUR_SPACE = "RGB"

    def __init__(self):
        super().__init__()

        # load static test image
        image_path = os.path.join(os.path.dirname(__file__), "euro_128.png")
        img = Image.open(image_path).convert("RGB")
        if img.size != (self.IMAGE_W, self.IMAGE_H):
            raise ValueError(
                f"expected {image_path} to be {self.IMAGE_W}x{self.IMAGE_H}, got {img.size}"
            )

        # flatten to (xy, 3) uint8 & pack into 8x3=24 bits
        pixels = np.asarray(img, dtype=np.uint8).reshape(-1, 3)
        packed_pixels = [
            (int(px[0]) << 16) | (int(px[1]) << 8) | int(px[2]) for px in pixels
        ]

        self.spi_incoming_img = Memory(
            width=8 * 3,
            depth=self.IMAGE_W * self.IMAGE_H,
            init=packed_pixels,
        )
        self.original_img = Memory(
            width=8 * 3,
            depth=self.IMAGE_W * self.IMAGE_H,
            init=packed_pixels,
        )
        self.feedback_img = Memory(
            width=8 * 3,
            depth=self.IMAGE_W * self.IMAGE_H,
            init=[0 for _ in range(self.IMAGE_W * self.IMAGE_H)],
        )

        # Exported serialized image stream for top-level audio TX.
        self.serial_tx_byte = Signal(8)
        self.serial_tx_sync_active = Signal()

    def elaborate(self, platform):

        m = Module()
        m.domains.spi = ClockDomain("spi", async_reset=True)
        m.d.comb += [
            ClockSignal("spi").eq(self.spi_sck),
            ResetSignal("spi").eq(self.spi_cs),
        ]
        # use_yuv = self.FEEDBACK_COLOUR_SPACE == "YUV"

        # Keep synchronized copies of audio inputs in the local sync domain.
        audio_sync = [Signal(signed(16), name=f"audio_sync_{ch}") for ch in range(4)]
        for ch in range(4):
            m.d.sync += audio_sync[ch].eq(getattr(self.i, f"audio_in{ch}"))

        # x / y within original img
        src_x = Signal(range(self.IMAGE_W))
        src_y = Signal(range(self.IMAGE_H))

        # Flattened pixel index for image memory reads.
        # pix_idx = src_y * W + src_X
        pix_idx = Signal(range(self.IMAGE_W * self.IMAGE_H))

        # address for the feedback writer
        fb_idx = Signal(range(self.IMAGE_W * self.IMAGE_H))

        # Delay display valid by one cycle to match synchronous RAM read latency.
        in_range = Signal()
        in_range_d = Signal()

        # Blend control derived from in1: 0 -> feedback, 255 -> original.
        blend_alpha = Signal(8)
        blend_alpha_d = Signal(8)
        blend_inv_alpha_d = Signal(8)
        blend_r_mix = Signal(17)
        blend_g_mix = Signal(17)
        blend_b_mix = Signal(17)

        # which of the delayed signals to use;
        #  0 -> no delay,
        #  1 -> delay 1 sample
        #  2 -> delay 2 samples
        rx_delay_sel = Signal(range(3), init=1)

        # copies of in2 delayed by 1 and 2 samples
        rx_data_d1 = Signal(signed(16))
        rx_data_d2 = Signal(signed(16))

        # effective received sample ( after being delayed 0, 1, or
        # 2 samples based on rx_delay_sel
        rx_data = Signal(signed(16))

        # unsigned version of feedback loop
        in2_unsigned = Signal(unsigned(ASQ.width))

        # feedback 8bit pixel converted from in2_unsigned
        feedback_px = Signal(8)

        # which plane, R, G or B, is being processed
        rx_plane = Signal(range(3), init=0)

        # YUV versions
        # fb_y = Signal(unsigned(8))
        # fb_u = Signal(unsigned(8))
        # fb_v = Signal(unsigned(8))
        # fb_d = Signal(signed(10))
        # fb_e = Signal(signed(10))
        # fb_r_tmp = Signal(signed(20))
        # fb_g_tmp = Signal(signed(20))
        # fb_b_tmp = Signal(signed(20))
        # fb_r = Signal(unsigned(8))
        # fb_g = Signal(unsigned(8))
        # fb_b = Signal(unsigned(8))

        # one cycle delayed version of audio_tick
        audio_tick_d = Signal()

        # per sample strobe
        audio_tick_rise = Signal()

        # syncing signals
        rx_sync = Signal()
        rx_locked = Signal(init=0)

        # serializer state for original image -> audio stream
        tx_plane = Signal(range(3), init=0)
        tx_idx = Signal(range(self.IMAGE_W * self.IMAGE_H), init=0)
        tx_sync_count = Signal(
            range(self.SERIAL_SYNC_BURST + 1), init=self.SERIAL_SYNC_BURST
        )

        # read original image for both display and serializer
        m.submodules.original_rp = original_rp = self.original_img.read_port(
            domain="sync"
        )
        m.submodules.original_tx_rp = original_tx_rp = self.original_img.read_port(
            domain="sync"
        )
        m.submodules.spi_incoming_wp = spi_incoming_wp = (
            self.spi_incoming_img.write_port(domain="spi")
        )
        m.submodules.original_wp = original_wp = self.original_img.write_port(
            domain="spi"
        )

        # Feedback image uses independent read ports for display and read-modify-write.
        m.submodules.feedback_display_rp = feedback_display_rp = (
            self.feedback_img.read_port(domain="sync")
        )
        m.submodules.feedback_modify_rp = feedback_modify_rp = (
            self.feedback_img.read_port(domain="sync")
        )
        m.submodules.feedback_wp = feedback_wp = self.feedback_img.write_port(
            domain="sync"
        )

        # SPI image ingest state.
        spi_bit_count = Signal(range(8))
        spi_rx_shift = Signal(8)
        spi_tx_shift = Signal(8)
        spi_tx_active = Signal()
        spi_tx_armed = Signal()

        spi_stage = Signal(range(10))
        pkt_frame_id = Signal(8)
        pkt_off_hi = Signal(8)
        pkt_len_hi = Signal(8)
        pkt_pix_off = Signal(range(self.N_PIXELS))
        pkt_pix_len = Signal(range(self.N_PIXELS + 1))
        pkt_bytes_left = Signal(16)
        pkt_pixel_addr = Signal(range(self.N_PIXELS))
        payload_byte_pos = Signal(range(3))
        payload_r = Signal(8)
        payload_g = Signal(8)
        crc_calc = Signal(16, init=0xFFFF)
        crc_hi = Signal(8)

        spi_wr_en = Signal()
        spi_wr_addr = Signal(range(self.N_PIXELS))
        spi_wr_data = Signal(24)

        m.d.comb += [
            self.spi_miso.eq(Mux(spi_tx_active, spi_tx_shift[7], 0)),
            spi_incoming_wp.en.eq(spi_wr_en),
            spi_incoming_wp.addr.eq(spi_wr_addr),
            spi_incoming_wp.data.eq(spi_wr_data),
            original_wp.en.eq(spi_wr_en),
            original_wp.addr.eq(spi_wr_addr),
            original_wp.data.eq(spi_wr_data),
        ]

        def crc16_next(crc, byte):
            val = Value.cast(crc)
            byte = Value.cast(byte)
            for i in range(8):
                bit = val[15] ^ byte[7 - i]
                val = Mux(bit, ((val << 1) ^ 0x1021) & 0xFFFF, (val << 1) & 0xFFFF)
            return val

        m.d.comb += [
            # image is 1/8 output size
            src_x.eq(self.i.x[3:10]),
            src_y.eq(self.i.y[3:10]),
            # (x, y) -> flattened idx
            pix_idx.eq((src_y << 7) + src_x),
            # in_range check
            in_range.eq(
                self.i.de
                & (self.i.x >= 0)
                & (self.i.y >= 0)
                & (self.i.x < self.OUT_W)
                & (self.i.y < self.OUT_H)
            ),
            # manual delay tuning read from in0
            # (-1, -0.25) => 2 sample delay
            # (-0.25, 0.25) => 1 sample delay
            # (0.25, 1) => no delay
            rx_data.eq(
                Mux(
                    rx_delay_sel == 0,
                    audio_sync[2],
                    Mux(rx_delay_sel == 1, rx_data_d1, rx_data_d2),
                )
            ),
            # Map signed in1 from [-32768, 32767] to [0, 255] blend alpha.
            blend_alpha.eq((audio_sync[1] + (1 << (ASQ.width - 1)))[8:16]),
            # Inverse of top-level image-byte serialization on channel 3:
            # out = (pixel * 257) - 32768  =>  pixel = ((in + 32768) >> 8).
            in2_unsigned.eq(rx_data + (1 << (ASQ.width - 1))),
            feedback_px.eq(in2_unsigned[8:16]),
            # sync channel
            rx_sync.eq(audio_sync[3] > 0),
            audio_tick_rise.eq(self.i.audio_tick ^ audio_tick_d),
            # Set up image read/write addresses.
            original_rp.addr.eq(pix_idx),
            original_tx_rp.addr.eq(tx_idx),
            feedback_display_rp.addr.eq(pix_idx),
            feedback_modify_rp.addr.eq(fb_idx),
            feedback_wp.addr.eq(fb_idx),
            feedback_wp.data.eq(
                Mux(
                    rx_plane == 0,
                    Cat(
                        feedback_modify_rp.data[0:8],
                        feedback_modify_rp.data[8:16],
                        feedback_px,
                    ),
                    Mux(
                        rx_plane == 1,
                        Cat(
                            feedback_modify_rp.data[0:8],
                            feedback_px,
                            feedback_modify_rp.data[16:24],
                        ),
                        Cat(
                            feedback_px,
                            feedback_modify_rp.data[8:16],
                            feedback_modify_rp.data[16:24],
                        ),
                    ),
                )
            ),
            feedback_wp.en.eq(audio_tick_rise & rx_locked & ~rx_sync),
            self.serial_tx_byte.eq(
                Mux(
                    tx_plane == 0,
                    original_tx_rp.data[16:24],
                    Mux(
                        tx_plane == 1,
                        original_tx_rp.data[8:16],
                        original_tx_rp.data[0:8],
                    ),
                )
            ),
            self.serial_tx_sync_active.eq(tx_sync_count != 0),
            blend_inv_alpha_d.eq(0xFF - blend_alpha_d),
            blend_r_mix.eq(
                (original_rp.data[16:24] * blend_alpha_d)
                + (feedback_display_rp.data[16:24] * blend_inv_alpha_d)
            ),
            blend_g_mix.eq(
                (original_rp.data[8:16] * blend_alpha_d)
                + (feedback_display_rp.data[8:16] * blend_inv_alpha_d)
            ),
            blend_b_mix.eq(
                (original_rp.data[0:8] * blend_alpha_d)
                + (feedback_display_rp.data[0:8] * blend_inv_alpha_d)
            ),
        ]

        m.d.spi += spi_wr_en.eq(0)

        next_byte = Cat(self.spi_mosi, spi_rx_shift[:-1])
        m.d.spi += spi_rx_shift.eq(next_byte)
        with m.If(spi_bit_count == 7):
            m.d.spi += spi_bit_count.eq(0)
            with m.Switch(spi_stage):
                with m.Case(0):
                    with m.If(next_byte == 0xA5):
                        m.d.spi += spi_stage.eq(1)
                with m.Case(1):
                    with m.If(next_byte == 0x5A):
                        m.d.spi += spi_stage.eq(2)
                    with m.Else():
                        m.d.spi += spi_stage.eq(0)
                with m.Case(2):
                    m.d.spi += [
                        pkt_frame_id.eq(next_byte),
                        crc_calc.eq(crc16_next(Const(0xFFFF, 16), next_byte)),
                        spi_stage.eq(3),
                    ]
                with m.Case(3):
                    m.d.spi += [
                        pkt_off_hi.eq(next_byte),
                        crc_calc.eq(crc16_next(crc_calc, next_byte)),
                        spi_stage.eq(4),
                    ]
                with m.Case(4):
                    pix_off = Cat(next_byte, pkt_off_hi)
                    m.d.spi += [
                        pkt_pix_off.eq(pix_off),
                        crc_calc.eq(crc16_next(crc_calc, next_byte)),
                        spi_stage.eq(5),
                    ]
                with m.Case(5):
                    m.d.spi += [
                        pkt_len_hi.eq(next_byte),
                        crc_calc.eq(crc16_next(crc_calc, next_byte)),
                        spi_stage.eq(6),
                    ]
                with m.Case(6):
                    pix_len = Cat(next_byte, pkt_len_hi)
                    m.d.spi += crc_calc.eq(crc16_next(crc_calc, next_byte))
                    with m.If(
                        (pix_len > 0) & ((pkt_pix_off + pix_len) <= self.N_PIXELS)
                    ):
                        m.d.spi += [
                            pkt_pix_len.eq(pix_len),
                            pkt_bytes_left.eq(pix_len * 3),
                            pkt_pixel_addr.eq(pkt_pix_off),
                            payload_byte_pos.eq(0),
                            spi_stage.eq(7),
                        ]
                    with m.Else():
                        m.d.spi += [
                            spi_tx_shift.eq(0xE1),
                            spi_tx_active.eq(1),
                            spi_tx_armed.eq(1),
                            spi_stage.eq(0),
                        ]
                with m.Case(7):
                    m.d.spi += [
                        crc_calc.eq(crc16_next(crc_calc, next_byte)),
                        pkt_bytes_left.eq(pkt_bytes_left - 1),
                    ]
                    with m.If(payload_byte_pos == 0):
                        m.d.spi += [
                            payload_r.eq(next_byte),
                            payload_byte_pos.eq(1),
                        ]
                    with m.Elif(payload_byte_pos == 1):
                        m.d.spi += [
                            payload_g.eq(next_byte),
                            payload_byte_pos.eq(2),
                        ]
                    with m.Else():
                        m.d.spi += [
                            payload_byte_pos.eq(0),
                            spi_wr_en.eq(1),
                            spi_wr_addr.eq(pkt_pixel_addr),
                            spi_wr_data.eq(Cat(next_byte, payload_g, payload_r)),
                            pkt_pixel_addr.eq(pkt_pixel_addr + 1),
                        ]
                    with m.If(pkt_bytes_left == 1):
                        m.d.spi += spi_stage.eq(8)
                with m.Case(8):
                    m.d.spi += [
                        crc_hi.eq(next_byte),
                        spi_stage.eq(9),
                    ]
                with m.Case(9):
                    with m.If(Cat(next_byte, crc_hi) == crc_calc):
                        m.d.spi += [
                            spi_tx_shift.eq(0x11),
                            spi_tx_active.eq(1),
                            spi_tx_armed.eq(1),
                            spi_stage.eq(0),
                        ]
                    with m.Else():
                        m.d.spi += [
                            spi_tx_shift.eq(0xE2),
                            spi_tx_active.eq(1),
                            spi_tx_armed.eq(1),
                            spi_stage.eq(0),
                        ]
        with m.Else():
            m.d.spi += spi_bit_count.eq(spi_bit_count + 1)

        with m.If(spi_tx_active):
            with m.If(spi_tx_armed):
                m.d.spi += spi_tx_armed.eq(0)
            with m.Else():
                m.d.spi += spi_tx_shift.eq((spi_tx_shift << 1)[:8])

        # if use_yuv:
        #     m.d.comb += [
        #         fb_y.eq(feedback_display_rp.data[16:24]),
        #         fb_u.eq(feedback_display_rp.data[8:16]),
        #         fb_v.eq(feedback_display_rp.data[0:8]),
        #         fb_d.eq(fb_u - 128),
        #         fb_e.eq(fb_v - 128),
        #         # BT.601 full-range integer approximation.
        #         fb_r_tmp.eq(fb_y + ((fb_e * 359) >> 8)),
        #         fb_g_tmp.eq(fb_y - (((fb_d * 88) + (fb_e * 183)) >> 8)),
        #         fb_b_tmp.eq(fb_y + ((fb_d * 454) >> 8)),
        #         fb_r.eq(Mux(fb_r_tmp < 0, 0, Mux(fb_r_tmp > 255, 255, fb_r_tmp[0:8]))),
        #         fb_g.eq(Mux(fb_g_tmp < 0, 0, Mux(fb_g_tmp > 255, 255, fb_g_tmp[0:8]))),
        #         fb_b.eq(Mux(fb_b_tmp < 0, 0, Mux(fb_b_tmp > 255, 255, fb_b_tmp[0:8]))),
        #     ]

        m.d.sync += [
            in_range_d.eq(in_range),
            blend_alpha_d.eq(blend_alpha),
            audio_tick_d.eq(self.i.audio_tick),
            rx_data_d1.eq(audio_sync[2]),
            rx_data_d2.eq(rx_data_d1),
        ]

        # choose receive delay based on in0: 0, 1, or 2 samples.
        with m.If(audio_sync[0] > (1 << (ASQ.width - 3))):
            m.d.sync += rx_delay_sel.eq(0)
        with m.Elif(audio_sync[0] < -(1 << (ASQ.width - 3))):
            m.d.sync += rx_delay_sel.eq(2)
        with m.Else():
            m.d.sync += rx_delay_sel.eq(1)

        # advance serializer state and feedback write position on each audio tick.
        with m.If(audio_tick_rise):
            with m.If(tx_sync_count != 0):
                m.d.sync += tx_sync_count.eq(tx_sync_count - 1)
            with m.Else():
                with m.If(tx_idx == (self.IMAGE_W * self.IMAGE_H - 1)):
                    m.d.sync += tx_idx.eq(0)
                    with m.If(tx_plane == 2):
                        m.d.sync += [
                            tx_plane.eq(0),
                            tx_sync_count.eq(self.SERIAL_SYNC_BURST),
                        ]
                    with m.Else():
                        m.d.sync += tx_plane.eq(tx_plane + 1)
                with m.Else():
                    m.d.sync += tx_idx.eq(tx_idx + 1)

            with m.If(rx_sync):
                m.d.sync += [
                    rx_locked.eq(1),
                    fb_idx.eq(0),
                    rx_plane.eq(0),
                ]
            with m.Elif(rx_locked):
                with m.If(fb_idx == (self.IMAGE_W * self.IMAGE_H - 1)):
                    m.d.sync += fb_idx.eq(0)
                    with m.If(rx_plane == 2):
                        m.d.sync += rx_plane.eq(0)
                    with m.Else():
                        m.d.sync += rx_plane.eq(rx_plane + 1)
                with m.Else():
                    m.d.sync += fb_idx.eq(fb_idx + 1)

        with m.If(in_range_d):
            # if use_yuv:
            #     m.d.sync += [
            #         self.o.r.eq(
            #             Mux(
            #                 show_original_d,
            #                 original_rp.data[16:24],
            #                 fb_r,
            #             )
            #         ),
            #         self.o.g.eq(Mux(show_original_d, original_rp.data[8:16], fb_g)),
            #         self.o.b.eq(Mux(show_original_d, original_rp.data[0:8], fb_b)),
            #     ]
            # else:
            m.d.sync += [
                self.o.r.eq((blend_r_mix + 0x80) >> 8),
                self.o.g.eq((blend_g_mix + 0x80) >> 8),
                self.o.b.eq((blend_b_mix + 0x80) >> 8),
            ]
        with m.Else():
            m.d.sync += [
                self.o.r.eq(0),
                self.o.g.eq(0),
                self.o.b.eq(0),
            ]

        return m


class EuroVidRack(Elaboratable):
    """
    Top-level EuroVidRack design.

    Provides the clocking, DVI timing/PHY, and audio interface wiring around
    the EuroVidRackCore beamracing logic.
    """

    def __init__(self, clock_settings):

        # This core only works with static modelines
        assert clock_settings.modeline is not None

        self.clock_settings = clock_settings
        self.pmod0 = eurorack_pmod.EurorackPmod(self.clock_settings.audio_clock)
        self.dvi_tgen = dvi.DVITimingGen()

        # Instantiate the EuroVidRack core logic to wrap at top-level.
        self.core = DomainRenamer("dvi")(EuroVidRackCore())

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

            ex_idx = 1
            spi_idx = 0
            platform.add_resources(
                [
                    Resource(
                        "beamrace_spi",
                        spi_idx,
                        Subsignal("cs", Pins("1", dir="i", conn=("pmod", ex_idx))),
                        Subsignal("sck", Pins("7", dir="i", conn=("pmod", ex_idx))),
                        Subsignal("mosi", Pins("9", dir="i", conn=("pmod", ex_idx))),
                        Subsignal("miso", Pins("8", dir="o", conn=("pmod", ex_idx))),
                        Attrs(IO_TYPE="LVCMOS33", DRIVE="8"),
                    )
                ]
            )
            spi = platform.request("beamrace_spi", spi_idx)
        else:
            m.submodules.car = sim.FakeTiliquaDomainGenerator()

        m.submodules.pmod0 = pmod0 = self.pmod0

        m.submodules.dvi_tgen = dvi_tgen = self.dvi_tgen

        # Configure the DVI timing generator to match the selected resolution
        for member in dvi_tgen.timings.signature.members:
            m.d.comb += getattr(dvi_tgen.timings, member).eq(getattr(self.clock_settings.modeline, member))

        # Beamracer core itself
        m.submodules.core = core = self.core

        if sim.is_hw(platform):
            m.d.comb += [
                core.spi_cs.eq(spi.cs.i),
                core.spi_sck.eq(spi.sck.i),
                core.spi_mosi.eq(spi.mosi.i),
                spi.miso.o.eq(core.spi_miso),
            ]
        else:
            m.d.comb += [
                core.spi_cs.eq(1),
                core.spi_sck.eq(0),
                core.spi_mosi.eq(0),
            ]

        # Audio routing: channel 3 carries serialized image samples, channel 4 carries sync framing.
        sample_stb = Signal()
        audio_tick_toggle = Signal()
        tx_sync_active = Signal()
        tx_audio = Signal(signed(ASQ.width))
        tx_sample = Signal(signed(ASQ.width))
        tx_sync = Signal(signed(ASQ.width))
        signed_midpt_offset = 1 << (ASQ.width - 1)

        m.d.comb += [
            tx_sync_active.eq(core.serial_tx_sync_active),
            tx_audio.eq((core.serial_tx_byte * 257) - signed_midpt_offset),
            tx_sample.eq(Mux(tx_sync_active, 0, tx_audio)),
            tx_sync.eq(
                Mux(
                    tx_sync_active,
                    signed_midpt_offset - 1,
                    -signed_midpt_offset,
                )
            ),
        ]

        m.d.comb += sample_stb.eq(pmod0.o_cal.valid & pmod0.i_cal.ready)

        with m.If(sample_stb):
            m.d.sync += audio_tick_toggle.eq(~audio_tick_toggle)

        m.d.comb += [
            # wire pmod valid and ready
            pmod0.i_cal.valid.eq(pmod0.o_cal.valid),
            pmod0.o_cal.ready.eq(pmod0.i_cal.ready),
            # nothing on out1/out2
            # out3 image data
            # out4 frame syncing
            pmod0.i_cal.payload[0].eq(0),
            pmod0.i_cal.payload[1].eq(0),
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


def simulation_ports(fragment):
    # Ports required by `sim.cpp` for end-to-end simulation.
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

if __name__ == "__main__":
    this_path = os.path.dirname(os.path.realpath(__file__))
    top_level_cli(
        EuroVidRack,
        sim_ports=simulation_ports,
        sim_harness="../../src/top/beamrace/sim.cpp",
    )
