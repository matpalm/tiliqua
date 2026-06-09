"""
Paula recorder/playback scaffold using on-chip EBR.

Implements two independent mono sampler paths:

- sample 0: in0 -> out0, notes 44 (record toggle) / 36 (playback)
- sample 1: in1 -> out1, notes 45 (record toggle) / 37 (playback)

Recording is deterministically decimated from 48 kHz input to 14.4 kHz
capture rate using a fixed 3/10 phase accumulator.
"""

from amaranth import *
from amaranth.lib import cdc, data, enum, stream, wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out

from tiliqua import midi
from tiliqua.build.cli import top_level_cli
from tiliqua.build.types import BitstreamHelp
from tiliqua.dsp import ASQ
from tiliqua.periph import eurorack_pmod
from tiliqua.platform import RebootProvider


class PaulaRecorderCore(wiring.Component):

    RECORD_NOTE0 = 44
    PLAY_NOTE0 = 36
    RECORD_NOTE1 = 45
    PLAY_NOTE1 = 37
    MIDI_CHANNEL = 0  # CH1 => control mode on BSP

    i_midi: In(stream.Signature(midi.MidiMessage))
    i: In(stream.Signature(data.ArrayLayout(ASQ, 4)))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 4)))

    def elaborate(self, platform):
        m = Module()

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
                & (msg.status.nibble.channel == self.MIDI_CHANNEL)
                & (msg.midi_payload.note_on.velocity != 0)
            ):
                with m.Switch(msg.midi_payload.note_on.note):
                    with m.Case(self.RECORD_NOTE0):
                        m.d.comb += rec_evt0.eq(1)
                    with m.Case(self.PLAY_NOTE0):
                        m.d.comb += play_evt0.eq(1)
                    with m.Case(self.RECORD_NOTE1):
                        m.d.comb += rec_evt1.eq(1)
                    with m.Case(self.PLAY_NOTE1):
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


class Sample(wiring.Component):

    class PlayState(enum.Enum, shape=unsigned(2)):
        IDLE = 0
        PREFETCH = 1
        PLAY = 2

    RECORD_SECONDS = 2
    INPUT_FS = 48000
    CAPTURE_FS = 14400
    CAPTURE_NUM = 3
    CAPTURE_DEN = 10
    MAX_CAPTURE_SAMPLES = RECORD_SECONDS * CAPTURE_FS

    i_sample: In(ASQ)
    sample_tick: In(1)
    record_toggle_evt: In(1)
    playback_evt: In(1)
    o_sample: Out(ASQ)

    def elaborate(self, platform):
        m = Module()

        m.submodules.sample_mem = sample_mem = Memory(
            shape=signed(8), depth=self.MAX_CAPTURE_SAMPLES, init=[]
        )
        wr = sample_mem.write_port()
        rd = sample_mem.read_port()

        recording = Signal(init=0)
        valid_length = Signal(range(self.MAX_CAPTURE_SAMPLES + 1), init=0)
        write_addr = Signal(range(self.MAX_CAPTURE_SAMPLES + 1), init=0)

        play_state = Signal(self.PlayState, init=self.PlayState.IDLE)
        play_addr = Signal(range(self.MAX_CAPTURE_SAMPLES + 1), init=0)
        play_count = Signal(range(self.MAX_CAPTURE_SAMPLES + 1), init=0)
        play_acc = Signal(range(self.CAPTURE_DEN), init=0)

        # Deterministic decimator phase for 48 kHz -> 14.4 kHz (3/10 ratio).
        decim_acc = Signal(range(self.CAPTURE_DEN), init=0)

        sample_i16 = Signal(signed(ASQ.as_shape().width))
        sample_q8 = Signal(signed(8))

        do_write = Signal()
        do_capture_strobe = Signal()
        do_record_input = Signal()
        do_prefetch = Signal()
        do_play_tick = Signal()
        do_play_advance = Signal()
        play_last = Signal()
        do_read = Signal()

        m.d.comb += [
            self.o_sample.eq(0),
            sample_i16.eq(self.i_sample.as_value()),
            sample_q8.eq(sample_i16 >> 8),
        ]

        with m.If(play_state == self.PlayState.PLAY):
            m.d.comb += self.o_sample.as_value().eq(rd.data << 8)

        m.d.comb += [
            do_record_input.eq(
                self.sample_tick
                & recording
                & ~self.record_toggle_evt
                & ~self.playback_evt
            ),
            do_capture_strobe.eq(do_record_input & (decim_acc >= 7)),
            do_write.eq(do_capture_strobe & (write_addr < self.MAX_CAPTURE_SAMPLES)),
            wr.en.eq(do_write),
            wr.addr.eq(write_addr),
            wr.data.eq(sample_q8),
            do_prefetch.eq(
                self.sample_tick
                & (play_state == self.PlayState.PREFETCH)
                & ~self.record_toggle_evt
                & ~self.playback_evt
            ),
            do_play_tick.eq(
                self.sample_tick
                & (play_state == self.PlayState.PLAY)
                & ~self.record_toggle_evt
                & ~self.playback_evt
            ),
            do_play_advance.eq(do_play_tick & (play_acc >= 7)),
            play_last.eq((play_count + 1) >= valid_length),
            do_read.eq(do_prefetch | (do_play_advance & ~play_last)),
            rd.en.eq(do_read),
            rd.addr.eq(play_addr),
        ]

        with m.If(self.record_toggle_evt):
            with m.If(recording):
                m.d.sync += recording.eq(0)
            with m.Else():
                m.d.sync += [
                    recording.eq(1),
                    write_addr.eq(0),
                    valid_length.eq(0),
                    decim_acc.eq(0),
                    play_state.eq(self.PlayState.IDLE),
                    play_addr.eq(0),
                    play_count.eq(0),
                    play_acc.eq(0),
                ]
        with m.Elif(self.playback_evt):
            with m.If(valid_length != 0):
                m.d.sync += [
                    recording.eq(0),
                    play_state.eq(self.PlayState.PREFETCH),
                    play_addr.eq(0),
                    play_count.eq(0),
                    play_acc.eq(0),
                ]

        with m.If(do_record_input):
            with m.If(do_capture_strobe):
                m.d.sync += decim_acc.eq(decim_acc - 7)
            with m.Else():
                m.d.sync += decim_acc.eq(decim_acc + self.CAPTURE_NUM)

        with m.If(do_write):
            m.d.sync += [
                write_addr.eq(write_addr + 1),
                valid_length.eq(write_addr + 1),
            ]
            with m.If((write_addr + 1) >= self.MAX_CAPTURE_SAMPLES):
                m.d.sync += recording.eq(0)

        with m.If(do_prefetch):
            m.d.sync += [
                play_state.eq(self.PlayState.PLAY),
                play_addr.eq(play_addr + 1),
            ]

        with m.If(do_play_tick):
            with m.If(do_play_advance):
                m.d.sync += play_acc.eq(play_acc - 7)
                with m.If(play_last):
                    m.d.sync += [
                        play_state.eq(self.PlayState.IDLE),
                        play_addr.eq(0),
                        play_count.eq(0),
                        play_acc.eq(0),
                    ]
                with m.Else():
                    m.d.sync += [
                        play_count.eq(play_count + 1),
                        play_addr.eq(play_addr + 1),
                    ]
            with m.Else():
                m.d.sync += play_acc.eq(play_acc + self.CAPTURE_NUM)

        return m


class PaulaTop(Elaboratable):

    bitstream_help = BitstreamHelp(
        brief="Paula record/playback (EBR, 2s max)",
        io_left=[
            "sample0 in",
            "sample1 in",
            "cv reserve 0",
            "",
            "sample0 out",
            "sample1 out",
            "",
            "",
        ],
        io_right=["", "", "", "", "", "TRS MIDI in"],
    )

    def __init__(self, clock_settings):
        self.clock_settings = clock_settings
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.car = car = platform.clock_domain_generator(self.clock_settings)
        m.submodules.reboot = reboot = RebootProvider(car.settings.frequencies.sync)
        m.submodules.btn = cdc.FFSynchronizer(
            platform.request("encoder").s.i, reboot.button
        )

        m.submodules.pmod0_provider = pmod0_provider = eurorack_pmod.FFCProvider()
        m.submodules.pmod0 = pmod0 = eurorack_pmod.EurorackPmod(
            self.clock_settings.audio_clock
        )
        wiring.connect(m, pmod0.pins, pmod0_provider.pins)
        m.d.comb += pmod0.codec_mute.eq(reboot.mute)

        m.submodules.core = core = PaulaRecorderCore()
        wiring.connect(m, pmod0.o_cal, core.i)
        wiring.connect(m, core.o, pmod0.i_cal)

        midi_pins = platform.request("midi")
        m.submodules.serialrx = serialrx = midi.SerialRx(
            system_clk_hz=60e6, pins=midi_pins
        )
        m.submodules.midi_decode = midi_decode = midi.MidiDecodeSerial()
        wiring.connect(m, serialrx.o, midi_decode.i)
        wiring.connect(m, midi_decode.o, core.i_midi)

        return m


if __name__ == "__main__":
    top_level_cli(PaulaTop, video_core=False)
