"""Tournament fitness: the Quilter-ordered lexicographic tuple. Wirelength
must be the LAST word, DRC the first."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace.tournament import rank_key


def cand(**kw):
    base = dict(drc=0, unrouted=0, prc_pass=10, clearance=0.127,
                min_w=0.15, vias=100, wl=1000)
    base.update(kw)
    return base


class TestRank(unittest.TestCase):
    def test_drc_beats_everything(self):
        clean_but_long = cand(drc=0, wl=9000, vias=500)
        dirty_but_short = cand(drc=3, wl=1000, vias=50)
        self.assertLess(rank_key(clean_but_long), rank_key(dirty_but_short))

    def test_completion_beats_wirelength(self):
        complete_long = cand(unrouted=0, wl=5000)
        incomplete_short = cand(unrouted=2, wl=1000)
        self.assertLess(rank_key(complete_long), rank_key(incomplete_short))

    def test_prc_beats_vias_and_length(self):
        physics_good = cand(prc_pass=12, vias=400, wl=4000)
        physics_poor = cand(prc_pass=6, vias=100, wl=2000)
        self.assertLess(rank_key(physics_good), rank_key(physics_poor))

    def test_conservative_rules_beat_length(self):
        # Tournament #1's exact lesson: pad 0.6 (looser) won despite worst
        # gate wirelength — conservativeness outranks wirelength.
        roomy = cand(clearance=0.2, wl=4000)
        tight = cand(clearance=0.127, wl=2000)
        self.assertLess(rank_key(roomy), rank_key(tight))

    def test_wirelength_is_the_final_tiebreak(self):
        a = cand(wl=1000)
        b = cand(wl=2000)
        self.assertLess(rank_key(a), rank_key(b))

    def test_missing_drc_ranks_last(self):
        graded = cand(drc=2)
        ungraded = cand(drc=None)
        self.assertLess(rank_key(graded), rank_key(ungraded))


if __name__ == "__main__":
    unittest.main()
