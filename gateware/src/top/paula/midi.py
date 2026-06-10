from amaranth import *
from amaranth.lib import wiring

from tiliqua import midi as tiliqua_midi


class MidiProcessing(Elaboratable):
    """TRS MIDI RX + decode with NOTE_ON extraction for a single channel."""

    def __init__(self, midi_channel: int, system_clk_hz: float = 60e6):
        self.midi_channel = midi_channel
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

        return m
