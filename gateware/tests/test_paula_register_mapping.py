import sys
import unittest
from pathlib import Path

PAULA_DIR = Path(__file__).resolve().parents[1] / "src" / "top" / "paula"
if str(PAULA_DIR) not in sys.path:
    sys.path.insert(0, str(PAULA_DIR))

from fake_agnus import FakeAgnus
from midi import RegisterExpMapping


class PaulaRegisterMappingTests(unittest.TestCase):

    def test_audxper_anchor_cc_maps_to_anchor_value(self):
        paula_min_period = 121
        paula_max_period = 0xFFFF
        paula_base_period = FakeAgnus.DEFAULT_AUDxPER
        anchor_cc = 64

        mapping = RegisterExpMapping(
            enc_range=(0, 127),
            reg_range=(paula_max_period, paula_min_period),
            enc_anchor=anchor_cc,
            reg_anchor=paula_base_period,
        )

        for lut_idx, per_val in enumerate(mapping.lut_values):
            cc_val = mapping.enc_min + lut_idx
            print(f"AUDxxxx cc={cc_val:3d} -> per={per_val}")

        lut_idx = anchor_cc - mapping.enc_min
        self.assertEqual(mapping.lut_values[lut_idx], paula_base_period)


if __name__ == "__main__":
    unittest.main()
