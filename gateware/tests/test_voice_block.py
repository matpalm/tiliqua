# Copyright (c) 2024 Seb Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0

import unittest

from amaranth import *
from amaranth.sim import *

from amaranth_future import fixed
from tiliqua import dsp
from tiliqua.dsp import ASQ
from tiliqua.dsp.voice_block import VoiceBlock
from tiliqua.test import stream


class VoiceBlockTests(unittest.TestCase):

    def test_voice_block_basic(self):
        """Write a saw wavetable, gate voice 0, check output is produced."""

        m = Module()
        m.submodules.dut = dut = VoiceBlock(n_voices=8)

        WT_SIZE = VoiceBlock.WAVETABLE_SIZE

        # -0.9 to +0.9 sawtooth
        saw_table = [int(0.9 * 32767 * (2.0 * i / WT_SIZE - 1.0))
                     for i in range(WT_SIZE)]

        async def testbench(ctx):

            # write wavetable
            for addr, sample in enumerate(saw_table):
                ctx.set(dut.wt_write_addr, addr)
                ctx.set(dut.wt_write_data, sample)
                ctx.set(dut.wt_write_en, 1)
                await ctx.tick()
            ctx.set(dut.wt_write_en, 0)
            await ctx.tick()

            # set ADSR params
            EnvUQ = dsp.MultiADSR.EnvUQ
            ctx.set(dut.attack_rate, fixed.Const(0.125, shape=EnvUQ))
            ctx.set(dut.decay_rate, fixed.Const(0.008, shape=EnvUQ))
            ctx.set(dut.sustain_level, fixed.Const(0.625, shape=EnvUQ))
            ctx.set(dut.release_rate, fixed.Const(0.004, shape=EnvUQ))
            ctx.set(dut.reso, 0x2000)

            # gate voice 0
            ctx.set(dut.voice_gates[0], 1)
            ctx.set(dut.voice_freq_incs[0], fixed.Const(0.02, shape=ASQ))
            ctx.set(dut.voice_velocity[0], fixed.Const(0.99, shape=EnvUQ))
            await ctx.tick()

            # collect some samples
            peak_l = 0.0
            for _ in range(500):
                sample = await stream.get(ctx, dut.o)
                peak_l = max(peak_l, abs(sample[0].as_float()))

            # release gate on voice 0
            ctx.set(dut.voice_gates[0], 0)
            for _ in range(250):
                await stream.get(ctx, dut.o)

            assert peak_l > 0.01, "left channel should have output"

        sim = Simulator(m)
        sim.add_clock(1e-6)
        sim.add_testbench(testbench)
        with sim.write_vcd(vcd_file=open("test_voice_block.vcd", "w")):
            sim.run()
