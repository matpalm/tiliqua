# Copyright (c) 2024 S. Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#

"""Utilities for dealing with contiguous blocks of samples."""

from amaranth import *
from amaranth.lib import data, fifo, stream, wiring
from amaranth.lib.wiring import In, Out


class Block(data.StructLayout):
    """:class:`data.StructLayout` representing a 'Block' of samples.

    shape : Shape
        Shape of the ``sample`` payload of elements in this block.

    This is normally used in combination with  :class:`stream.Signature`, where
    ``valid``, ``ready`` and ``payload.first`` are used to delineate samples
    inside. Blocks are transferred one sample at a time - a practical example
    with blocks of length 8:

    .. code-block:: text

                         |-- block 1 --| |-- block 2 --| |---
        payload.sample:  0 1 2 3 4 5 6 7 8 A B C D E F G H I ...
        payload.first:   -_______________-_______________-__
        valid:           -_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-_-
        ready:           (all ones)

    Most cores here are assuming they are working with blocks of some predefined
    size - that is, each producer/consumer must expect the same size of :class:`Block`.

    Members
    -------
    first : :py:`unsigned(1)`
        Strobe asserted for first sample in a block, deasserted otherwise.
    sample : :py:`shape`
        Payload of this sample in the block.
    """
    def __init__(self, shape):
        # TODO: future - add expected size as metadata and verify on wiring.connect ?
        super().__init__({
            "first": unsigned(1),
            "sample": shape
        })

class WrapCore(wiring.Component):

    """
    Wrap a streaming component with simple ``i``, ``o`` streams such that
    it takes/emits :class:`Block` streams (where ``payload.first`` is tracked).

    This only supports simple cores that have:

    - An input stream ``i`` of type ``stream.Signature(shape)``
    - An output stream ``o`` of type ``stream.Signature(shape)``

    A FIFO of size ``max_latency`` is used to track and propagate ``payload.first`` from
    the input to the output of the wrapped core. The wrapped core must never store more
    than ``max_latency`` elements in flight for this to work correctly.

    Members
    -------
    i : :py:`In(stream.Signature(Block(self.shape_i)))`
        Incoming blocks, where shape of block payload is inherited from the wrapped core.
    o : :py:`In(stream.Signature(Block(self.shape_o)))`
        Outgoing blocks, where shape of block payload is inherited from the wrapped core.
    """

    def __init__(self, core, max_latency=16):
        """
        core : wiring.Component
            DSP core to be wrapped. ``shape_i`` and ``shape_o`` come from
            ``i.payload.shape()`` and ``o.payload.shape()``.
        max_latency : int
            Maximum amount of elements that may be in-flight inside the wrapped core.
        """
        self.core = core
        self.shape_i = core.i.payload.shape()
        self.shape_o = core.o.payload.shape()
        self.max_latency = max_latency
        super().__init__({
            "i": In(stream.Signature(Block(self.shape_i))),
            "o": Out(stream.Signature(Block(self.shape_o))),
        })

    def elaborate(self, platform):
        m = Module()

        m.submodules.dsp_core = dsp_core = self.core

        # FIFO to preserve the 'first' signal
        m.submodules.first_fifo = first_fifo = fifo.SyncFIFOBuffered(
            width=1, depth=self.max_latency
        )

        sample_in = stream.Signature(self.shape_i).create()
        m.d.comb += [
            sample_in.valid.eq(self.i.valid),
            sample_in.payload.eq(self.i.payload.sample),
            self.i.ready.eq(sample_in.ready & first_fifo.w_rdy),
        ]
        wiring.connect(m, sample_in, dsp_core.i)

        # Store 'first' signal in FIFO whenever a sample is transferred
        m.d.comb += [
            first_fifo.w_en.eq(self.i.valid & self.i.ready),
            first_fifo.w_data.eq(self.i.payload.first),
        ]

        sample_out = stream.Signature(self.shape_o).flip().create()
        wiring.connect(m, dsp_core.o, sample_out)

        m.d.comb += [
            self.o.valid.eq(sample_out.valid & first_fifo.r_rdy),
            self.o.payload.sample.eq(sample_out.payload),
            self.o.payload.first.eq(first_fifo.r_data),
            sample_out.ready.eq(self.o.ready & first_fifo.r_rdy),
            first_fifo.r_en.eq(self.o.valid & self.o.ready),
        ]

        return m

class BlockMerge(wiring.Component):
    """Join N :class:`Block` streams into one with a merged payload.

    Waits for all inputs to be valid, then emits a single Block whose
    ``sample`` is a :class:`data.StructLayout` built from the given dict.
    Input port names match the dict keys.

    WARN: The ``first`` flag is forwarded from the first input, this component
    does not drop elements until all ``first`` flags are aligned!

    Parameters
    ----------
    fields : dict of str -> Shape
        Mapping of field names to shapes. Each entry becomes an input port.
    """

    def __init__(self, fields):
        self._names = list(fields.keys())
        ports = {name: In(stream.Signature(Block(shape)))
                 for name, shape in fields.items()}
        ports["o"] = Out(stream.Signature(Block(data.StructLayout(fields))))
        super().__init__(ports)

    def elaborate(self, platform):
        m = Module()
        inputs = [getattr(self, name) for name in self._names]
        all_valid = Cat([i.valid for i in inputs]).all()
        m.d.comb += [
            self.o.valid.eq(all_valid),
            self.o.payload.first.eq(inputs[0].payload.first),
        ]
        for name, inp in zip(self._names, inputs):
            m.d.comb += [
                getattr(self.o.payload.sample, name).eq(inp.payload.sample),
                inp.ready.eq(self.o.ready & all_valid),
            ]
        return m


class BlockSelect(wiring.Component):

    """
    Take a block of a certain size, and drop any entries
    not in 'indices'. Converts a ``Block`` stream of wider
    blocks into a ``Block`` stream of smaller blocks by
    dropping elements based on their index relative to 'first'.
    """

    def __init__(self, shape, indices):
        self._indices = list(indices)
        self._max_ix = max(indices)
        super().__init__({
            "i": In(stream.Signature(Block(shape))),
            "o": Out(stream.Signature(Block(shape))),
        })

    def elaborate(self, platform):
        m = Module()

        ix = Signal(range(self._max_ix + 1))
        keep = Signal()
        first_out = Signal()

        for n, sel in enumerate(self._indices):
            with m.If(ix == sel):
                m.d.comb += keep.eq(1)
                m.d.comb += first_out.eq(n == 0)

        m.d.comb += [
            self.o.payload.sample.eq(self.i.payload.sample),
            self.o.payload.first.eq(first_out),
            self.o.valid.eq(self.i.valid & keep),
            self.i.ready.eq(Mux(keep, self.o.ready, 1)),
        ]

        with m.If(self.i.valid & self.i.ready):
            with m.If(self.i.payload.first):
                m.d.sync += ix.eq(1)
            with m.Else():
                m.d.sync += ix.eq(ix + 1)

        return m


def connect_without_payload(m, stream_o, stream_i):
    """
    Connect 2 :class:`Block` streams *without* connecting the payload.
    This is a useful building block for building custom connectors that
    bridge block streams encapsulating different types.
    """
    m.d.comb += [
        stream_i.valid.eq(stream_o.valid),
        stream_o.ready.eq(stream_i.ready),
    ]
    shape_o = stream_o.payload.shape()
    shape_i = stream_i.payload.shape()
    if isinstance(shape_o, Block) or isinstance(shape_i, Block):
        assert isinstance(shape_o, Block) and isinstance(shape_i, Block)
        m.d.comb += stream_i.payload.first.eq(stream_o.payload.first)

