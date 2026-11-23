"""Channel budget: textbook sanity of the physics, path/stub splitting on a branched net, DC drop, verdict tiers."""
import math
import pytest

pcbnew = pytest.importorskip("pcbnew")
from fluxplace import channel as CH

MM = pcbnew.FromMM
# 6 layer stack, planes In1 / In4 (signals: F, In2, In3, B)
ROWS = [{"name": "F.Cu", "type": "copper", "thickness": 0.035, "epsilon_r": 4.4},
        {"name": None, "type": "prepreg", "thickness": 0.12, "epsilon_r": 4.45, "loss_tangent": 0.02},
        {"name": "In1.Cu", "type": "copper", "thickness": 0.035, "epsilon_r": 4.4},
        {"name": None, "type": "core", "thickness": 0.43, "epsilon_r": 4.6, "loss_tangent": 0.02},
        {"name": "In2.Cu", "type": "copper", "thickness": 0.035, "epsilon_r": 4.4},
        {"name": None, "type": "prepreg", "thickness": 0.175, "epsilon_r": 4.74, "loss_tangent": 0.02},
        {"name": "In3.Cu", "type": "copper", "thickness": 0.035, "epsilon_r": 4.4},
        {"name": None, "type": "core", "thickness": 0.43, "epsilon_r": 4.6, "loss_tangent": 0.02},
        {"name": "In4.Cu", "type": "copper", "thickness": 0.035, "epsilon_r": 4.4},
        {"name": None, "type": "prepreg", "thickness": 0.12, "epsilon_r": 4.45, "loss_tangent": 0.02},
        {"name": "B.Cu", "type": "copper", "thickness": 0.035, "epsilon_r": 4.4}]
PLANES = ["In1.Cu", "In4.Cu"]


class Bd:
    def __init__(self):
        self.b = pcbnew.BOARD()
        self.b.SetCopperLayerCount(6)
        self.nets = {}
        self.fp = pcbnew.FOOTPRINT(self.b)
        self.fp.SetReference("U1")
        self.b.Add(self.fp)
        self.fps = {"U1": self.fp}

    def net(self, name):
        if name not in self.nets:
            ni = pcbnew.NETINFO_ITEM(self.b, name)
            self.b.Add(ni)
            self.nets[name] = ni
        return self.nets[name]

    def pad(self, num, x, y, net, size=1.0, ref="U1"):
        if ref != "U1":
            if ref not in self.fps:
                f = pcbnew.FOOTPRINT(self.b)
                f.SetReference(ref)
                self.b.Add(f)
                self.fps[ref] = f
        fp = self.fps[ref]
        p = pcbnew.PAD(fp)
        p.SetNumber(str(num))
        p.SetSize(pcbnew.VECTOR2I(MM(size), MM(size)))
        p.SetShape(pcbnew.PAD_SHAPE_RECT)
        p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        ls = pcbnew.LSET()
        ls.AddLayer(pcbnew.F_Cu)
        p.SetLayerSet(ls)
        p.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
        p.SetNet(self.net(net))
        fp.Add(p)

    def seg(self, a, b, net, layer=pcbnew.F_Cu, w=0.15):
        t = pcbnew.PCB_TRACK(self.b)
        t.SetStart(pcbnew.VECTOR2I(MM(a[0]), MM(a[1])))
        t.SetEnd(pcbnew.VECTOR2I(MM(b[0]), MM(b[1])))
        t.SetLayer(layer)
        t.SetWidth(MM(w))
        t.SetNet(self.net(net))
        self.b.Add(t)

    def via(self, x, y, net, top=pcbnew.F_Cu, bot=pcbnew.B_Cu):
        v = pcbnew.PCB_VIA(self.b)
        v.SetPosition(pcbnew.VECTOR2I(MM(x), MM(y)))
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetLayerPair(top, bot)
        v.SetWidth(MM(0.46))
        v.SetDrill(MM(0.2))
        v.SetNet(self.net(net))
        self.b.Add(v)


# ---------------------------------------------------------------- physics vs textbook
def test_skin_depth_and_rs_copper_1ghz():
    assert CH.skin_depth_um(1.0) == pytest.approx(2.09, abs=0.02)     # Pozar/Bogatin: copper 1 GHz = 2.09 um
    rs = math.sqrt(math.pi * 1e9 * CH.MU0 / CH.SIGMA_CU)
    assert rs == pytest.approx(8.25e-3, abs=0.05e-3)                  # Rs of copper at 1 GHz, 8.25 mohm/sq


def test_roughness_limits():
    assert CH.rough_factor(1.0, 0.0) == pytest.approx(1.0)
    assert CH.rough_factor(10.0, 5.0) == pytest.approx(2.0, abs=0.05)     # Hammerstad-Jensen saturates at 2


def test_stripline_delay_matches_85ps_sqrt_er_per_inch():
    d = CH.delay_ps_mm(ROWS, PLANES, "In2.Cu", 0.1)
    er = 4.6                                                          # core Er both sides (prepreg 4.74 one side)
    assert d == pytest.approx(85 * math.sqrt(er) / 25.4, rel=0.04)    # Bogatin: 85 ps/in * sqrt(Er)


def test_dielectric_loss_fr4_bogatin_rule():
    ac, ad = CH.loss_db_mm(ROWS, PLANES, "In2.Cu", 1.0, 0.1)
    dk = (CH.delay_ps_mm(ROWS, PLANES, "In2.Cu", 0.1) * CH.IM.C_MM_PER_PS) ** 2
    assert ad == pytest.approx(2.3 * 1.0 * 0.02 * math.sqrt(dk) / 25.4, rel=0.03)    # Bogatin: dB/in = 2.3 f(GHz) Df sqrt(Dk)
    assert ac > 0 and CH.loss_db_mm(ROWS, PLANES, "In2.Cu", 4.0, 0.1)[1] == pytest.approx(4 * ad, rel=1e-6)


def test_no_plane_layer_has_no_loss():
    assert CH.loss_db_mm(ROWS, [], "F.Cu", 1.0, 0.1) is None


# ---------------------------------------------------------------- measurement
def test_branched_net_trunk_and_stub_and_pad_copper():
    d = Bd()
    d.pad(1, 0, 0, "N")
    d.pad(2, 20, 0, "N")
    d.pad(3, 10, 5, "N")
    d.seg((0, 0), (10, 0), "N")             # starts at pad 1 centre: 0.5 mm of it is inside the 1 mm pad
    d.seg((10, 0), (20, 0), "N")
    d.seg((10, 0), (10, 5), "N")            # 5 mm branch to pad 3 = stub
    d.seg((10, 5), (10, 5.01), "N")         # glue inside pad 3
    p = CH.measure_paths(d.b, ["N"], ROWS)["N"][0]
    assert p["found"] and {p["a"], p["b"]} == {"U1.1", "U1.2"}
    assert p["length_mm"] == pytest.approx(19.0, abs=0.01)           # 20 mm minus 0.5 inside each end pad
    assert p["inside_pad_mm"] == pytest.approx(1.0, abs=0.05)
    assert p["layer_mm"] == {"F.Cu": pytest.approx(19.0, abs=0.01)}
    tr = [s for s in p["stubs"] if s["kind"] == "trace"]
    assert len(tr) == 1 and tr[0]["mm"] == pytest.approx(4.5, abs=0.1)    # up to the pad-3 edge
    assert p["stub_max_mm"] == pytest.approx(4.5, abs=0.1)


def test_mode_all_returns_every_pad_pair():
    d = Bd()
    for i, x in enumerate((0, 10, 20)):
        d.pad(i + 1, x, 0, "N")
    d.seg((0, 0), (10, 0), "N")
    d.seg((10, 0), (20, 0), "N")          # a trace must END on a pad to connect to it (pass-through is not split)
    assert len(CH.measure_paths(d.b, ["N"], ROWS, mode="all")["N"]) == 3


def test_via_and_barrel_stub():
    d = Bd()
    d.pad(1, 0, 0, "N")
    d.pad(2, 10, 0, "N")
    d.seg((0, 0), (3, 0), "N")
    d.via(3, 0, "N")                        # through via F..B, signal leaves on In2 -> barrel In2..B is a stub
    d.seg((3, 0), (7, 0), "N", pcbnew.In2_Cu)
    d.via(7, 0, "N")
    d.seg((7, 0), (10, 0), "N")
    p = CH.measure_paths(d.b, ["N"], ROWS)["N"][0]
    assert p["found"] and p["vias"] == 2
    assert p["layer_mm"].get("In2.Cu") == pytest.approx(4.0, abs=0.01)
    assert not any(s["kind"] == "via" for s in p["stubs"])           # barrel stubs are pairaudit.via_stubs' (tests/test_pairskew.py), the one function
    assert p["via_mm"] > 0.8                                          # the barrels on the signal path are counted


def test_open_net_is_not_found():
    d = Bd()
    d.pad(1, 0, 0, "N")
    d.pad(2, 10, 0, "N")
    assert CH.measure_paths(d.b, ["N"], ROWS)["N"][0]["found"] is False


# ---------------------------------------------------------------- DC drop
def test_dc_drop_100mm_1mm_wide_35um():
    d = Bd()
    d.pad(1, 0, 0, "P", 0.2)
    d.pad(2, 100, 0, "P", 0.2)
    d.seg((0, 0), (100, 0), "P", w=1.0)
    p = CH.measure_paths(d.b, ["P"], ROWS)["P"][0]
    r = CH.dc_resistance_mohm(p, ROWS)
    assert r == pytest.approx(1.72e-8 * 0.1 / (1e-3 * 35e-6) * 1e3, rel=0.01)     # rho L / (w t) = 49.1 mohm
    cons = {"dcdrop": [{"net": "P", "current_a": 2.0, "drop_mv_max": 90, "drop_mv_target": 50, "source": "t"}],
            "stackup": {"layers": [dict(x) for x in ROWS if x]}}
    row = CH.evaluate(d.b, {"dcdrop": cons["dcdrop"], "stackup": {"layers": [
        {"name": r_["name"], "type": r_["type"], "thickness": r_["thickness"], "epsilon_r": r_["epsilon_r"]} for r_ in ROWS],
        "planes": PLANES}})[0]
    assert row["kind"] == "dc" and row["metrics"][0]["value"] == pytest.approx(2 * r, rel=0.01)
    assert row["verdict"] == "fail"                                   # 98 mV > 90 mV max


def test_via_barrel_resistance_small_and_positive():
    p = {"layer_sq": {}, "via_list": [{"top": "F.Cu", "bottom": "B.Cu", "drill": 0.2}]}
    r = CH.dc_resistance_mohm(p, ROWS)
    assert 1.0 < r < 10.0                                             # ~1.6 mm barrel, 20 um plating: a few mohm


# ---------------------------------------------------------------- verdict tiers + budget
def test_verdict_tiers():
    assert CH.verdict(1, 2, 5)[0] == "ok"
    assert CH.verdict(2, 2, 5)[0] == "ok"
    v, margin, over = CH.verdict(3, 1, 10)
    assert (v, margin, over) == ("over-target-pass", 7, 2)
    assert CH.verdict(10, 1, 10)[0] == "over-target-pass"
    assert CH.verdict(10.01, 1, 10)[0] == "fail"
    assert CH.verdict(4, None, None)[0] == "n/a"
    assert CH.verdict(4, None, 3)[0] == "fail"


def test_budget_fallback_from_pairs_skew_keys():
    cons = {"pairs": {"PCIE_": {"skew_mm": 0.1, "skew_max_mm": 0.5}}}
    b, lab, fam = CH.resolve_budget(cons, "PCIE_TX_P", ps_per_mm=7.0)
    assert fam is None and b["skew_ps_max"] == pytest.approx(3.5) and b["skew_ps_target"] == pytest.approx(0.7)
    assert lab["skew_ps_max"].startswith("fallback [pairs.PCIE_] skew_max_mm")
    b, lab, _ = CH.resolve_budget({"pairs": {"PCIE_": {"skew_mm": 0.1}}}, "PCIE_TX_P", 7.0)    # old single limit = max
    assert b["skew_ps_max"] == pytest.approx(0.7) and "skew_ps_target" not in b
    b, lab, fam = CH.resolve_budget({"channel": {"PCIE_": {"skew_ps_max": 9, "source": "s"}, "PCIE_TX": {"skew_ps_max": 4, "source": "t"}},
                                     "pairs": {"PCIE_": {"skew_mm": 0.1}}}, "PCIE_TX_P", 7.0)
    assert fam == "PCIE_TX" and b["skew_ps_max"] == 4 and lab["skew_ps_max"].startswith("[channel.PCIE_TX]")


def test_pair_row_line_format():
    d = Bd()
    for n, y in (("A_P", 0), ("A_N", 1)):
        d.pad(1 if n == "A_P" else 3, 0, y, n)
        d.pad(2 if n == "A_P" else 4, 50 + (3 if n == "A_N" else 0), y, n, ref="J1")
        d.seg((0, y), (50 + (3 if n == "A_N" else 0), y), n)
    cons = {"channel": {"A_": {"loss_db_max": 6.0, "loss_db_target": 1.0, "freq_ghz": 4, "skew_ps_max": 40, "skew_ps_target": 5,
                               "length_mm_max": 150, "source": "t"}},
            "stackup": {"layers": [{"name": r["name"], "type": r["type"], "thickness": r["thickness"], "epsilon_r": r["epsilon_r"]} for r in ROWS],
                        "planes": PLANES}}
    row = CH.evaluate(d.b, cons)[0]
    assert row["name"] == "A_P/N" and row["kind"] == "pair"
    s = CH.line(row)
    assert s.startswith("CHANNEL A_P/N: loss ") and "dB" in s and "skew " in s and "length 52.0/150 mm" in s
    assert row["verdict"] in ("over-target-pass", "ok", "fail")


def test_predict():
    b = {"length_mm_max": 100, "loss_db_max": 3, "freq_ghz": 4, "skew_ps_max": 7}
    r = CH.predict([(0, 0), (30, 10)], "F.Cu", b, stack=(ROWS, PLANES))
    assert r["length_mm"] == pytest.approx(40 * CH.DETOUR) and r["length_margin_mm"] == pytest.approx(100 - 46)
    assert r["loss_db"] > 0 and r["max_mismatch_mm"] > 0
    assert CH.predict([(0, 0), (200, 0)], "F.Cu", b)["verdict"] == "fail"


def test_board_stackup_supplies_er_and_df(tmp_path):
    f = tmp_path / "b.kicad_pcb"
    f.write_text('(kicad_pcb\n\t(setup\n\t\t(stackup\n\t\t\t(layer "F.Cu"\n\t\t\t\t(type "copper")\n\t\t\t\t(thickness 0.035)\n\t\t\t)\n'
                 '\t\t\t(layer "dielectric 1"\n\t\t\t\t(type "core")\n\t\t\t\t(thickness 0.2)\n\t\t\t\t(epsilon_r 3.9)\n\t\t\t\t(loss_tangent 0.011)\n\t\t\t)\n'
                 '\t\t\t(layer "B.Cu"\n\t\t\t\t(type "copper")\n\t\t\t\t(thickness 0.035)\n\t\t\t)\n\t\t)\n\t)\n)\n')
    rows = CH.board_stack_rows(str(f))
    assert [r["type"] for r in rows] == ["copper", "core", "copper"]
    assert rows[1]["epsilon_r"] == 3.9 and rows[1]["loss_tangent"] == 0.011 and rows[0]["epsilon_r"] is None


def test_three_terminal_pair_matches_orientations_and_non_pairs_excluded():
    """U1 -> J1.A6 / J1.B6 (Type-C style): P and N legs are compared per matching orientation, never across."""
    d = Bd()
    d.pad(1, 0, 0, "U_P")
    d.pad(2, 0, 1, "U_N")
    d.pad("A6", 30, 0, "U_P", ref="J1")
    d.pad("B6", 30, 4, "U_P", ref="J1")
    d.pad("A7", 30, 1, "U_N", ref="J1")
    d.pad("B7", 30, 3, "U_N", ref="J1")
    d.seg((0, 0), (30, 0), "U_P")
    d.seg((30, 0), (30, 4), "U_P")          # A6 -> B6 shorting stub (4 mm)
    d.seg((0, 1), (30, 1), "U_N")
    d.seg((30, 1), (30, 3), "U_N")          # A7 -> B7 (2 mm)
    d.pad(1, 5, 9, "U_LED", ref="J2")
    d.pad(2, 9, 9, "U_LED", ref="J2")
    cons = {"channel": {"U_": {"skew_ps_max": 40, "skew_ps_target": 1, "stub_mm_max": 3, "source": "t"}},
            "stackup": {"layers": [{"name": r["name"], "type": r["type"], "thickness": r["thickness"], "epsilon_r": r["epsilon_r"]} for r in ROWS],
                        "planes": PLANES}}
    rows = CH.evaluate(d.b, cons)
    assert [r["name"] for r in rows] == ["U_P/N"]                     # U_LED is not a pair
    m = {x["name"]: x for x in rows[0]["metrics"]}
    assert "J1.A6" in m["skew"]["detail"] and "J1.B6" in m["skew"]["detail"]
    assert m["skew"]["value"] < 40 and m["stub"]["value"] == pytest.approx(3.5, abs=0.6)    # branch beyond the used terminal


# ---------------------------------------------------------------- DC drop: filled zone copper (D71da, own math)
def _filled_zone(d, net, x0, y0, x1, y1, layer=pcbnew.F_Cu):
    z = pcbnew.ZONE(d.b)
    z.SetLayer(layer)
    z.SetNet(d.net(net))
    ps = pcbnew.SHAPE_POLY_SET()
    ps.NewOutline()
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        ps.Append(MM(x), MM(y))
    z.SetFilledPolysList(layer, ps)
    z.SetIsFilled(True)
    d.b.Add(z)


def test_dc_drop_filled_zone_conducts_and_unfilled_does_not():
    d = Bd()
    d.pad(1, 0, 0, "P", 0.2)
    d.pad(2, 100, 0, "P", 0.2)
    d.seg((0, 0), (100, 0), "P", w=0.15)
    r0 = CH.dc_resistance_mohm(CH.measure_paths(d.b, ["P"], ROWS, zones=True)["P"][0], ROWS)
    _filled_zone(d, "P", -5, -20, 105, 20)                           # 110 x 40 mm fill over both pads
    assert CH.dc_resistance_mohm(CH.measure_paths(d.b, ["P"], ROWS)["P"][0], ROWS) == pytest.approx(r0)   # zones off: unchanged
    p = CH.measure_paths(d.b, ["P"], ROWS, zones=True)["P"][0]
    r1 = CH.dc_resistance_mohm(p, ROWS)
    assert r1 < r0 / 50                                              # ~ d^2/A = 2.3 squares vs 667 squares of track
    assert p["layer_sq"]["F.Cu"] == pytest.approx(100 ** 2 / (110 * 40), rel=0.2)


def test_dc_drop_zone_that_misses_an_end_does_not_help_it():
    d = Bd()
    d.pad(1, 0, 0, "P", 0.2)
    d.pad(2, 100, 0, "P", 0.2)
    d.seg((0, 0), (100, 0), "P", w=0.15)
    r0 = CH.dc_resistance_mohm(CH.measure_paths(d.b, ["P"], ROWS, zones=True)["P"][0], ROWS)
    _filled_zone(d, "P", 40, -20, 105, 20)                           # fill stops 40 mm short of pad 1
    r1 = CH.dc_resistance_mohm(CH.measure_paths(d.b, ["P"], ROWS, zones=True)["P"][0], ROWS)
    assert r1 == pytest.approx(r0)                                   # fill not reaching a node adds nothing (track under the fill edge is not credited: conservative)
