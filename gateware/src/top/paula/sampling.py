from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from amaranth_soc import wishbone

class Sample(wiring.Component):
    PSRAM_ADDR_WIDTH = 22

    bus: Out(
        wishbone.Signature(
            addr_width=PSRAM_ADDR_WIDTH,
            data_width=32,
            granularity=8,
            features={"cti", "bte"},
        )
    )

    i_reset: In(1)
    i_sample_tick: In(1)
    i_sample_byte: In(unsigned(8))
    o_pos_zero_cross: Out(1)

    i_capture_flush: In(1)
    i_capture_strobe: In(1)
    i_capture_byte: In(unsigned(8))
    i_capture_word_addr: In(unsigned(16))
    o_capture_word_written: Out(1)

    i_read_en: In(1)
    i_read_addr: In(unsigned(16))
    o_read_valid: Out(1)
    o_read_data: Out(unsigned(16))

    def __init__(self, depth: int, psram_base_word: int):
        self.depth = int(depth)
        self.psram_base_word = int(psram_base_word)
        if self.depth <= 0:
            raise ValueError("depth must be > 0")
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        addr_width = max(1, (self.depth - 1).bit_length())

        in_sample_s8 = Signal(signed(8))
        in_prev_s8 = Signal(signed(8), init=0)

        capture_half = Signal(init=0)
        capture_first = Signal(unsigned(8), init=0)

        write_pending = Signal(init=0)
        write_addr = Signal(unsigned(addr_width), init=0)
        write_data = Signal(unsigned(16), init=0)

        read_cache_addr = Signal(unsigned(addr_width), init=0)
        read_cache_data = Signal(unsigned(16), init=0)
        read_cache_valid = Signal(init=0)
        read_inflight = Signal(init=0)
        read_inflight_addr = Signal(unsigned(addr_width), init=0)

        req_addr = Signal(unsigned(self.bus.addr_width))
        req_we = Signal()
        req_data = Signal(unsigned(self.bus.data_width))
        req_sel = Signal(unsigned(self.bus.sel.shape().width))

        read_stale = Signal()
        read_needed = Signal()
        start_write = Signal()
        start_read = Signal()
        read_ack = Signal()
        write_ack = Signal()

        bus_cyc = Signal(init=0)
        bus_stb = Signal(init=0)
        bus_we = Signal(init=0)
        bus_adr = Signal(unsigned(self.bus.addr_width), init=0)
        bus_dat_w = Signal(unsigned(self.bus.data_width), init=0)
        bus_sel = Signal(unsigned(self.bus.sel.shape().width), init=0)

        m.d.comb += [
            in_sample_s8.eq(self.i_sample_byte.as_signed()),
            self.o_pos_zero_cross.eq(
                self.i_sample_tick & (in_prev_s8 <= 0) & (in_sample_s8 > 0)
            ),
            self.o_capture_word_written.eq(self.i_capture_strobe & capture_half),
            self.o_read_data.eq(read_cache_data),
            self.o_read_valid.eq(
                read_cache_valid
                & (read_cache_addr == self.i_read_addr[:addr_width])
                & (~read_inflight)
            ),
            self.bus.cyc.eq(bus_cyc),
            self.bus.stb.eq(bus_stb),
            self.bus.we.eq(bus_we),
            self.bus.adr.eq(bus_adr),
            self.bus.dat_w.eq(bus_dat_w),
            self.bus.sel.eq(bus_sel),
            self.bus.cti.eq(wishbone.CycleType.CLASSIC),
            self.bus.bte.eq(wishbone.BurstTypeExt.LINEAR),
        ]

        m.d.comb += [
            read_stale.eq(
                (~read_cache_valid)
                | (read_cache_addr != self.i_read_addr[:addr_width])
                | read_inflight
            ),
            read_needed.eq(self.i_read_en & read_stale),
            start_write.eq((~bus_cyc) & write_pending),
            start_read.eq((~bus_cyc) & (~write_pending) & read_needed),
            read_ack.eq(bus_cyc & (~bus_we) & self.bus.ack),
            write_ack.eq(bus_cyc & bus_we & self.bus.ack),
            req_addr.eq(self.psram_base_word + write_addr),
            req_we.eq(1),
            req_data.eq(write_data),
            req_sel.eq(0b0011),
        ]

        with m.If(start_read):
            m.d.comb += [
                req_addr.eq(self.psram_base_word + self.i_read_addr[:addr_width]),
                req_we.eq(0),
                req_data.eq(0),
                req_sel.eq(0b0011),
            ]

        with m.If(self.i_reset | self.i_capture_flush):
            m.d.sync += [
                in_prev_s8.eq(0),
                capture_half.eq(0),
                capture_first.eq(0),
                write_pending.eq(0),
                read_cache_valid.eq(0),
                read_inflight.eq(0),
                bus_cyc.eq(0),
                bus_stb.eq(0),
                bus_we.eq(0),
            ]
        with m.Elif(self.i_capture_strobe):
            with m.If(capture_half == 0):
                m.d.sync += [
                    capture_first.eq(self.i_capture_byte),
                    capture_half.eq(1),
                ]
            with m.Else():
                m.d.sync += capture_half.eq(0)

        with m.If(self.o_capture_word_written):
            m.d.sync += [
                write_pending.eq(1),
                write_addr.eq(self.i_capture_word_addr[:addr_width]),
                write_data.eq(Cat(self.i_capture_byte, capture_first)),
            ]

        with m.If(start_write | start_read):
            m.d.sync += [
                bus_cyc.eq(1),
                bus_stb.eq(1),
                bus_we.eq(req_we),
                bus_adr.eq(req_addr),
                bus_dat_w.eq(req_data),
                bus_sel.eq(req_sel),
            ]
            with m.If(start_read):
                m.d.sync += [
                    read_inflight.eq(1),
                    read_inflight_addr.eq(self.i_read_addr[:addr_width]),
                ]

        with m.If(self.bus.ack):
            m.d.sync += [
                bus_cyc.eq(0),
                bus_stb.eq(0),
            ]

        with m.If(write_ack):
            m.d.sync += write_pending.eq(0)

        with m.If(read_ack):
            m.d.sync += [
                read_cache_data.eq(self.bus.dat_r[:16]),
                read_cache_addr.eq(read_inflight_addr),
                read_cache_valid.eq(1),
                read_inflight.eq(0),
            ]

        with m.If(self.i_sample_tick):
            m.d.sync += in_prev_s8.eq(in_sample_s8)

        return m
