"""Comprehension-layer tests: every Quilter heuristic we adopted, plus the
gaps we deliberately exceed (series-R crystals, deterministic converter caps)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace.comprehend import (comprehend, parse_value, ipc2221_width_mm,
                                  rail_voltage)


def P(ref, pad, net, value="", pin="", fp=""):
    return dict(ref=ref, footprint=fp, value=value, pad=pad, pin=pin,
                net=net, drill=False)


class TestValues(unittest.TestCase):
    def test_parse(self):
        self.assertAlmostEqual(parse_value("100n"), 1e-7)
        self.assertAlmostEqual(parse_value("4.7uF"), 4.7e-6)
        self.assertAlmostEqual(parse_value("0.1u"), 1e-7)
        self.assertAlmostEqual(parse_value("10k", "R"), 1e4)
        self.assertAlmostEqual(parse_value("4R7", "R"), 4.7)
        self.assertIsNone(parse_value("DNP"))

    def test_rail_voltage(self):
        self.assertEqual(rail_voltage("3V3"), 3.3)
        self.assertEqual(rail_voltage("+5V"), 5.0)
        self.assertEqual(rail_voltage("1V8"), 1.8)
        self.assertEqual(rail_voltage("28V_IN"), 28.0)
        self.assertEqual(rail_voltage("/power/12V_FAN"), 12.0)
        self.assertIsNone(rail_voltage("SDA"))

    def test_ipc2221_monotonic(self):
        w1 = ipc2221_width_mm(0.5)
        w2 = ipc2221_width_mm(2.0)
        self.assertGreater(w2, w1)
        self.assertGreater(w1, 0.05)
        # internal layers need roughly twice the width
        self.assertGreater(ipc2221_width_mm(1.0, internal=True),
                           ipc2221_width_mm(1.0) * 1.8)


class TestPowerNets(unittest.TestCase):
    def test_quilter_current_floors(self):
        pads = [P("U1", "1", "1V8"), P("U1", "2", "5V"), P("U1", "3", "GND"),
                P("J1", "1", "1V8"), P("J1", "2", "5V")]
        c = comprehend(pads)
        by = {p["net"]: p for p in c["power_nets"]}
        self.assertEqual(by["1V8"]["current_ma"], 200)   # <3V -> 200mA
        self.assertEqual(by["5V"]["current_ma"], 500)    # >=3V -> 500mA
        self.assertNotIn("GND", by)                      # ground is a plane


class TestDiffPairs(unittest.TestCase):
    def test_conventions(self):
        nets = [("USB_DP", "USB_DM"), ("TX+", "TX-"), ("MDI0_P", "MDI0_N"),
                ("SSTXA", "SSTXB"), ("ddr_dqs_t", "ddr_dqs_c")]
        pads = []
        for i, (a, b) in enumerate(nets):
            pads += [P(f"U{i+1}", "1", a), P(f"U{i+1}", "2", b),
                     P(f"J{i+1}", "1", a), P(f"J{i+1}", "2", b)]
        c = comprehend(pads)
        found = {(d["p"], d["n"]) for d in c["diff_pairs"]}
        for a, b in nets:
            self.assertTrue((a, b) in found or (b, a) in found,
                            f"pair {a}/{b} not detected: {found}")

    def test_v_prefix_never_pairs(self):
        pads = [P("U1", "1", "VDDP"), P("U1", "2", "VDDN"),
                P("C9", "1", "VDDP"), P("C9", "2", "VDDN")]
        c = comprehend(pads)
        self.assertEqual(c["diff_pairs"], [])

    def test_series_merge(self):
        # AC coupling caps: PCIE_TX_P -> C1 -> PCIE_TX_C_P (far side)
        pads = [P("U1", "1", "PCIE_TX_P"), P("U1", "2", "PCIE_TX_N"),
                P("C1", "1", "PCIE_TX_P"), P("C1", "2", "PCIE_TXC_P"),
                P("C2", "1", "PCIE_TX_N"), P("C2", "2", "PCIE_TXC_N"),
                P("J1", "1", "PCIE_TXC_P"), P("J1", "2", "PCIE_TXC_N")]
        c = comprehend(pads)
        segs = [set(d["segments"]) for d in c["diff_pairs"]]
        self.assertTrue(any({"PCIE_TX_P", "PCIE_TX_N", "PCIE_TXC_P",
                             "PCIE_TXC_N"} <= s for s in segs), segs)


class TestBypassCaps(unittest.TestCase):
    def test_capacitance_rank(self):
        pads = [P("U1", "1", "3V3", pin="VDD"), P("U1", "2", "GND"),
                P("C1", "1", "3V3", value="10u"), P("C1", "2", "GND"),
                P("C2", "1", "3V3", value="100n"), P("C2", "2", "GND"),
                P("C3", "1", "3V3", value="1u"), P("C3", "2", "GND")]
        c = comprehend(pads)
        rank = {b["cap"]: b["rank"] for b in c["bypass_caps"]}
        self.assertEqual(rank, {"C2": 0, "C3": 1, "C1": 2})  # smallest first

    def test_pin_priority(self):
        pads = [P("U1", "7", "5V", pin="VIN"), P("U1", "8", "GND"),
                P("C1", "1", "5V", value="100n"), P("C1", "2", "GND")]
        c = comprehend(pads)
        self.assertEqual(c["bypass_caps"][0]["pin"], "VIN")

    def test_signal_cap_not_bypass(self):
        pads = [P("U1", "1", "SDA"), P("U1", "2", "GND"),
                P("C1", "1", "SDA"), P("C1", "2", "GND")]
        c = comprehend(pads)
        self.assertEqual(c["bypass_caps"], [])


class TestCrystals(unittest.TestCase):
    def test_direct(self):
        pads = [P("Y1", "1", "XIN"), P("Y1", "2", "XOUT"),
                P("U1", "5", "XIN"), P("U1", "6", "XOUT")]
        c = comprehend(pads)
        self.assertEqual(c["crystals"][0]["parent"], "U1")

    def test_series_r_detected(self):
        # Quilter documents MISSING this: Y1 -> R1 -> U1
        pads = [P("Y1", "1", "XIN"), P("Y1", "2", "XOUT_R"),
                P("R1", "1", "XOUT_R", value="1k"), P("R1", "2", "XOUT"),
                P("U1", "5", "XIN"), P("U1", "6", "XOUT")]
        c = comprehend(pads)
        self.assertEqual(len(c["crystals"]), 1)
        self.assertEqual(c["crystals"][0]["parent"], "U1")
        self.assertEqual(c["crystals"][0]["series_r"], ["R1"])

    def test_load_caps_join_cluster(self):
        pads = [P("Y1", "1", "XIN"), P("Y1", "2", "XOUT"),
                P("U1", "5", "XIN"), P("U1", "6", "XOUT"),
                P("C5", "1", "XIN", value="12p"), P("C5", "2", "GND"),
                P("C6", "1", "XOUT", value="12p"), P("C6", "2", "GND")]
        c = comprehend(pads)
        self.assertEqual(c["crystals"][0]["load_caps"], ["C5", "C6"])


class TestConverters(unittest.TestCase):
    def _buck(self):
        return [P("U2", "1", "5V", pin="VIN"), P("U2", "2", "SW"),
                P("U2", "3", "GND"), P("U2", "4", "FB"),
                P("L1", "1", "SW"), P("L1", "2", "3V3"),
                P("C10", "1", "5V", value="10u"), P("C10", "2", "GND"),
                P("C11", "1", "5V", value="22u"), P("C11", "2", "GND"),
                P("C12", "1", "3V3", value="22u"), P("C12", "2", "GND"),
                P("R5", "1", "FB"), P("R5", "2", "3V3")]

    def test_hot_loop(self):
        c = comprehend(self._buck())
        self.assertEqual(len(c["converters"]), 1)
        u = c["converters"][0]
        self.assertEqual((u["u"], u["l"], u["sw"], u["vout"]),
                         ("U2", "L1", "SW", "3V3"))
        # DETERMINISTIC bulk pick: largest farads wins (22u beats 10u)
        self.assertEqual(u["cin"], "C11")
        self.assertEqual(u["cout"], "C12")
        self.assertEqual(u["fb_nets"], ["FB"])

    def test_deterministic_tie(self):
        pads = self._buck()
        # same-value tie -> lowest refdes, every run
        for p in pads:
            if p["ref"] == "C11":
                p["value"] = "10u"
        c = comprehend(pads)
        self.assertEqual(c["converters"][0]["cin"], "C10")


if __name__ == "__main__":
    unittest.main()
