"""PCBWay worksheet: reading the form's answers off the board.

Every case here is a way the worksheet lied about a real board (utv-comms-bridge
V1.3) before the fix. A wrong number here is not cosmetic — it is a re-quote, a
part nobody solders, or a fabrication tier the board does not fit in.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace import pcbway as P                          # noqa: E402

# a minimal board: rounded-rect outline with a nested (stroke) block — the shape
# that broke the first parser — one QFN, one radial cap, one connector with
# mechanical board-locks, and a track/via pair
BOARD = """(kicad_pcb
\t(general
\t\t(thickness 1.6)
\t)
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(1 "In1.Cu" signal)
\t\t(2 "In2.Cu" signal)
\t\t(31 "B.Cu" signal)
\t)
\t(gr_line
\t\t(start 10 10)
\t\t(end 60 10)
\t\t(stroke
\t\t\t(width 0.1)
\t\t\t(type default)
\t\t)
\t\t(layer "Edge.Cuts")
\t)
\t(gr_line
\t\t(start 60 10)
\t\t(end 60 40)
\t\t(stroke
\t\t\t(width 0.1)
\t\t\t(type default)
\t\t)
\t\t(layer "Edge.Cuts")
\t)
\t(footprint "Texas_RHB0032E_VQFN-32-1EP_5x5mm_P0.5mm"
\t\t(layer "F.Cu")
\t\t(property "Reference" "U5")
\t\t(pad "1" smd roundrect
\t\t\t(at 0 0)
\t\t)
\t\t(pad "2" smd roundrect
\t\t\t(at 0 0.5)
\t\t)
\t\t(pad "3" smd roundrect
\t\t\t(at 0 1.0)
\t\t)
\t\t(pad "4" smd roundrect
\t\t\t(at 0 1.5)
\t\t)
\t\t(pad "5" smd roundrect
\t\t\t(at 0 2.0)
\t\t)
\t\t(pad "6" smd roundrect
\t\t\t(at 0 2.5)
\t\t)
\t\t(pad "7" smd roundrect
\t\t\t(at 0 3.0)
\t\t)
\t\t(pad "8" smd roundrect
\t\t\t(at 0 3.5)
\t\t)
\t)
\t(footprint "CP_Radial_D10.0mm_P5.00mm"
\t\t(layer "B.Cu")
\t\t(property "Reference" "C12")
\t\t(pad "1" thru_hole circle
\t\t\t(at 0 0)
\t\t\t(drill 1.0)
\t\t)
\t\t(pad "2" thru_hole circle
\t\t\t(at 5 0)
\t\t\t(drill 1.0)
\t\t)
\t)
\t(footprint "Molex_Micro-Fit_3.0_43650-0215_1x02_P3.00mm_Vertical"
\t\t(layer "F.Cu")
\t\t(property "Reference" "J3")
\t\t(pad "" np_thru_hole circle
\t\t\t(at -3 0)
\t\t\t(drill 3.0)
\t\t)
\t\t(pad "" np_thru_hole circle
\t\t\t(at 3 0)
\t\t\t(drill 3.0)
\t\t)
\t\t(pad "1" thru_hole oval
\t\t\t(at 0 0)
\t\t\t(drill 1.02)
\t\t)
\t\t(pad "2" thru_hole oval
\t\t\t(at 3 0)
\t\t\t(drill 1.02)
\t\t)
\t)
\t(segment
\t\t(start 1 1)
\t\t(end 2 2)
\t\t(width 0.09)
\t)
\t(via
\t\t(at 3 3)
\t\t(size 0.6)
\t\t(drill 0.3)
\t)
)
"""


def _board(tmpdir):
    p = os.path.join(tmpdir, "b.kicad_pcb")
    open(p, "w").write(BOARD)
    return p


class TestBoardFacts(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.f = P.board_facts(_board(self.tmp))

    def test_outline_includes_the_stroke(self):
        # the board is cut on the OUTSIDE of the outline: 50x30 of geometry with
        # a 0.1 line is the 50.1 x 30.1 the fab quotes, and the number KiCad
        # itself reports. Reporting 50.0 under-declares the panel.
        self.assertEqual(self.f["size_mm"], (50.1, 30.1))

    def test_nested_stroke_block_does_not_hide_the_layer(self):
        # a non-greedy regex stops at the stroke's closing paren, so the
        # (layer "Edge.Cuts") line never appears and the board has no outline.
        # That shipped a worksheet with a blank Size field.
        self.assertIsNotNone(self.f["size_mm"])

    def test_layers_and_thickness(self):
        self.assertEqual(self.f["layers"], 4)
        self.assertEqual(self.f["thickness_mm"], 1.6)

    def test_min_track_and_hole(self):
        self.assertAlmostEqual(self.f["min_track_mm"], 0.09)
        self.assertAlmostEqual(self.f["min_hole_mm"], 0.3)

    def test_board_locks_do_not_make_a_connector_smd(self):
        # J3 is 2 signal pins + 2 np_thru_hole mechanical locks. Counting the
        # locks as pads puts the drilled ratio at exactly 0.5 -> "SMD part",
        # and the through-hole count on the form comes up short.
        self.assertIn("J3", self.f["tht_refs"])
        self.assertIn("C12", self.f["tht_refs"])
        self.assertNotIn("U5", self.f["tht_refs"])

    def test_fine_pitch_is_found_by_geometry_not_by_name(self):
        self.assertIn("U5", self.f["fine_refs"])
        self.assertAlmostEqual(self.f["finest_pitch_mm"], 0.5)

    def test_hidden_joint_packages_drive_the_xray_count(self):
        self.assertEqual(self.f["hidden_joint_refs"], ["U5"])


class TestFormTiers(unittest.TestCase):
    def test_track_tier_covers_the_design_it_does_not_round_up(self):
        # 0.09 mm track / 0.088 mm space is 3.54 / 3.46 mil. Picking 4/4mil
        # because "it's about 4 mil" quotes a board that cannot be built.
        pick, why = P.track_space_tier(0.09, 0.088)
        self.assertEqual(pick, "3/3mil")
        self.assertIn("3.46", why)

    def test_track_tier_picks_the_cheapest_tier_that_still_fits(self):
        # a 0.2 mm / 0.2 mm board is 7.9 mil: paying for 3/3mil is money burned
        self.assertEqual(P.track_space_tier(0.2, 0.2)[0], "6/6mil")

    def test_track_tier_flags_a_design_finer_than_the_form(self):
        self.assertIn("BELOW", P.track_space_tier(0.05, 0.05)[1])

    def test_hole_tier_declares_the_smallest_drill(self):
        self.assertEqual(P.hole_tier(0.3)[0], "0.3mm")
        # 0.35 mm is not a tier: declare 0.3, the tier that covers it
        self.assertEqual(P.hole_tier(0.35)[0], "0.3mm")

    def test_thickness_must_be_an_option_on_the_form(self):
        self.assertEqual(P.thickness_pick(1.6)[0], "1.6")
        self.assertEqual(P.thickness_pick(1.55)[0], P.CHOOSE)

    def test_no_board_means_choose_not_a_guess(self):
        self.assertEqual(P.track_space_tier(None, None)[0], P.CHOOSE)
        self.assertEqual(P.hole_tier(None)[0], P.CHOOSE)


class TestWorksheet(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.facts = P.collect(board=_board(self.tmp), quantity=5, name="demo")
        self.md = P.worksheet(self.facts, zip_name="demo-fab.zip")

    def test_every_field_on_the_page_is_present(self):
        # the point of the doc is that nobody has to invent an answer at the
        # order form: a field we silently omit is a field they guess
        for field in ("3 flexible options", "Board type", "Assembly side(s)",
                      "Quantity", "Contains sensitive components/parts",
                      "substitutes made in China", "Number of Unique Parts",
                      "Number of SMD Parts", "Number of BGA/QFP Parts",
                      "Number of Through-Hole Parts", "Depanel", "Conformal",
                      "Press-fit", "Cable wire harness", "Package box",
                      "Flying Probe Testing", "Function test",
                      "Firmware loading", "Box build", "X-ray",
                      "Different design in panel", "Size (single)", "Layers",
                      "Material", "FR4-TG", "Thickness", "Min track/spacing",
                      "Min hole size", "Solder mask", "Silkscreen",
                      "UV printing", "Edge connector", "Surface finish",
                      "Immersion Gold", "Via process", "Finished copper",
                      "Remove product No.", "Impedance control"):
            self.assertIn(field, self.md, "%s missing from the worksheet" % field)

    def test_fine_pitch_board_asks_for_enig(self):
        self.assertIn("**Immersion gold (ENIG)**", self.md)

    def test_substitutions_are_refused_by_default(self):
        row = [ln for ln in self.md.splitlines() if "substitutes made in China" in ln][0]
        self.assertIn("**No**", row)

    def test_turnkey_unless_sourcing_says_otherwise(self):
        self.assertIn("**Turnkey", self.md)
        facts = dict(self.facts, consign=[("XAL7070-153MEC", "LEAD", "L1")])
        self.assertIn("**Combo", P.worksheet(facts))
        self.assertIn("XAL7070-153MEC", P.worksheet(facts))


class TestPlaceCrossCheck(unittest.TestCase):
    def test_a_part_missing_from_pos_csv_is_flagged_not_dropped(self):
        # ANT1 (a THT patch antenna) is excluded from KiCad's position file.
        # Silently trusting pos.csv means the assembler never fits it and the
        # board ships without its antenna.
        import tempfile
        tmp = tempfile.mkdtemp()
        board = _board(tmp)
        os.makedirs(os.path.join(tmp, "place"))
        with open(os.path.join(tmp, "place", "pos.csv"), "w") as fh:
            fh.write("Ref,Val,Package,PosX,PosY,Rot,Side\n")
            fh.write("U5,x,x,0,0,0,top\n")
            fh.write("C12,x,x,0,0,0,bottom\n")
        f = P.collect(board=board, fab_dir=tmp, name="demo")
        self.assertEqual(f["unplaced_refs"], ["J3"])
        self.assertEqual(f["placements"], 3)        # 2 placed + 1 counted back in
        self.assertIn("J3", f["tht_refs"])          # still a through-hole joint
        self.assertIn("NOT in `place/pos.csv`", P.worksheet(f))


if __name__ == "__main__":
    unittest.main()
