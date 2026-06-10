from amaranth import *
from amaranth.lib import wiring

from tiliqua import midi as tiliqua_midi


class MidiProcessing(Elaboratable):
    """TRS MIDI RX + decode with NOTE_ON extraction for a single channel."""

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

        # Map 0..127 MIDI CC data to 0..64 inclusive.
        cc_data = Signal(unsigned(8))
        cc_value_0_to_64 = Signal(unsigned(7))

        m.d.comb += [
            midi_decode.o.ready.eq(1),
            self.o_note_on_valid.eq(0),
            self.o_note.eq(0),
            cc_data.eq(0),
            cc_value_0_to_64.eq((cc_data + 1) >> 1),
        ]

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
                m.d.comb += cc_data.eq(msg.midi_payload.control_change.data)
                with m.Switch(msg.midi_payload.control_change.controller_number):
                    for cc_number, target_signal in self.cc_mapping.items():
                        with m.Case(int(cc_number)):
                            m.d.sync += target_signal.eq(cc_value_0_to_64)

        return m
