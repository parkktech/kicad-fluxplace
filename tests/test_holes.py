"""HOLE_TO_HOLE: edge-to-edge spacing of every drilled hole, vias and pad holes alike."""
import json, os, shutil, subprocess, sys, time
import pytest
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import planboards as PB
from fluxplace import holes as HO, review as R


def via(x, y, drill, dia=0.6):
    return f'(via (at {x} {y}) (size {dia}) (drill {drill}) (layers "F.Cu" "B.Cu") (net "N"))\n'


def hole_pad(num, dx, dy, drill, typ="thru_hole"):
    return (f'(pad "{num}" {typ} circle (at {dx} {dy}) (size {drill + 0.4} {drill + 0.4}) '
            f'(drill {drill}) (layers "*.Cu" "*.Mask"))')


def _board(tmp_path, body, min_h2h=0.25):
    """Text board + a .kicad_pro whose design rules carry the hole minimum."""
    path = PB.write(tmp_path, body)
    if min_h2h is not None:
        (tmp_path / "b.kicad_pro").write_text(json.dumps(
            {"board": {"design_settings": {"rules": {"min_hole_to_hole": min_h2h}}}}))
    return path


CONS = {}  # no [fab] profile: the .kicad_pro minimum applies


def test_seeded_via_pair_caught(tmp_path):
    # centres 0.5 apart, drills 0.3: edge gap 0.2 < 0.25
    path = _board(tmp_path, via(10, 10, 0.3) + via(10.5, 10, 0.3) + via(30, 10, 0.3))
    h = [f for f in HO.check_hole_spacing(R.facts_from_board(path), CONS) if f["code"] == "HOLE_TO_HOLE"]
    assert len(h) == 1
    assert h[0]["level"] == "WARN"                   # .kicad_pro fallback minimum: advisory
    assert "board .kicad_pro min_hole_to_hole" in h[0]["msg"]
    assert "via@10.000,10.000" in h[0]["refs"] and "via@10.500,10.000" in h[0]["refs"]
    assert "0.200 mm" in h[0]["msg"] and "required 0.25 mm" in h[0]["msg"]


def test_pad_holes_plated_and_not(tmp_path):
    # two pads 0.9 apart, drills 0.6: gap 0.3 (ok); NPTH pad 0.5 from a PTH pad: gap 0.5-0.3-0.3 < 0 -> overlap
    fp_a = PB.fp("J1", 10, 10, [hole_pad("1", -0.45, 0, 0.6), hole_pad("2", 0.45, 0, 0.6)])
    fp_b = PB.fp("J2", 10, 20, [hole_pad("1", 0, 0, 0.6, typ="np_thru_hole"),
                                hole_pad("2", 0.7, 0, 0.6)])  # NPTH 0.7 from PTH "2": gap 0.1 < 0.25
    path = _board(tmp_path, fp_a + fp_b)
    h = [f for f in HO.check_hole_spacing(R.facts_from_board(path), CONS) if f["code"] == "HOLE_TO_HOLE"]
    labels = sorted(tuple(sorted(f["refs"])) for f in h)
    assert labels == [("J2.1", "J2.2")]
    assert not any("J1." in " ".join(f["refs"]) for f in h)  # 0.3 mm gap, not flagged


def test_same_pad_skipped_and_undefined_rule_is_info(tmp_path):
    path = _board(tmp_path, via(10, 10, 0.3) + via(10.5, 10, 0.3), min_h2h=None)
    out = HO.check_hole_spacing(R.facts_from_board(path), CONS)
    assert [f["code"] for f in out] == ["HOLE_TO_HOLE_UNDEFINED"]
    assert out[0]["level"] == "INFO"


def test_profile_minimum_wins_over_kicad_pro(tmp_path):
    path = _board(tmp_path, via(10, 10, 0.3) + via(10.5, 10, 0.3), min_h2h=0.1)
    # profile "proto" carries hole_to_hole 0.50: the gap 0.2 fails against it
    out = [f for f in HO.check_hole_spacing(R.facts_from_board(path), {"fab": {"profile": "proto"}})
           if f["code"] == "HOLE_TO_HOLE"]
    assert len(out) == 1 and "required 0.5 mm" in out[0]["msg"] and "fab profile proto" in out[0]["msg"]


def test_matches_kicad_drc_count(tmp_path):
    cli = shutil.which("kicad-cli") or "/usr/bin/kicad-cli"
    if not os.path.exists(cli):
        pytest.skip("kicad-cli not installed")
    body = (via(10, 10, 0.3) + via(10.5, 10, 0.3) + via(10.2, 10.6, 0.3)  # one close pair; the third is 0.33 mm clear
            + via(40, 10, 0.3) + via(45, 10, 0.3)                       # 4.7 mm apart: fine
            + PB.fp("J1", 70, 10, [hole_pad("1", -0.45, 0, 0.6), hole_pad("2", 0.45, 0, 0.6)])
            + PB.fp("J2", 70, 20, [hole_pad("1", 0, 0, 0.6, typ="np_thru_hole"), hole_pad("2", 0.7, 0, 0.6)]))
    path = _board(tmp_path, body)
    ours = [f for f in HO.check_hole_spacing(R.facts_from_board(path), CONS) if f["code"] == "HOLE_TO_HOLE"]
    rep = str(tmp_path / "drc.json")
    subprocess.run([cli, "pcb", "drc", "--format", "json", "--severity-all", "-o", rep, path],
                   capture_output=True, text=True, timeout=600)
    d = json.load(open(rep))
    kc = [v for v in d.get("violations", []) if v.get("type") == "hole_to_hole"]
    assert len(ours) == len(kc) and len(ours) >= 2, (len(ours), len(kc), [f["msg"] for f in ours])


def test_thousand_holes_fast_and_grid_correct(tmp_path):
    # 1600 vias on a 1.0 mm pitch (edge gap 0.7, clean) with one seeded close pair.
    facts = {"vias": [], "pth_pads": [], "board_path": str(tmp_path / "x.kicad_pcb")}
    (tmp_path / "x.kicad_pro").write_text(json.dumps(
        {"board": {"design_settings": {"rules": {"min_hole_to_hole": 0.25}}}}))
    for i in range(40):
        for j in range(40):
            facts["vias"].append({"x": 1.0 * i, "y": 1.0 * j, "drill_mm": 0.3, "net": "N"})
    facts["vias"].append({"x": 5.4, "y": 5.0, "drill_mm": 0.3, "net": "N"})  # seeded: 0.4 from (5,5), 0.6 from (6,5)
    t0 = time.time()
    out = [f for f in HO.check_hole_spacing(facts, {}) if f["code"] == "HOLE_TO_HOLE"]
    assert time.time() - t0 < 5.0
    assert len(out) == 1 and "via@5.400,5.000" in out[0]["refs"]


def test_fab_profile_minimum_is_fail_and_fallback_overlap_is_fail(tmp_path):
    # 0.2 mm edge gap, [fab] profile says 0.5: a FAIL because the rule is the quoted fab's
    path = _board(tmp_path, via(10, 10, 0.3) + via(10.5, 10, 0.3), min_h2h=None)
    h = [f for f in HO.check_hole_spacing(R.facts_from_board(path), {"fab": {"profile": "proto"}})
         if f["code"] == "HOLE_TO_HOLE"]
    assert h and all(f["level"] == "FAIL" for f in h), h
    # a true overlap is FAIL even when the minimum is only the .kicad_pro fallback
    (tmp_path / "ov").mkdir()
    path = _board(tmp_path / "ov", via(10, 10, 0.3) + via(10.2, 10, 0.3))
    h = [f for f in HO.check_hole_spacing(R.facts_from_board(path), CONS) if f["code"] == "HOLE_TO_HOLE"]
    assert h and h[0]["level"] == "FAIL", h


def test_stitch_grid_keeps_hole_to_hole_to_npth_mounting_hole():
    import pcbnew
    from fluxplace import planes, holes as H
    b = pcbnew.BOARD()
    ed = b.GetLayerID("Edge.Cuts")
    for x0, y0, x1, y1 in ((0, 0, 20, 0), (20, 0, 20, 20), (20, 20, 0, 20), (0, 20, 0, 0)):
        s = pcbnew.PCB_SHAPE(b); s.SetShape(pcbnew.SHAPE_T_SEGMENT); s.SetLayer(ed)
        s.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x0), pcbnew.FromMM(y0))); s.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
        b.Add(s)
    b.GetDesignSettings().m_HoleToHoleMin = 0           # only the passed fab floor gates
    b.Add(pcbnew.NETINFO_ITEM(b, "GND"))
    fp = pcbnew.FOOTPRINT(b)
    pad = pcbnew.PAD(fp)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH); pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(3.2), pcbnew.FromMM(3.2))); pad.SetDrillSize(pcbnew.VECTOR2I(pcbnew.FromMM(3.2), pcbnew.FromMM(3.2)))
    pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(11.1), pcbnew.FromMM(10)))
    fp.Add(pad); b.Add(fp)
    # grid x = 1,4,7,10,13: the point at x=13 is 1.9 mm from the hole centre (edge gap 0.15 < 0.279) outside the pad box, so only the gate refuses it
    H2H = 0.279
    n = planes.stitch(b, "GND", pitch=3.0, via_d=0.45, via_drill=0.3, keep=0.0, edge_in=1.0, hole_to_hole=H2H)
    assert n > 0 and not any(abs(v[0] - 13.0) < 0.01 and abs(v[1] - 10.0) < 0.01 for v in H.board_holes(b))
    holes = H.board_holes(b)
    npth = [h for h in holes if h[2] == 3.2][0]
    for x, y, d in holes:
        if d != 3.2:
            assert ((x - npth[0]) ** 2 + (y - npth[1]) ** 2) ** 0.5 - (3.2 + d) / 2 >= 0.279 - 1e-6
    assert not H.hole_clear([npth], 13.0, 10.0, 0.3, 0.279) and H.hole_clear([npth], 13.2, 10.0, 0.3, 0.279)
