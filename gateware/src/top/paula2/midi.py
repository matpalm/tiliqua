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
    ):
        self.enc_min, self.enc_max = enc_range
        self.param_min, self.param_max = reg_range
        self.mapping = mapping
        self.lut_len = (self.enc_max - self.enc_min) + 1

        self.lut_values = self._build_lut_values()
        value_bits = max(self.param_max, 1).bit_length()
        value_shape = unsigned(value_bits)
        self._lut = Array(Const(v, value_shape) for v in self.lut_values)

        max_val = max(self.lut_values)
        self.target = Signal(range(max_val + 1), init=reg_init)
        self.written = Signal(range(max_val + 1), init=0)

    def _build_lut_values(self):
        if self.lut_len == 1:
            return [self.param_min]

        values = []
        span = self.lut_len - 1

        if self.mapping == "linear":
            for i in range(self.lut_len):
                t = i / span
                v = self.param_min + t * (self.param_max - self.param_min)
                values.append(int(round(v)))
            return values

        ratio = self.param_max / self.param_min
        for i in range(self.lut_len):
            t = i / span
            v = self.param_min * (ratio**t)
            values.append(int(round(v)))
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
    """TRS MIDI RX + decode with CONTROL_CHANGE handling for a single channel."""

    def __init__(
        self,
        midi_channel: int,
        cc_mapping: dict | None = None,
        system_clk_hz: float = 60e6,
    ):
        self.midi_channel = midi_channel
        self.cc_mapping = cc_mapping or {}
        self.system_clk_hz = system_clk_hz

    def elaborate(self, platform):
        m = Module()

        midi_pins = platform.request("midi")
        m.submodules.serialrx = serialrx = tiliqua_midi.SerialRx(
            system_clk_hz=self.system_clk_hz, pins=midi_pins
        )
        m.submodules.midi_decode = midi_decode = tiliqua_midi.MidiDecodeSerial()
        wiring.connect(m, serialrx.o, midi_decode.i)

        m.d.comb += midi_decode.o.ready.eq(1)

        with m.If(midi_decode.o.valid):
            msg = midi_decode.o.payload
            with m.If(
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
