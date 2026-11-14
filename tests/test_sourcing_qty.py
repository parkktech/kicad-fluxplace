"""Quantity-aware sourcing: price breaks, qty-scaled stock grade, cheapest
in-stock source, and candidate alternates. Fixtures are synthetic and every
lookup is a cache hit, so nothing here touches the network."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace import sourcing as S                        # noqa: E402

CREDS = {"DIGIKEY_CLIENT_ID": "synthetic", "DIGIKEY_CLIENT_SECRET": "synthetic",
         "MOUSER_API_KEY": "synthetic"}
BREAKS = [[1, 0.100], [10, 0.080], [100, 0.050]]


def _dk(stock, breaks=BREAKS, status="Active"):
    return [stock, status, breaks[0][1], "", "", breaks]


def _mo(stock, breaks=BREAKS, status="Active"):
    return [stock, status, breaks[0][1], "", "", breaks]


def _partdata(package="0603 (1608 Metric)", value="10kOhms", tol="1%"):
    return {"package": package, "pins": None, "temp_min": None,
            "temp_max": None, "clamp_v": None,
            "raw_params": {"Package / Case": package, "Resistance": value,
                           "Tolerance": tol}}


class TestPriceBreaks(unittest.TestCase):
    def test_parse_digikey_standard_pricing(self):
        p = {"StandardPricing": [{"BreakQuantity": 100, "UnitPrice": 0.05},
                                 {"BreakQuantity": 1, "UnitPrice": "0.10"}]}
        self.assertEqual(S._breaks_dk(p), [[1, 0.10], [100, 0.05]])

    def test_parse_mouser_price_strings(self):
        p = {"PriceBreaks": [{"Quantity": 10, "Price": "$0.08"},
                             {"Quantity": 1, "Price": "$0.10"}]}
        self.assertEqual(S._breaks_mouser(p), [[1, 0.10], [10, 0.08]])

    def test_missing_breaks_are_empty_not_error(self):
        self.assertEqual(S._breaks_dk({}), [])
        self.assertEqual(S._breaks_mouser({"PriceBreaks": [{"Quantity": 1}]}), [])

    def test_unit_price_follows_quantity(self):
        self.assertEqual(S.unit_price_at(BREAKS, 1), (0.100, 1))
        self.assertEqual(S.unit_price_at(BREAKS, 9), (0.100, 1))
        self.assertEqual(S.unit_price_at(BREAKS, 10), (0.080, 1))
        self.assertEqual(S.unit_price_at(BREAKS, 500), (0.050, 1))

    def test_below_first_break_buys_the_moq(self):
        self.assertEqual(S.unit_price_at([[10, 0.08]], 4), (0.08, 10))

    def test_no_breaks_no_price(self):
        self.assertEqual(S.unit_price_at([], 10), (None, None))


class _CacheCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self._patch = mock.patch.object(S, "credentials", return_value=CREDS)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def write_cache(self, sourcing=None, partdata=None):
        now = time.time()
        if sourcing is not None:
            json.dump({m: {"_t": now, "dk": d, "mo": o}
                       for m, (d, o) in sourcing.items()},
                      open(os.path.join(self.dir, S.CACHE_NAME), "w"))
        if partdata is not None:
            json.dump({m: {"_t": now, "data": d} for m, d in partdata.items()},
                      open(os.path.join(self.dir, S.PARTDATA_CACHE), "w"))


class TestQuantityGrade(_CacheCase):
    def test_stock_grade_changes_with_quantity(self):
        # 40 units stocked; one board uses the part once
        self.write_cache({"RC-SYN-1": (_dk(40), None)})
        by = {"RC-SYN-1": ["R1"]}
        at10, _ = S.check(by, cache_dir=self.dir, qty=10, log=lambda *_: None)
        at100, _ = S.check(by, cache_dir=self.dir, qty=100, log=lambda *_: None)
        self.assertEqual(at10["RC-SYN-1"]["required"], 10)
        self.assertEqual(at10["RC-SYN-1"]["verdict"], "OK")
        self.assertEqual(at100["RC-SYN-1"]["required"], 100)
        self.assertEqual(at100["RC-SYN-1"]["verdict"], "LOW")

    def test_required_scales_with_refs_per_board(self):
        self.write_cache({"C-SYN-2": (_dk(200), None)})
        rep, _ = S.check({"C-SYN-2": ["C1", "C2", "C3"]}, cache_dir=self.dir,
                         qty=100, log=lambda *_: None)
        self.assertEqual(rep["C-SYN-2"]["required"], 300)
        self.assertEqual(rep["C-SYN-2"]["verdict"], "LOW")

    def test_legacy_mode_unchanged(self):
        self.write_cache({"RC-SYN-1": (_dk(40), None)})
        rep, counts = S.check({"RC-SYN-1": ["R1"]}, need=10, cache_dir=self.dir,
                              log=lambda *_: None)
        self.assertEqual(rep["RC-SYN-1"]["verdict"], "OK")
        self.assertNotIn("prices", rep["RC-SYN-1"])


class TestQuantityPrice(_CacheCase):
    def test_price_changes_with_quantity(self):
        self.write_cache({"RC-SYN-1": (_dk(100000), _mo(100000))})
        by = {"RC-SYN-1": ["R1"]}
        at1, _ = S.check(by, cache_dir=self.dir, qty=1, log=lambda *_: None)
        at100, _ = S.check(by, cache_dir=self.dir, qty=100, log=lambda *_: None)
        self.assertAlmostEqual(at1["RC-SYN-1"]["chosen_ext"], 0.10)
        self.assertAlmostEqual(at100["RC-SYN-1"]["chosen_ext"], 5.00)
        self.assertAlmostEqual(at100["RC-SYN-1"]["prices"]["digikey"]["unit"], 0.05)

    def test_cheapest_in_stock_source_is_chosen(self):
        dk = _dk(100000, [[1, 0.10], [100, 0.05]])
        mo = _mo(100000, [[1, 0.04], [100, 0.09]])
        self.write_cache({"RC-SYN-1": (dk, mo)})
        rep, _ = S.check({"RC-SYN-1": ["R1"]}, cache_dir=self.dir, qty=100,
                         log=lambda *_: None)
        self.assertEqual(rep["RC-SYN-1"]["chosen"], "digikey")      # 5.00 < 9.00
        self.assertEqual(rep["RC-SYN-1"]["chosen_ext"], 5.00)

    def test_out_of_stock_cheaper_source_is_not_chosen(self):
        dk = _dk(20, [[1, 0.01]])            # cheapest, but too few units
        mo = _mo(1000, [[1, 0.09]])
        self.write_cache({"RC-SYN-1": (dk, mo)})
        rep, _ = S.check({"RC-SYN-1": ["R1"]}, cache_dir=self.dir, qty=100,
                         log=lambda *_: None)
        self.assertEqual(rep["RC-SYN-1"]["chosen"], "mouser")

    def test_old_cache_rows_report_unpriced_not_zero(self):
        old_dk = [500, "Active", 0.1, "", ""]          # written before breaks
        self.write_cache({"RC-SYN-1": (old_dk, None)})
        rep, _ = S.check({"RC-SYN-1": ["R1"]}, cache_dir=self.dir, qty=10,
                         log=lambda *_: None)
        self.assertIsNone(rep["RC-SYN-1"]["chosen_ext"])
        self.assertEqual(rep["RC-SYN-1"]["prices"]["digikey"]["note"],
                         "cache predates price breaks")
        t = S.totals(rep)
        self.assertEqual(t["best"]["unpriced"], 1)

    def test_totals_sum_extended_cost(self):
        self.write_cache({"A-SYN": (_dk(9999, [[1, 0.5]]), None),
                          "B-SYN": (_dk(9999, [[1, 0.25]]), None)})
        rep, _ = S.check({"A-SYN": ["R1", "R2"], "B-SYN": ["C1"]},
                         cache_dir=self.dir, qty=10, log=lambda *_: None)
        t = S.totals(rep)
        self.assertEqual(t["digikey"]["ext"], round(0.5 * 20 + 0.25 * 10, 2))
        self.assertEqual(t["best"]["lines"], 2)


class TestAlternates(_CacheCase):
    def test_same_package_and_value_becomes_candidate(self):
        target = _partdata(value="10kOhms", tol="1%")
        same = _partdata(value="10kOhms", tol="1%")
        other_value = _partdata(value="12kOhms", tol="1%")
        other_pkg = _partdata(package="0402 (1005 Metric)", value="10kOhms")
        self.write_cache(
            {"TGT-SYN": (_dk(0, [[1, 0.1]]), None),          # LEAD
             "ALT-SYN": (_dk(5000, [[1, 0.02]]), None),
             "BAD-VAL": (_dk(5000, [[1, 0.02]]), None),
             "BAD-PKG": (_dk(5000, [[1, 0.02]]), None)},
            partdata={"TGT-SYN": target, "ALT-SYN": same,
                      "BAD-VAL": other_value, "BAD-PKG": other_pkg})
        rep, _ = S.check({"TGT-SYN": ["R1"]}, cache_dir=self.dir, qty=10,
                         log=lambda *_: None)
        alts = rep["TGT-SYN"]["alternates"]
        self.assertEqual([a["mpn"] for a in alts], ["ALT-SYN"])
        self.assertIn("not verified", alts[0]["status"])

    def test_no_alternates_for_ok_lines(self):
        self.write_cache({"TGT-SYN": (_dk(5000, [[1, 0.1]]), None)},
                         partdata={"TGT-SYN": _partdata()})
        rep, _ = S.check({"TGT-SYN": ["R1"]}, cache_dir=self.dir, qty=10,
                         log=lambda *_: None)
        self.assertEqual(rep["TGT-SYN"]["alternates"], [])


if __name__ == "__main__":
    unittest.main()
