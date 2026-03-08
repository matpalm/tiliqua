# Copyright (c) 2024 S. Holzapfel <me@sebholzapfel.com>
#
# SPDX-License-Identifier: CERN-OHL-S-2.0

"""Streaming DSP library with a strong focus on audio."""

import os

from amaranth.lib import stream

from amaranth_future import fixed

# Native 'Audio sample SQ': shape of audio samples from CODEC.
# Signed fixed point, where -1 to +1 represents -8.192V to +8.192V.
_ASQ_WIDTH = int(os.environ.get('TILIQUA_ASQ_WIDTH', '16'))
_ASQ_I_BITS = int(os.environ.get('TILIQUA_ASQ_I_BITS', '1'))
ASQ = fixed.SQ(_ASQ_I_BITS, _ASQ_WIDTH - _ASQ_I_BITS)


def asq_from_volts(volts):
    """Convert a voltage to a ``fixed.Const`` of shape ``ASQ`` (4 counts/mV)."""
    return fixed.Const(volts / 8.192, shape=ASQ)

# Components that are accessed using `dsp.fft.STFT()`-like pattern (qualified)
from . import block, complex, delay_effect, fft, mac, spectral

# Components that can be accessed directly using `dsp.VCA()`-like pattern
from .delay_line import *
from .effects import *
from .filters import *
from .misc import *
from .mix import *
from .oneshot import *
from .oscillators import *
from .resample import *
from .stream_util import *
from .vca import *
from .voice_block import *

# Dummy values used to hook up to unused stream in/out ports, so they don't block forever
ASQ_READY = stream.Signature(ASQ, always_ready=True).flip().create()
ASQ_VALID = stream.Signature(ASQ, always_valid=True).create()
