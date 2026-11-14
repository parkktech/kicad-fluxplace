"""Stackup template catalog: loads, round-trips through stackup-define on a
board built by pcbnew, and its impedance numbers agree with the closed forms
in stackup.py and the geometry walk in impedance.py."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from fluxplace import stackup as ST, impedance as IM      # noqa: E402

try:
    import pcbnew
except ImportError:                                        # pragma: no cover
    pcbnew = None


class TestCatalog(unittest.TestCase):
    def test_catalog_loads_and_covers_2_4_6_8_layers(self):
        names = ST.profile_names()
        for n in ("generic-2l-1.6", "generic-4l-1.6", "generic-6l-1.6",
                  "generic-8l-1.6", "jlcpcb-4l-1.6", "jlcpcb-6l-1.6",
                  "pcbway-4l-1.6", "pcbway-6l-1.6"):
            self.assertIn(n, names)

    def test_every_template_is_well_formed(self):
        for n in ST.profile_names():
            p = ST.PROFILES[n]
            with self.subTest(template=n):
                self.assertTrue(p.get("source"), "source field required")
                self.assertIn(p.get("kind"), ("fab", "generic"))
                copper = [l["name"] for l in p["layers"] if l["type"] == "copper"]
                self.assertEqual(len(copper), len(set(copper)))
                self.assertEqual(copper[0], "F.Cu")
                self.assertEqual(copper[-1], "B.Cu")
                # copper and dielectric must alternate: 2N-1 entries for N copper
                self.assertEqual(len(p["layers"]), 2 * len(copper) - 1)
                # 1.6 nominal; the existing 2-layer profile totals 1.58 mm (kept)
                self.assertAlmostEqual(ST.total_thickness(n), 1.6, delta=0.05)
                for ref in p.get("reference_layers", []):
                    self.assertIn(ref, copper[1:-1])

    def test_generic_templates_declare_generic_source(self):
        for n in ST.profile_names():
            if ST.PROFILES[n]["kind"] == "generic":
                self.assertEqual(ST.PROFILES[n]["source"], "generic textbook values")

    def test_fab_templates_are_marked_unverified(self):
        self.assertIn("unverified", ST.PROFILES["jlcpcb-4l-1.6"]["source"])

    def test_json_file_is_the_source(self):
        with open(ST.TEMPLATE_FILE) as fh:
            self.assertEqual(set(json.load(fh)["templates"]), set(ST.profile_names()))

    def test_constraints_template_resolves_and_explicit_wins(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "c.toml")
            with open(c, "w") as fh:
                fh.write('[fab]\nstackup_template = "generic-6l-1.6"\n')
            self.assertEqual(ST.resolve_template(None, c), "generic-6l-1.6")
            self.assertEqual(ST.resolve_template("generic-4l-1.6", c), "generic-4l-1.6")
            self.assertIsNone(ST.resolve_template(None, None))
            with self.assertRaises(ValueError):
                ST.resolve_template("no-such-stackup")


@unittest.skipIf(pcbnew is None, "pcbnew not importable")
class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "t.kicad_pcb")
        pcbnew.BOARD().Save(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_apply_template_writes_stackup_and_board_reloads(self):
        res = ST.apply_to_board(self.path, "generic-6l-1.6", backup=False)
        self.assertTrue(res["changed"], res)
        self.assertTrue(ST.board_has_stackup(self.path))
        src = open(self.path).read()
        self.assertEqual(src.count('(type "copper")'), 6)
        self.assertIsNotNone(pcbnew.LoadBoard(self.path))

    def test_refuses_overwrite_without_replace_and_replaces_with_it(self):
        ST.apply_to_board(self.path, "generic-4l-1.6", backup=False)
        again = ST.apply_to_board(self.path, "generic-8l-1.6", backup=False)
        self.assertFalse(again["changed"])
        swapped = ST.apply_to_board(self.path, "generic-8l-1.6", backup=False,
                                    replace=True)
        self.assertTrue(swapped["changed"])
        self.assertTrue(swapped["replaced_existing"])
        self.assertEqual(open(self.path).read().count('(type "copper")'), 8)

    def test_every_template_round_trips(self):
        for n in ST.profile_names():
            with self.subTest(template=n):
                with open(self.path, "w") as fh:
                    fh.write("")
                pcbnew.BOARD().Save(self.path)
                self.assertTrue(ST.apply_to_board(self.path, n, backup=False)["changed"])
                self.assertTrue(ST.board_has_stackup(self.path))


class TestImpedanceConsistency(unittest.TestCase):
    def test_template_report_is_self_consistent(self):
        for n in ST.profile_names():
            r = IM.template_report(n)
            if r["se50"] is None:
                continue
            with self.subTest(template=n):
                h, er = r["h_mm"], r["er"]
                t = ST.PROFILES[n]["layers"][0]["thickness"]
                w = r["se50"]["width_mm"]
                # the same width fed straight to the closed form lands on 50 ohm
                self.assertAlmostEqual(ST.microstrip_z0(w, h, t, er), 50.0,
                                       delta=0.75)
                # and the template's own solver agrees with impedance.py
                self.assertAlmostEqual(
                    ST.solve_microstrip_width(50.0, h, t, er), w, delta=0.01)
                for key, target in (("diff90", 90.0), ("diff100", 100.0)):
                    d = r[key]
                    if d is None:
                        continue
                    z = ST.diff_microstrip_z(d["width_mm"], d["gap_mm"], h, t, er)
                    self.assertAlmostEqual(z, target, delta=target * 0.02)

    def test_two_layer_uses_bottom_plane(self):
        r = IM.template_report("generic-2l-1.6")
        self.assertEqual(r["planes"], ["B.Cu"])
        self.assertEqual(r["model"], "microstrip")

    def test_report_matches_impedance_geometry_model(self):
        r = IM.template_report("generic-4l-1.6")
        self.assertEqual(r["signal_layer"], "F.Cu")
        self.assertEqual(r["model"], "microstrip")


if __name__ == "__main__":
    unittest.main()
