import os

from amaranth import *


class PaulaAudioWrapper(Elaboratable):
    """Amaranth wrapper for Minimig `paula_audio` and its dependent RTL files."""

    def __init__(self):
        self.clk7_en = Signal()
        self.cck = Signal()
        self.rst = Signal()
        self.strhor = Signal()

        self.reg_address_in = Signal(unsigned(8))
        self.data_in = Signal(unsigned(16))
        self.dmaena = Signal(unsigned(4))
        self.audpen = Signal(unsigned(4))

        self.audint = Signal(unsigned(4))
        self.dmal = Signal(unsigned(4))
        self.dmas = Signal(unsigned(4))
        self.ldata = Signal(unsigned(15))
        self.rdata = Signal(unsigned(15))
        self.ldata_okk = Signal(unsigned(9))
        self.rdata_okk = Signal(unsigned(9))

    def add_verilog_sources(self, platform):
        if platform is None or not hasattr(platform, "add_file"):
            return

        vroot = os.path.join(
            os.path.dirname(os.path.realpath(__file__)),
            "minimig",
        )

        for file_name in [
            "paula_audio.v",
            "paula_audio_channel.v",
            "paula_audio_mixer.v",
            "paula_audio_volume.v",
        ]:
            with open(os.path.join(vroot, file_name), "r") as f:
                platform.add_file(file_name, f.read())

    def elaborate(self, platform):
        m = Module()

        self.add_verilog_sources(platform)

        m.submodules.paula_audio = Instance(
            "paula_audio",
            i_clk=ClockSignal("sync"),
            i_clk7_en=self.clk7_en,
            i_cck=self.cck,
            i_rst=self.rst,
            i_strhor=self.strhor,
            i_reg_address_in=self.reg_address_in,
            i_data_in=self.data_in,
            i_dmaena=self.dmaena,
            o_audint=self.audint,
            i_audpen=self.audpen,
            o_dmal=self.dmal,
            o_dmas=self.dmas,
            o_ldata=self.ldata,
            o_rdata=self.rdata,
            o_ldata_okk=self.ldata_okk,
            o_rdata_okk=self.rdata_okk,
        )

        return m
