from amaranth import *
from amaranth.lib import enum, wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out
from math import gcd

from tiliqua.dsp import ASQ
from dither import Dither8BitQuantiser
from led_low_pass import LedLowPass

class Sample(wiring.Component):

    # TODO: fix bug where zero crossing isn't checked when we hit max sample length

    class PlayState(enum.Enum, shape=unsigned(2)):
        IDLE = 0
        PREFETCH = 1
        PLAY = 2

    RECORD_SECONDS = 1
    INPUT_FREQ = 48_000
    CAPTURE_FREQ = 16_726  # protracker .mod files ( high quality )
    _FREQ_GCD = gcd(INPUT_FREQ, CAPTURE_FREQ)
    CAPTURE_NUM = CAPTURE_FREQ // _FREQ_GCD
    CAPTURE_DENOM = INPUT_FREQ // _FREQ_GCD
    CAPTURE_EDGE = CAPTURE_DENOM - CAPTURE_NUM
    MAX_CAPTURE_SAMPLES = RECORD_SECONDS * CAPTURE_FREQ

    i_sample: In(ASQ)
    sample_tick: In(1)
    record_start_evt: In(1)
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
        play_acc = Signal(range(self.CAPTURE_DENOM), init=0)
        decim_acc = Signal(range(self.CAPTURE_DENOM), init=0)

        m.submodules.dither = dither = Dither8BitQuantiser()
        m.submodules.led_lp = led_lp = LedLowPass()

        sample_in = Signal(signed(dither.INPUT_WIDTH))
        raw_out = Signal(signed(dither.INPUT_WIDTH))
        play_sample = Signal(signed(dither.INPUT_WIDTH))
        last_played_sample = Signal(signed(dither.INPUT_WIDTH), init=0)
        fixed_capture_samples = min(self.CAPTURE_FREQ, self.MAX_CAPTURE_SAMPLES)

        do_write = Signal()
        do_capture_strobe = Signal()
        do_record_input = Signal()
        do_prefetch = Signal()
        do_play_tick = Signal()
        do_play_advance = Signal()
        play_last = Signal()
        do_read = Signal()

        m.d.comb += [
            raw_out.eq(0),
            sample_in.eq(self.i_sample.as_value()),
            play_sample.eq(rd.data << dither.TRUNC_SHIFT),
            led_lp.i_sample.eq(raw_out),
            led_lp.tick.eq(self.sample_tick),
            self.o_sample.as_value().eq(led_lp.o_sample),
            dither.i_sample.eq(sample_in),
            dither.tick.eq(self.sample_tick),
        ]

        with m.If(play_state == self.PlayState.PLAY):
            m.d.comb += raw_out.eq(play_sample)
        with m.Elif(play_state == self.PlayState.PREFETCH):
            # Hold last sample through prefetch to avoid a one-sample zero bubble.
            m.d.comb += raw_out.eq(last_played_sample)

        m.d.comb += [
            do_record_input.eq(self.sample_tick & recording & ~self.playback_evt),
            do_capture_strobe.eq(do_record_input & (decim_acc >= self.CAPTURE_EDGE)),
            do_write.eq(do_capture_strobe & (write_addr < self.MAX_CAPTURE_SAMPLES)),
            wr.en.eq(do_write),
            wr.addr.eq(write_addr),
            wr.data.eq(dither.o_sample_q8),
            do_prefetch.eq(
                self.sample_tick
                & (play_state == self.PlayState.PREFETCH)
                & ~self.record_start_evt
                & ~self.playback_evt
            ),
            do_play_tick.eq(
                self.sample_tick
                & (play_state == self.PlayState.PLAY)
                & ~self.record_start_evt
                & ~self.playback_evt
            ),
            do_play_advance.eq(do_play_tick & (play_acc >= self.CAPTURE_EDGE)),
            play_last.eq((play_count + 1) >= valid_length),
            do_read.eq(do_prefetch | (do_play_advance & ~play_last)),
            rd.en.eq(do_read),
            rd.addr.eq(play_addr),
        ]

        with m.If(self.record_start_evt):
            # One-shot capture: start immediately and run for a fixed duration.
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
            # One-shot playback: restart from sample start on each trigger.
            with m.If(valid_length != 0):
                m.d.sync += [
                    recording.eq(0),
                    play_state.eq(self.PlayState.PREFETCH),
                    play_addr.eq(0),
                    play_count.eq(0),
                    play_acc.eq(0),
                    last_played_sample.eq(0),
                ]

        with m.If(self.sample_tick):
            with m.If(play_state == self.PlayState.PLAY):
                m.d.sync += last_played_sample.eq(play_sample)

        with m.If(do_record_input):
            with m.If(do_capture_strobe):
                m.d.sync += decim_acc.eq(decim_acc - self.CAPTURE_EDGE)
            with m.Else():
                m.d.sync += decim_acc.eq(decim_acc + self.CAPTURE_NUM)

        with m.If(do_write):
            m.d.sync += [
                write_addr.eq(write_addr + 1),
                valid_length.eq(write_addr + 1),
            ]
            with m.If((write_addr + 1) >= fixed_capture_samples):
                m.d.sync += [
                    recording.eq(0),
                ]

        with m.If(do_prefetch):
            m.d.sync += [
                play_state.eq(self.PlayState.PLAY),
                play_addr.eq(play_addr + 1),
            ]

        with m.If(do_play_tick):
            with m.If(do_play_advance):
                m.d.sync += play_acc.eq(play_acc - self.CAPTURE_EDGE)
                with m.If(play_last):
                    # One-shot playback: stop at sample end.
                    m.d.sync += [
                        play_state.eq(self.PlayState.IDLE),
                        play_addr.eq(0),
                        play_count.eq(0),
                        last_played_sample.eq(0),
                    ]
                with m.Else():
                    m.d.sync += [
                        play_count.eq(play_count + 1),
                        play_addr.eq(play_addr + 1),
                    ]
            with m.Else():
                m.d.sync += play_acc.eq(play_acc + self.CAPTURE_NUM)

        return m
