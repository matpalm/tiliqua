from amaranth_future import fixed

from tiliqua.dsp import ASQ

# 'Plot Sample SQ': fixed-point type for all plotting/rasterization operations.
# Decoupled from ASQ so wider audio paths don't affect plot scaling.
PSQ = fixed.SQ(ASQ.i_bits, 13)

# The plotting subsystem was brought up assuming this many f_bits for
# fixed-point to pixel-deflection mapping. This is used to fixup pixel
# scaling in some places if PSQ above is changed.
PSQ_BASE_FBITS = 15

def psq_from_volts(volts):
    return fixed.Const(volts / 8.192, shape=PSQ)
