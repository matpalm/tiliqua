from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out


class FakeAgnus(wiring.Component):
    """Hybrid fake Agnus shim: audio DMA lines now, bus placeholders for later."""

    i_audio_dmal: In(unsigned(2))
    i_audio_dmas: In(unsigned(2))
    o_audio_grant: Out(unsigned(2))
    o_audio_restart_ack: Out(unsigned(2))

    i_reg_write: In(1)
    i_reg_addr: In(unsigned(9))
    i_reg_data: In(unsigned(16))
    o_reg_read_data: Out(unsigned(16))

    def elaborate(self, platform):
        m = Module()

        m.d.comb += [
            # For now, grant immediately and ack restart requests.
            self.o_audio_grant.eq(self.i_audio_dmal),
            self.o_audio_restart_ack.eq(self.i_audio_dmas),
            self.o_reg_read_data.eq(0),
        ]

        return m
