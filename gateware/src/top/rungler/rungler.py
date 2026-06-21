from amaranth import *


class Rungler(Elaboratable):
    def __init__(self):
        self.osc1_trigger = Signal()
        self.osc2_data = Signal()

        # 3 bit "DAC"
        self.rungler_out = Signal(3)

        # last bit
        self.bit_out = Signal()

    def elaborate(self, platform):
        m = Module()

        # 8 bit shift register. avoid 0 for init for feedback
        shift_reg = Signal(8, reset=0b00000001)

        # rising edge detection for osc1 to trigger register
        osc1_d = Signal()
        osc1_rising = Signal()
        m.d.sync += osc1_d.eq(self.osc1_trigger)
        m.d.comb += osc1_rising.eq(self.osc1_trigger & ~osc1_d)

        # rungler logic
        with m.If(osc1_rising):
            # xor bits 7 and 6 with data ( is this right? )
            feedback = Signal()
            m.d.comb += feedback.eq(shift_reg[7] ^ shift_reg[6] ^ self.osc2_data)
            # shift left into feedback
            m.d.sync += shift_reg.eq(Cat(feedback, shift_reg[0:7]))

        # takes bits 5,6,7 as 3-bit "R2R dac" => 8 voltages
        m.d.comb += [
            self.rungler_out.eq(Cat(shift_reg[5], shift_reg[6], shift_reg[7])),
            self.bit_out.eq(shift_reg[7]),
        ]

        return m
