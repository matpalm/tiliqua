"""
Paula recorder/playback scaffold using on-chip EBR.

Records mono 8-bit PCM from input channel 0 and plays one-shot to output
channel 0. Recording is deterministically decimated from 48 kHz input to
14.4 kHz capture rate using a fixed 3/10 phase accumulator.

MIDI NOTE_ON events on channel 1 control the transport:

- note 44: pad c# in ctrl mode on BSP start / stops recording
- note 36: pad c in ctrl mode on BSP triggers one-shot playback from sample start
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

    class PlayState(enum.Enum, shape=unsigned(2)):
        IDLE = 0
        PREFETCH = 1
        PLAY = 2

    RECORD_NOTE = 44
    PLAY_NOTE = 36
    MIDI_CHANNEL = 0  # CH1 => control mode on BSP

    RECORD_SECONDS = 2
    INPUT_FS = 48000
    CAPTURE_FS = 14400
    CAPTURE_NUM = 3
    CAPTURE_DEN = 10
    MAX_CAPTURE_SAMPLES = RECORD_SECONDS * CAPTURE_FS

    i_midi: In(stream.Signature(midi.MidiMessage))
    i: In(stream.Signature(data.ArrayLayout(ASQ, 4)))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 4)))

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

        sample_accepted = Signal()
        record_toggle_evt = Signal()
        playback_evt = Signal()

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
            self.i.ready.eq(self.o.ready),
            self.o.valid.eq(self.i.valid),
            self.i_midi.ready.eq(1),
            sample_accepted.eq(self.i.valid & self.o.ready),
            # Channel 0 is the audio output in this milestone.
            self.o.payload[0].eq(0),
            self.o.payload[1].eq(0),
            self.o.payload[2].eq(0),
            self.o.payload[3].eq(0),
            sample_i16.eq(self.i.payload[0].as_value()),
            sample_q8.eq(sample_i16 >> 8),
            record_toggle_evt.eq(0),
            playback_evt.eq(0),
        ]

        with m.If(self.i_midi.valid):
            msg = self.i_midi.payload
            with m.If(
                (msg.status.kind == midi.Status.Kind.NOTE_ON)
                & (msg.status.nibble.channel == self.MIDI_CHANNEL)
                & (msg.midi_payload.note_on.velocity != 0)
            ):
                with m.If(msg.midi_payload.note_on.note == self.RECORD_NOTE):
                    m.d.comb += record_toggle_evt.eq(1)
                with m.Elif(msg.midi_payload.note_on.note == self.PLAY_NOTE):
                    m.d.comb += playback_evt.eq(1)

        with m.If(play_state == self.PlayState.PLAY):
            m.d.comb += self.o.payload[0].as_value().eq(rd.data << 8)

        m.d.comb += [
            do_record_input.eq(
                sample_accepted & recording & ~record_toggle_evt & ~playback_evt
            ),
            do_capture_strobe.eq(do_record_input & (decim_acc >= 7)),
            do_write.eq(do_capture_strobe & (write_addr < self.MAX_CAPTURE_SAMPLES)),
            wr.en.eq(do_write),
            wr.addr.eq(write_addr),
            wr.data.eq(sample_q8),
            do_prefetch.eq(
                sample_accepted
                & (play_state == self.PlayState.PREFETCH)
                & ~record_toggle_evt
                & ~playback_evt
            ),
            do_play_tick.eq(
                sample_accepted
                & (play_state == self.PlayState.PLAY)
                & ~record_toggle_evt
                & ~playback_evt
            ),
            do_play_advance.eq(do_play_tick & (play_acc >= 7)),
            play_last.eq((play_count + 1) >= valid_length),
            do_read.eq(do_prefetch | (do_play_advance & ~play_last)),
            rd.en.eq(do_read),
            rd.addr.eq(play_addr),
        ]

        with m.If(record_toggle_evt):
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
        with m.Elif(playback_evt):
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
            "sample in",
            "cv reserve 0",
            "cv reserve 1",
            "",
            "sample out",
            "",
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
