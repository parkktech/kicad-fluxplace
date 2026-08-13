"""Sourcing gate: MPN matching and verdict grading.

Every case here is a bug that actually shipped and cried wolf on a real board
(utv-comms-bridge V1.3), and would have triggered a needless part swap.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace import sourcing as S                        # noqa: E402


class TestMpnMatching(unittest.TestCase):
    def test_plating_qualifier_is_cosmetic(self):
        # our BOM says BM03B-GHS-TBT(LF)(SN); DigiKey lists BM03B-GHS-TBT
        # with 27k in stock. Reported as "nobody carries this" before the fix.
        ok, exact = S._same_part("BM03B-GHS-TBT", "BM03B-GHS-TBT(LF)(SN)")
        self.assertTrue(ok)
        self.assertTrue(exact)

    def test_whitespace_is_cosmetic(self):
        # DigiKey lists C&K's part as "KMR221G LFS" — with a space. 95k stock.
        ok, exact = S._same_part("KMR221G LFS", "KMR221GLFS")
        self.assertTrue(ok)
        self.assertTrue(exact)

    def test_different_values_never_match(self):
        # same series, different resistance / different inductance: these are
        # NOT interchangeable and must never be silently accepted
        self.assertFalse(S._same_part("RC0603FR-0710KL", "RC0603FR-0768KL")[0])
        self.assertFalse(S._same_part("XAL7070-153MEC", "XAL7070-562MEC")[0])

    def test_packaging_variant_matches_but_is_flagged(self):
        ok, exact = S._same_part("GRM21BR61C226ME44LX", "GRM21BR61C226ME44L")
        self.assertTrue(ok)
        self.assertFalse(exact)      # caller must surface what it matched

    def test_search_term_strips_qualifiers(self):
        # DigiKey's keyword search returns nothing for the parenthesised form
        self.assertEqual(S._search_term("BM03B-GHS-TBT(LF)(SN)"),
                         "BM03B-GHS-TBT")
        self.assertEqual(S._search_term("RC0603FR-0710KL"), "RC0603FR-0710KL")


class TestGrading(unittest.TestCase):
    def test_stock_passes(self):
        self.assertEqual(S.grade([5000, "Active", 1.0, ""], None, 10)[0], "OK")

    def test_zero_stock_but_catalogued_is_lead_not_blocker(self):
        v = S.grade([0, "Active", 1.0, ""], None, 10)[0]
        self.assertEqual(v, "LEAD")
        self.assertNotIn(v, S.BLOCKERS)

    def test_nobody_carries_it_is_a_blocker(self):
        v = S.grade(None, None, 10, errors=[])[0]
        self.assertEqual(v, "NONE")
        self.assertIn(v, S.BLOCKERS)

    def test_eol_is_a_blocker(self):
        v = S.grade([9999, "Obsolete", 1.0, ""], None, 10)[0]
        self.assertEqual(v, "RISK")
        self.assertIn(v, S.BLOCKERS)

    def test_failed_lookup_is_never_a_blocker(self):
        # a transient Mouser 403 must not read as "nobody stocks this part",
        # or --strict-sourcing aborts a layout over an API hiccup
        v = S.grade(None, None, 10, errors=["Mouser"])[0]
        self.assertEqual(v, "ERR")
        self.assertNotIn(v, S.BLOCKERS)

    def test_partial_evidence_with_error_is_err(self):
        v = S.grade([0, "Active", 1.0, ""], None, 10, errors=["Mouser"])[0]
        self.assertEqual(v, "ERR")
        self.assertNotIn(v, S.BLOCKERS)


if __name__ == "__main__":
    unittest.main()
