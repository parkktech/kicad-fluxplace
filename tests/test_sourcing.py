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



def test_unreachable_api_returns_within_the_cap(tmp_path, monkeypatch):
    """R107: every request hangs/fails; check() returns within [parts] lookup_budget_s + 1 request, parts are ERR (not a pass, not a blocker)."""
    import time
    import urllib.error
    from fluxplace import redact as RD
    monkeypatch.setenv("FLUXPLACE_LOOKUP_BUDGET_S", "2")
    monkeypatch.setenv("FLUXPLACE_HTTP_DOWN_AFTER", "1000000")          # the host-down negative cache is not what is tested here
    monkeypatch.setattr(S, "credentials", lambda: {"DIGIKEY_CLIENT_ID": "x", "DIGIKEY_CLIENT_SECRET": "y", "MOUSER_API_KEY": "z"})
    monkeypatch.setattr(S, "_DK_MIN_INTERVAL", 0, raising=False)
    seen = []

    def slow(req, timeout=30):
        seen.append(timeout)
        time.sleep(min(timeout, 0.6))
        raise urllib.error.URLError("unreachable")
    monkeypatch.setattr(RD.urllib.request, "urlopen", slow)
    RD._DOWN.clear()
    by = {f"PART{i}": [f"R{i}"] for i in range(40)}
    t0 = time.time()
    report, counts = S.check(by, qty=5, cache_dir=str(tmp_path), log=lambda *_: None)
    took = time.time() - t0
    assert took < 6, took
    assert set(report) == set(by) and counts.get("OK", 0) == 0 and counts.get("NONE", 0) == 0
    assert all(v["verdict"] == "ERR" for v in report.values())
    assert S._DEADLINE[0] == 0.0


def test_host_down_is_remembered_so_the_outage_costs_two_timeouts(tmp_path, monkeypatch):
    import time
    import urllib.error
    from fluxplace import redact as RD
    monkeypatch.setenv("FLUXPLACE_LOOKUP_BUDGET_S", "0")
    monkeypatch.setattr(S, "credentials", lambda: {"MOUSER_API_KEY": "z"})
    calls = []

    def dead(req, timeout=30):
        calls.append(1)
        raise urllib.error.URLError("unreachable")
    monkeypatch.setattr(RD.urllib.request, "urlopen", dead)
    RD._DOWN.clear()
    report, _ = S.check({f"P{i}": [f"R{i}"] for i in range(30)}, qty=1, cache_dir=str(tmp_path), log=lambda *_: None)
    assert len(calls) == 2 and all(v["verdict"] == "ERR" for v in report.values())


if __name__ == "__main__":
    unittest.main()
