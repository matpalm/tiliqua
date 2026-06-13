from amaranth import *
from amaranth.lib import wiring
from typing import Tuple

from tiliqua import midi as tiliqua_midi


class RegisterMapping(object):
    """Map unsigned controller values into a register value via LUT."""

    def __init__(
        self,
        enc_range: Tuple[int, int],
        reg_range: Tuple[int, int],
        reg_init: int,
        mapping="linear",
        anchor: Tuple[int, int] | None = None,
    ):
        self.enc_min, self.enc_max = enc_range
        self.param_min, self.param_max = reg_range
        self.mapping = mapping
        self.anchor = anchor
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

        def interp(v0, v1, t):
            if self.mapping == "linear":
                return v0 + t * (v1 - v0)
            ratio = v1 / v0
            return v0 * (ratio**t)

        if self.anchor is None:
            if self.mapping == "exp" and (self.param_min <= 0 or self.param_max <= 0):
                raise ValueError("Exponential mapping requires positive range")

            for i in range(self.lut_len):
                t = i / span
                values.append(int(round(interp(self.param_min, self.param_max, t))))
            return values

        anchor_enc, anchor_val = self.anchor
        anchor_enc = max(self.enc_min, min(self.enc_max, anchor_enc))

        if self.mapping == "exp" and (
            self.param_min <= 0 or anchor_val <= 0 or self.param_max <= 0
        ):
            raise ValueError("Exponential mapping requires positive range")

        for i in range(self.lut_len):
            enc = self.enc_min + i
            if enc <= anchor_enc:
                seg_enc_min, seg_enc_max = self.enc_min, anchor_enc
                seg_val_min, seg_val_max = self.param_min, anchor_val
            else:
                seg_enc_min, seg_enc_max = anchor_enc, self.enc_max
                seg_val_min, seg_val_max = anchor_val, self.param_max

            if seg_enc_max == seg_enc_min:
                t = 0.0
            else:
                t = (enc - seg_enc_min) / (seg_enc_max - seg_enc_min)

            values.append(int(round(interp(seg_val_min, seg_val_max, t))))

        values[anchor_enc - self.enc_min] = int(anchor_val)
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
                with m.Switch(msg.midi_payload.control_change.controller_number):
                    for cc_number, mapping in self.cc_mapping.items():
                        with m.Case(int(cc_number)):
                            m.d.sync += mapping.target.eq(
                                mapping.map(msg.midi_payload.control_change.data)
                            )

        return m
