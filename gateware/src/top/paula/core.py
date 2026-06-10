from amaranth import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.wiring import In, Out
import json
from pathlib import Path

from tiliqua import midi
from tiliqua.dsp import ASQ

from sample import Sample

class PaulaRecorderCore(wiring.Component):

    i_midi: In(stream.Signature(midi.MidiMessage))
    i: In(stream.Signature(data.ArrayLayout(ASQ, 4)))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 4)))

    def elaborate(self, platform):
        m = Module()

        cfg_path = Path(__file__).with_name("config.json")
        with open(cfg_path, "r") as f:
            cfg = json.loads(f.read())
        midi_cfg = cfg["samples"]["midi"]
        MIDI_CHANNEL = midi_cfg["channel"]

        def record_note(slot: int):
            return midi_cfg["slots"][slot]["record_note"]

        def play_note(slot: int):
            return midi_cfg["slots"][slot]["play_note"]

        sample_accepted = Signal()

        rec_evt0 = Signal()
        play_evt0 = Signal()
        rec_evt1 = Signal()
        play_evt1 = Signal()

        m.d.comb += [
            self.i.ready.eq(self.o.ready),
            self.o.valid.eq(self.i.valid),
            self.i_midi.ready.eq(1),
            sample_accepted.eq(self.i.valid & self.o.ready),
            rec_evt0.eq(0),
            play_evt0.eq(0),
            rec_evt1.eq(0),
            play_evt1.eq(0),
        ]

        with m.If(self.i_midi.valid):
            msg = self.i_midi.payload
            with m.If(
                (msg.status.kind == midi.Status.Kind.NOTE_ON)
                & (msg.status.nibble.channel == MIDI_CHANNEL)
                & (msg.midi_payload.note_on.velocity != 0)
            ):
                with m.Switch(msg.midi_payload.note_on.note):
                    with m.Case(record_note(0)):
                        m.d.comb += rec_evt0.eq(1)
                    with m.Case(play_note(0)):
                        m.d.comb += play_evt0.eq(1)
                    with m.Case(record_note(1)):
                        m.d.comb += rec_evt1.eq(1)
                    with m.Case(play_note(1)):
                        m.d.comb += play_evt1.eq(1)

        m.submodules.sample0 = sample0 = Sample()
        m.submodules.sample1 = sample1 = Sample()

        m.d.comb += [
            sample0.i_sample.eq(self.i.payload[0]),
            sample0.sample_tick.eq(sample_accepted),
            sample0.record_toggle_evt.eq(rec_evt0),
            sample0.playback_evt.eq(play_evt0),
            sample1.i_sample.eq(self.i.payload[1]),
            sample1.sample_tick.eq(sample_accepted),
            sample1.record_toggle_evt.eq(rec_evt1),
            sample1.playback_evt.eq(play_evt1),
            self.o.payload[0].eq(sample0.o_sample),
            self.o.payload[1].eq(sample1.o_sample),
            self.o.payload[2].eq(0),
            self.o.payload[3].eq(0),
        ]

        return m
