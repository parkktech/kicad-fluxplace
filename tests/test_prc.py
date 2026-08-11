"""Placement-PRC tests: tolerance windows, rank scaling, order inversions."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace.prc import score


def part(x=0, y=0, pins=None):
    return dict(w=2.0, h=1.0, x=x, y=y, angle0=0.0, pins=pins or {})


class TestPRC(unittest.TestCase):
    def _comp(self):
        return dict(
            power_nets=[], diff_pairs=[],
            bypass_caps=[
                dict(cap="C1", parent="U1", net="3V3", pin="VDD",
                     farads=1e-7, rank=0),
                dict(cap="C2", parent="U1", net="3V3", pin="VDD",
                     farads=1e-5, rank=1)],
            crystals=[dict(crystal="Y1", parent="U1", nets=["XIN"],
                           series_r=[], load_caps=["C5"])],
            converters=[dict(u="U2", l="L1", sw="SW", vin="5V", vout="3V3",
                             cin="C10", cout="C12", fb_nets=[],
                             hot_loop=["U2", "L1", "C10", "C12"])])

    def _parts(self):
        return {
            "U1": part(pins={"3V3": (0.5, 0), "XIN": (-0.5, 0)}),
            "C1": part(pins={"3V3": (0, 0)}),
            "C2": part(pins={"3V3": (0, 0)}),
            "Y1": part(pins={"XIN": (0, 0)}),
            "C5": part(), "U2": part(), "L1": part(),
            "C10": part(), "C12": part(),
        }

    def test_good_placement_passes(self):
        pos = {"U1": (0, 0), "C1": (1.5, 1), "C2": (3, 1), "Y1": (-2, 0),
               "C5": (-2, 1.5), "U2": (30, 0), "L1": (33, 0),
               "C10": (28, 2), "C12": (33, 2)}
        rows, npass, nfail = score(self._parts(), pos, {}, self._comp())
        self.assertEqual(nfail, 0, [r for r in rows if not r["ok"]])
        self.assertGreater(npass, 5)

    def test_far_decap_fails_rank_window(self):
        pos = {"U1": (0, 0), "C1": (20, 0), "C2": (3, 1), "Y1": (-2, 0),
               "C5": (-2, 1.5), "U2": (30, 0), "L1": (33, 0),
               "C10": (28, 2), "C12": (33, 2)}
        rows, npass, nfail = score(self._parts(), pos, {}, self._comp())
        bad = [r for r in rows if not r["ok"]]
        self.assertTrue(any(r["check"] == "pin-distance" and "C1" in r["refs"]
                            for r in bad))

    def test_cap_order_inversion(self):
        # bulk cap C2 closer than the 100n C1 -> ordering violation
        pos = {"U1": (0, 0), "C1": (8, 0), "C2": (2, 0), "Y1": (-2, 0),
               "C5": (-2, 1.5), "U2": (30, 0), "L1": (33, 0),
               "C10": (28, 2), "C12": (33, 2)}
        rows, _, _ = score(self._parts(), pos, {}, self._comp())
        order = [r for r in rows if r["check"] == "cap-order"]
        self.assertEqual(len(order), 1)
        self.assertFalse(order[0]["ok"])

    def test_blown_hot_loop_fails_area(self):
        pos = {"U1": (0, 0), "C1": (1.5, 1), "C2": (3, 1), "Y1": (-2, 0),
               "C5": (-2, 1.5), "U2": (30, 0), "L1": (36, 0),
               "C10": (30, 25), "C12": (36, 25)}
        rows, _, _ = score(self._parts(), pos, {}, self._comp())
        area = [r for r in rows if r["check"] == "loop-area"]
        self.assertEqual(len(area), 1)
        self.assertFalse(area[0]["ok"])


if __name__ == "__main__":
    unittest.main()
