"""P2 placement controls: hard regions, hard bounds, cluster anchors,
side-flip picking. Quilter semantics: hard constraints are never silently
violated — they either hold or are reported in stats['outside']."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace.compact import (compact, assign_regions, cluster_anchor_map,
                               pick_flips)


def part(x, y, w=2.0, h=2.0, locked=False, side="F", sheet="root",
         drills=0, tht=False):
    return dict(x=x, y=y, w=w, h=h, locked=locked, side=side, sheet=sheet,
                drills=drills, tht=tht, pins={})


class TestRegions(unittest.TestCase):
    def test_member_stays_inside(self):
        parts = {"U1": part(50, 50, locked=True),
                 "R1": part(90, 90), "R2": part(10, 10)}
        regions = assign_regions(
            parts, [dict(name="rf", side="F", bbox=(80, 80, 100, 100))],
            {"rf": ["R1"]})
        pos, st = compact(parts, 0.3, 0.3, regions=regions)
        x, y = pos["R1"]
        self.assertTrue(80 <= x <= 100 and 80 <= y <= 100,
                        f"R1 escaped its region: {pos['R1']}")
        self.assertEqual(st["outside"], 0)
        # non-member compacted toward the anchor as usual
        self.assertGreater(pos["R2"][0], 10)

    def test_auto_association(self):
        parts = {"U1": part(50, 50, locked=True), "C1": part(85, 85),
                 "C2": part(20, 20)}
        regions = assign_regions(
            parts, [dict(name="ana", side="F", bbox=(80, 80, 100, 100))])
        self.assertEqual(regions[0]["members"], {"C1"})

    def test_region_pins_side(self):
        parts = {"U1": part(50, 50, locked=True), "C1": part(85, 85, side="F")}
        regions = assign_regions(
            parts, [dict(name="bot", side="B", bbox=(80, 80, 100, 100))],
            {"bot": ["C1"]})
        pos, st = compact(parts, 1.0, 1.0, regions=regions)
        self.assertEqual(st["sides"]["C1"], "B")

    def test_hard_bounds_reported_when_impossible(self):
        # 6 parts of 10x10 cannot fit a 12x12 box -> outside > 0, never silent
        parts = {f"U{i}": part(10 * i, 0, w=10, h=10) for i in range(6)}
        pos, st = compact(parts, 1.0, 1.0, bounds=(0, 0, 12, 12))
        self.assertGreater(st["outside"] + st["resid"], 0)

    def test_hard_bounds_hold_when_feasible(self):
        parts = {"R1": part(100, 100, w=2, h=2), "R2": part(-50, -50, w=2, h=2),
                 "R3": part(0, 80, w=2, h=2)}
        pos, st = compact(parts, 1.0, 1.0, anchor=(10, 10),
                          bounds=(0, 0, 20, 20))
        self.assertEqual(st["outside"], 0)
        for r, (x, y) in pos.items():
            self.assertTrue(0 <= x <= 20 and 0 <= y <= 20, (r, x, y))


class TestClusterAnchors(unittest.TestCase):
    def test_members_pull_to_locked_clustermate(self):
        parts = {
            "J1": part(0, 0, locked=True, sheet="rf"),
            "C1": part(60, 0, sheet="rf"),
            "U1": part(100, 100, locked=True, sheet="cpu"),
            "C2": part(60, 100, sheet="cpu"),
        }
        amap = cluster_anchor_map(parts)
        self.assertEqual(amap["C1"], (0.0, 0.0))
        self.assertEqual(amap["C2"], (100.0, 100.0))
        pos, _ = compact(parts, 0.5, 0.5, cluster_anchors=amap)
        # C1 moved toward J1 (x shrinks), C2 stayed near U1 (x grows toward 100)
        self.assertLess(pos["C1"][0], 45)
        self.assertGreater(pos["C2"][0], 75)


class TestFlips(unittest.TestCase):
    def _parts(self):
        return {
            "C1": part(10, 10, w=1.0, h=0.5),               # small cap -> flip
            "C2": part(30, 30, w=1.0, h=0.5),               # in B shadow
            "R1": part(12, 12, w=1.0, h=0.5),
            "U1": part(15, 15, w=8, h=8),                   # IC: never
            "C3": part(20, 20, w=1.0, h=0.5, drills=2),     # THT: never
            "C4": part(22, 22, w=1.0, h=0.5, locked=True),  # locked: never
        }

    def test_decap_mode_uses_comprehension(self):
        comp = dict(bypass_caps=[dict(cap="C1"), dict(cap="C3"),
                                 dict(cap="C4")])
        flips = pick_flips(self._parts(), "decaps", comp=comp)
        self.assertEqual(flips, ["C1"])   # C3 THT, C4 locked

    def test_passives_mode_flips_over_shadow_too(self):
        # a part over the B shadow still flips — compact's obstacle
        # constraint relocates it during legalization (the gate judges)
        ob = [dict(x=30, y=30, w=10, h=10, side="B")]
        flips = pick_flips(self._parts(), "passives", obstacles=ob)
        self.assertEqual(flips, ["C1", "C2", "R1"])   # U1 big, C3 THT, C4 locked

    def test_none_mode(self):
        self.assertEqual(pick_flips(self._parts(), "none"), [])


if __name__ == "__main__":
    unittest.main()
