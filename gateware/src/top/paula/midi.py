from amaranth import *
from amaranth.lib import wiring
from typing import Tuple

from tiliqua import midi as tiliqua_midi


class RegisterLinearMapping(object):
    """Map unsigned controller values into a register value via LUT."""

    def __init__(
        self,
        enc_range: Tuple[int, int],
        reg_range: Tuple[int, int],
        reg_init: int,
    ):
        self.enc_min, self.enc_max = enc_range
        self.param_min, self.param_max = reg_range
        self.reg_init = int(reg_init)
        self.lut_len = (self.enc_max - self.enc_min) + 1

        self.lut_values = self._build_lut_values()
        max_val = max(self.lut_values)
        value_bits = max(max_val, 1).bit_length()
        value_shape = unsigned(value_bits)
        self._lut = Array(Const(v, value_shape) for v in self.lut_values)

        self.target = Signal(range(max_val + 1), init=reg_init)
        self.written = Signal(range(max_val + 1), init=reg_init)
        self.reset_to_init = Signal(init=0)

    def _build_lut_values(self):
        if self.lut_len == 1:
            return [self.param_min]

        values = []
        span = self.lut_len - 1

        for i in range(self.lut_len):
            t = i / span
            values.append(
                int(round(self.param_min + t * (self.param_max - self.param_min)))
            )
        return values

    def map(self, encoder_value: Signal):
        idx = encoder_value - self.enc_min
        clamped_idx = Mux(
            encoder_value <= self.enc_min,
            0,
            Mux(encoder_value >= self.enc_max, self.lut_len - 1, idx),
        )
        return self._lut[clamped_idx]


class RegisterExpMapping(object):
    """Map unsigned controller values into a register value via LUT."""

    def __init__(
        self,
        enc_range: Tuple[int, int],
        reg_range: Tuple[int, int],
        enc_anchor: int,
        reg_anchor: int,
    ):
        """
        builds a lookup table for encoder values in enc_range that maps to values reg_range
        on an expotential curve. anchors the mapping such that enc_anchor maps to reg_anchor
        """

        self.enc_min, self.enc_max = enc_range
        self.param_min, self.param_max = reg_range
        self.enc_anchor = int(enc_anchor)
        self.reg_anchor = int(reg_anchor)
        self.reg_init = self.reg_anchor

        if self.enc_max < self.enc_min:
            raise ValueError("enc_range max must be >= min")
        self.enc_anchor = max(self.enc_min, min(self.enc_max, self.enc_anchor))
        if self.param_min <= 0 or self.param_max <= 0 or self.reg_anchor <= 0:
            raise ValueError("Exponential mapping requires positive range")

        self.lut_len = (self.enc_max - self.enc_min) + 1

        self.lut_values = self._build_lut_values()
        max_val = max(self.lut_values)
        value_bits = max(max_val, 1).bit_length()
        value_shape = unsigned(value_bits)
        self._lut = Array(Const(v, value_shape) for v in self.lut_values)

        self.target = Signal(range(max_val + 1), init=self.reg_init)
        self.written = Signal(range(max_val + 1), init=self.reg_init)
        self.reset_to_init = Signal(init=0)

    def _build_lut_values(self):
        if self.lut_len == 1:
            return [self.reg_anchor]

        values = []

        def interp_exp(v0, v1, t):
            ratio = v1 / v0
            return v0 * (ratio**t)

        for i in range(self.lut_len):
            enc = self.enc_min + i
            if enc <= self.enc_anchor:
                seg_enc_min, seg_enc_max = self.enc_min, self.enc_anchor
                seg_val_min, seg_val_max = self.param_min, self.reg_anchor
            else:
                seg_enc_min, seg_enc_max = self.enc_anchor, self.enc_max
                seg_val_min, seg_val_max = self.reg_anchor, self.param_max

            if seg_enc_max == seg_enc_min:
                t = 0.0
            else:
                t = (enc - seg_enc_min) / (seg_enc_max - seg_enc_min)

            values.append(int(round(interp_exp(seg_val_min, seg_val_max, t))))

        values[self.enc_anchor - self.enc_min] = int(self.reg_anchor)
        return values

    def map(self, encoder_value: Signal):
        idx = encoder_value - self.enc_min
        clamped_idx = Mux(
            encoder_value <= self.enc_min,
            0,
            Mux(encoder_value >= self.enc_max, self.lut_len - 1, idx),
        )
        return self._lut[clamped_idx]


class MidiProcessing(Elaboratable):

    def __init__(
        self,
        midi_channel: int,
        cc_mapping: dict | None = None,
        system_clk_hz: float = 60e6,
    ):
        self.midi_channel = midi_channel
        self.cc_mapping = cc_mapping or {}
        self.system_clk_hz = system_clk_hz

        self.o_note_on_valid = Signal()
        self.o_note = Signal(unsigned(8))
        self.o_cc_valid = Signal()
        self.o_cc_num = Signal(unsigned(8))
        self.o_cc_value = Signal(unsigned(8))

    def elaborate(self, platform):
        m = Module()

        midi_pins = platform.request("midi")
        m.submodules.serialrx = serialrx = tiliqua_midi.SerialRx(
            system_clk_hz=self.system_clk_hz, pins=midi_pins
        )
        m.submodules.midi_decode = midi_decode = tiliqua_midi.MidiDecodeSerial()
        wiring.connect(m, serialrx.o, midi_decode.i)

        m.d.comb += [
            midi_decode.o.ready.eq(1),
            self.o_note_on_valid.eq(0),
            self.o_note.eq(0),
            self.o_cc_valid.eq(0),
            self.o_cc_num.eq(0),
            self.o_cc_value.eq(0),
        ]

        unique_mappings = []
        for mapping in self.cc_mapping.values():
            if mapping not in unique_mappings:
                unique_mappings.append(mapping)

        # external pulses force mapped register targets back to init values
        for mapping in unique_mappings:
            with m.If(mapping.reset_to_init):
                m.d.sync += mapping.target.eq(mapping.reg_init)

        with m.If(midi_decode.o.valid):
            msg = midi_decode.o.payload
            with m.If(
                (msg.status.kind == tiliqua_midi.Status.Kind.NOTE_ON)
                & (msg.status.nibble.channel == self.midi_channel)
                & (msg.midi_payload.note_on.velocity != 0)
            ):
                m.d.comb += [
                    self.o_note_on_valid.eq(1),
                    self.o_note.eq(msg.midi_payload.note_on.note),
                ]
            with m.Elif(
                (msg.status.kind == tiliqua_midi.Status.Kind.CONTROL_CHANGE)
                & (msg.status.nibble.channel == self.midi_channel)
            ):
                m.d.comb += [
                    self.o_cc_valid.eq(1),
                    self.o_cc_num.eq(msg.midi_payload.control_change.controller_number),
                    self.o_cc_value.eq(msg.midi_payload.control_change.data),
                ]
                with m.Switch(msg.midi_payload.control_change.controller_number):
                    for cc_number, mapping in self.cc_mapping.items():
                        with m.Case(int(cc_number)):
                            m.d.sync += mapping.target.eq(
                                mapping.map(msg.midi_payload.control_change.data)
                            )

        return m
