from amaranth import *
from amaranth.lib.memory import Memory
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

class FakeAgnus(wiring.Component):

    # TODO: if we go to 3 channel processing let's introduce arrays

    BUFFER_DEPTH = 2048

    i_audio_dmal: In(unsigned(2))
    i_audio_dmas: In(unsigned(2))
    o_audio_grant: Out(unsigned(2))
    o_audio_restart_ack: Out(unsigned(2))

    i_sample_tick: In(1)
    i_sample0_word: In(unsigned(16))
    i_sample1_word: In(unsigned(16))
    o_audio_data0: Out(unsigned(16))
    o_audio_data1: Out(unsigned(16))

    i_reg_write: In(1)
    i_reg_addr: In(unsigned(9))
    i_reg_data: In(unsigned(16))
    o_reg_read_data: Out(unsigned(16))

    def elaborate(self, platform):
        m = Module()

        m.submodules.buf0 = buf0 = Memory(
            shape=unsigned(16), depth=self.BUFFER_DEPTH, init=[]
        )
        m.submodules.buf1 = buf1 = Memory(
            shape=unsigned(16), depth=self.BUFFER_DEPTH, init=[]
        )

        wr0 = buf0.write_port()
        wr1 = buf1.write_port()
        rd0 = buf0.read_port()
        rd1 = buf1.read_port()

        wr_ptr = Signal(range(self.BUFFER_DEPTH), init=0)
        rd_ptr0 = Signal(range(self.BUFFER_DEPTH), init=0)
        rd_ptr1 = Signal(range(self.BUFFER_DEPTH), init=0)

        with m.If(self.i_sample_tick):
            m.d.sync += wr_ptr.eq(Mux(wr_ptr == (self.BUFFER_DEPTH - 1), 0, wr_ptr + 1))

        # Align DMA readers to a delayed point in the ring buffer on restart.
        with m.If(self.i_audio_dmas[0]):
            m.d.sync += rd_ptr0.eq(wr_ptr)
        with m.If(self.i_audio_dmas[1]):
            m.d.sync += rd_ptr1.eq(wr_ptr)

        with m.If(self.i_sample_tick):
            m.d.sync += rd_ptr0.eq(
                Mux(rd_ptr0 == (self.BUFFER_DEPTH - 1), 0, rd_ptr0 + 1)
            )
            m.d.sync += rd_ptr1.eq(
                Mux(rd_ptr1 == (self.BUFFER_DEPTH - 1), 0, rd_ptr1 + 1)
            )
        with m.Elif(self.o_audio_grant[1]):
            m.d.sync += rd_ptr1.eq(
                Mux(rd_ptr1 == (self.BUFFER_DEPTH - 1), 0, rd_ptr1 + 1)
            )

        m.d.comb += [
            # Write incoming sample words into ring buffers at sample tick rate.
            wr0.en.eq(self.i_sample_tick),
            wr0.addr.eq(wr_ptr),
            wr0.data.eq(self.i_sample0_word),
            wr1.en.eq(self.i_sample_tick),
            wr1.addr.eq(wr_ptr),
            wr1.data.eq(self.i_sample1_word),
            # Read side follows DMA-consumed pointers.
            rd0.en.eq(1),
            rd0.addr.eq(rd_ptr0),
            rd1.en.eq(1),
            rd1.addr.eq(rd_ptr1),
            self.o_audio_data0.eq(rd0.data),
            self.o_audio_data1.eq(rd1.data),
            # Grant immediately and ack restart requests.
            self.o_audio_grant.eq(self.i_audio_dmal),
            self.o_audio_restart_ack.eq(self.i_audio_dmas),
            self.o_reg_read_data.eq(0),
        ]

        return m
