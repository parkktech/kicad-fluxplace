"""fit-sheet: the pure planner, the constraints rewrite, the review gate, and
(where pcbnew exists) a whole-board move on a synthetic board."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from fluxplace import sheet as SH
from fluxplace import review as R
from fluxplace import boardfile as BF


def inside(bb, paper):
    w, h = SH.PAPERS[paper]
    return not SH.off_sheet(bb, w, h)


# ------------------------------------------------------------------ plan()
def test_plan_negative_coords_centred_whole_mm():
    bb = (-8.3, -16.4, 68.3, 70.3)
    paper, (dx, dy), t = SH.plan(bb, "auto", current="A4")
    assert paper == "A4" and dx == int(dx) and dy == int(dy)
    assert (dx, dy) == (118, 61)      # centre 30 -> 148.5 is a tie; round-half-even
    assert t == pytest.approx((bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy))
    assert inside(t, "A4")
    # centred: horizontally on the page, vertically above the title block
    assert abs((t[0] + t[2]) / 2 - 148.5) <= 0.5
    assert abs((t[1] + t[3]) / 2 - (15 + 161) / 2) <= 0.5


def test_plan_already_fitted_is_a_no_op_and_idempotent():
    p1, off1, t1 = SH.plan((-8.3, -16.4, 68.3, 70.3), "auto", current="A4")
    p2, off2, t2 = SH.plan(t1, "auto", current=p1)
    assert off2 == (0, 0) and t2 == pytest.approx(t1)


def test_plan_half_mm_tie_does_not_oscillate():
    bb = (0.0, 0.0, 41.0, 40.0)      # centre lands on a .5 residual
    _, off, t = SH.plan(bb, "A4")
    assert SH.plan(t, "A4")[1] == (0, 0)


def test_plan_board_too_big_for_a4_goes_to_a3():
    paper, off, t = SH.plan((0, 0, 300, 150), "auto", current="A4")
    assert paper == "A3" and inside(t, "A3")


def test_plan_auto_keeps_a_larger_current_paper_when_it_fits():
    assert SH.plan((0, 0, 100, 80), "auto", current="A3")[0] == "A3"


def test_plan_explicit_paper_steps_up_only_when_needed():
    assert SH.plan((0, 0, 100, 80), "A5")[0] == "A5"
    assert SH.plan((0, 0, 250, 120), "A5")[0] == "A4"


def test_plan_tall_board_clears_the_title_block():
    # 120 mm tall does not fit above the title block on A4 (146 mm free... 
    # use 170: forced beside it), so it must sit left of the block
    bb = (0, 0, 120, 170)
    paper, off, t = SH.plan(bb, "auto", current="A4")
    assert paper == "A4"
    w, h = SH.PAPERS["A4"]
    assert not SH.overlaps_title_block(t, w, h) and inside(t, "A4")
    assert t[2] <= w - 12 - 108 - 5 + 1e-6            # left of the keep-out
    assert abs((t[1] + t[3]) / 2 - h / 2) <= 0.5       # full-height centring


def test_plan_tall_and_wide_board_goes_up_a_size():
    paper, _, t = SH.plan((0, 0, 200, 175), "auto", current="A4")
    assert paper == "A3" and inside(t, "A3")


def test_plan_us_series_and_unknown():
    assert SH.plan((0, 0, 100, 80), "auto", current="A")[0] == "A"
    assert SH.plan((0, 0, 300, 160), "auto", current="A")[0] == "B"
    with pytest.raises(ValueError):
        SH.plan((0, 0, 10, 10), "Z9")
    with pytest.raises(ValueError):
        SH.plan((0, 0, 2000, 2000), "auto", current="A4")


def test_title_block_overlap_detection():
    w, h = SH.PAPERS["A4"]
    assert SH.overlaps_title_block((200, 170, 250, 190), w, h)
    assert not SH.overlaps_title_block((20, 20, 100, 100), w, h)
    assert SH.off_sheet((200, 170, 250, 190), w, h)          # in area, on the block
    assert SH.off_sheet((-5, 20, 50, 60), w, h)              # outside the area
    assert not SH.off_sheet((20, 20, 100, 100), w, h)
    # touching the 5 mm clearance exactly is allowed
    assert not SH.off_sheet((15, 15, 100, 100), w, h)


def test_paper_text_roundtrip(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text('(kicad_pcb (version 1)\n  (paper "A4")\n  (layers (0 "F.Cu" signal)))')
    assert SH.read_paper(str(p)) == {"paper": "A4", "w_mm": 297.0, "h_mm": 210.0,
                                     "portrait": False}
    assert SH.set_paper_text(str(p), "A3") and SH.read_paper(str(p))["paper"] == "A3"
    p.write_text('(kicad_pcb\n  (paper "A4" portrait)\n (layers))')
    assert SH.read_paper(str(p))["h_mm"] == 297.0
    SH.set_paper_text(str(p), "A3")
    assert '(paper "A3" portrait)' in p.read_text()
    p.write_text('(kicad_pcb\n  (paper "User" 500 300)\n (layers))')
    assert SH.read_paper(str(p))["w_mm"] == 500.0
    p.write_text('(kicad_pcb\n  (general)\n  (layers (0 "F.Cu" signal)))')   # no token
    assert SH.set_paper_text(str(p), "A3") and '(paper "A3")' in p.read_text()


# ------------------------------------------------------- constraints rewrite
TOML = '''# header comment
[env]
temp_max_c = 85   # keep

[[keepout]]
name = "under CM5"
rect = [5.0, 2.0, 60.0, 42.0]   # x0 y0 x1 y1
side = "B"

[[keepout]]
around = ["MK1", "MK2"]
margin_mm = 0.5

# bolt pattern
[lock]
tol_mm = 0.01
[lock.at]
MH1 = [-3.3334, -11.8734, "F"]
MK1 = [54, 8.5, "F"]   # int x stays int
MH4=[15.5,65.7]

[bom]
max_parts = 330
'''


def test_constraints_rewrite_shifts_numbers_and_keeps_format():
    new, n = SH.rewrite_constraints(TOML, 119, 61, date="2026-11-11")
    assert n == 4
    L = new.split("\n")
    assert 'MH1 = [115.6666, 49.1266, "F"]' in L
    assert 'MK1 = [173, 69.5, "F"]   # int x stays int' in L
    assert "MH4=[15.5,65.7]".replace("15.5", "134.5").replace("65.7", "126.7") in L
    assert "rect = [124.0, 63.0, 179.0, 103.0]   # x0 y0 x1 y1" in L
    assert 'around = ["MK1", "MK2"]' in L and "margin_mm = 0.5" in L      # relative: untouched
    assert "temp_max_c = 85   # keep" in L and "# header comment" in L and "# bolt pattern" in L
    assert "max_parts = 330" in L
    assert new.rstrip("\n").endswith(
        "# fit-sheet 2026-11-11: all absolute coordinates shifted by (+119, +61) mm")
    # everything outside those four rows is byte-identical
    a = [l for l in TOML.split("\n") if l.strip()]
    b = [l for l in new.split("\n") if l.strip()][:-1]
    assert sum(x != y for x, y in zip(a, b)) == 4


def test_constraints_rewrite_negative_offset_and_zero_crossing():
    new, _ = SH.rewrite_constraints("[lock.at]\nMK1 = [54, 8.5, \"F\"]\n", -54, -8.5, date="d")
    assert 'MK1 = [0, 0.0, "F"]' in new.split("\n")


def test_constraints_multiline_rect_is_refused():
    with pytest.raises(ValueError):
        SH.rewrite_constraints("[[keepout]]\nrect = [\n1,2,3,4]\n", 1, 1)


# ------------------------------------------------------------- review gate
def facts(bb, paper="A4", w=297.0, h=210.0, parts=None, **kw):
    f = {"parts": parts or {}, "outline_bbox": bb,
         "page": {"paper": paper, "w_mm": w, "h_mm": h, "portrait": False}}
    f.update(kw)
    return f


def test_check_sheet_silent_when_fitted_or_no_page():
    assert R.check_sheet(facts([110.7, 44.6, 187.3, 131.3]), {}) == []
    assert R.check_sheet({"parts": {}}, {}) == []


def test_check_sheet_warns_off_page_and_fix_is_named():
    out = R.check_sheet(facts([-8.3, -16.4, 68.3, 70.3]), {})
    assert [f["code"] for f in out] == ["SHEET_OFF_PAGE"] and out[0]["level"] == "WARN"
    assert "fluxplace fit-sheet" in out[0]["msg"] and "A4" in out[0]["msg"]
    assert "-8.3" in out[0]["msg"]


def test_check_sheet_title_block_overlap_and_enforce():
    bb = [200, 170, 250, 190]
    out = R.check_sheet(facts(bb), {"sheet": {"enforce": True}})
    assert out[0]["level"] == "FAIL" and "title block" in out[0]["msg"]
    assert R.check_sheet(facts(bb), {"sheet": {"enforce": False}})[0]["level"] == "WARN"


def test_check_sheet_falls_back_to_footprints_and_says_so():
    parts = {"R1": {"bbox": (-2, -2, 2, 2)}}
    out = R.check_sheet(facts(None, parts=parts), {})
    assert out and "footprint extents" in out[0]["msg"]


def test_run_wires_check_sheet():
    f = facts([-8.3, -16.4, 68.3, 70.3], net_pads={}, net_tracks={}, size_mm=(76, 87),
              copper_layers=4, plane_layers=[], layer_track_mm={}, stackup=[], vias=[],
              via_type_counts={}, track_geom=[], pth_pads=[], dup_bare_pads=[],
              design_min={})
    out = R.run(f, cons={})
    assert "SHEET_OFF_PAGE" in [f["code"] for f in out]


# ------------------------------------------- file backend: page + outline
BOARD = '''(kicad_pcb (version 20260206) (generator "pcbnew") (generator_version "10.0")
 (paper "%s")
 (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
 (gr_rect (start -5 -4) (end 30 20) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
)'''


@pytest.mark.parametrize("paper,exp", [
    ('A3', ("A3", 420.0, 297.0, False)), ('A4" portrait', ("A4", 210.0, 297.0, True)),
    ('User" 500 300', ("User", 500.0, 300.0, False))])
def test_boardfile_page_and_outline(tmp_path, paper, exp):
    text = BOARD % paper if '"' not in paper else BOARD.replace('(paper "%s")', f'(paper "{paper})')
    p = tmp_path / "b.kicad_pcb"
    p.write_text(text)
    f = BF.facts_from_file(str(p))
    pg = f["page"]
    assert (pg["paper"], pg["w_mm"], pg["h_mm"], pg["portrait"]) == exp
    assert f["outline_bbox"] == pytest.approx([-5.05, -4.05, 30.05, 20.05])


def test_boardfile_no_outline_gives_none(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text('(kicad_pcb (version 1) (layers (0 "F.Cu" signal)))')
    f = BF.facts_from_file(str(p))
    assert f["outline_bbox"] is None and f["page"]["paper"] == "A4"


# --------------------------------------------------------- pcbnew: fit()
def _build(pcbnew, path):
    from pcbnew import FromMM as F, VECTOR2I as V
    b = pcbnew.BOARD()
    b.SetCopperLayerCount(4)
    net = pcbnew.NETINFO_ITEM(b, "SIG")
    b.Add(net)
    for (x0, y0, x1, y1) in [(-10, -8, 40, -8), (40, -8, 40, 22), (40, 22, -10, 22),
                             (-10, 22, -10, -8)]:
        s = pcbnew.PCB_SHAPE(b)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(V(F(x0), F(y0))); s.SetEnd(V(F(x1), F(y1)))
        s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(F(0.1))
        b.Add(s)
    for i, (ref, x, y) in enumerate([("R1", 5, 5), ("R2", 20.25, 12.5)]):
        fp = pcbnew.FOOTPRINT(b)
        fp.SetReference(ref); fp.SetValue("10k")
        b.Add(fp)
        pad = pcbnew.PAD(fp)
        pad.SetNumber("1"); pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetSize(V(F(0.5), F(0.6))); pad.SetLayerSet(pcbnew.PAD.SMDMask())
        pad.SetNet(net)
        fp.Add(pad)
        fp.SetPosition(V(F(x), F(y)))
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(V(F(5), F(5))); t.SetEnd(V(F(20.25), F(12.5)))
    t.SetWidth(F(0.2)); t.SetLayer(pcbnew.F_Cu); t.SetNet(net)
    b.Add(t)
    v = pcbnew.PCB_VIA(b)
    v.SetPosition(V(F(20.25), F(12.5))); v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); v.SetNet(net)
    b.Add(v)
    z = pcbnew.ZONE(b)
    z.SetLayer(pcbnew.B_Cu); z.SetNet(net)
    o = z.Outline(); o.NewOutline()
    for x, y in [(-9, -7), (39, -7), (39, 21), (-9, 21)]:
        o.Append(F(x), F(y))
    b.Add(z)
    tx = pcbnew.PCB_TEXT(b)
    tx.SetText("hello"); tx.SetPosition(V(F(0), F(0))); tx.SetLayer(pcbnew.F_SilkS)
    b.Add(tx)
    b.GetDesignSettings().SetAuxOrigin(V(F(-10), F(-8)))
    pcbnew.SaveBoard(path, b)
    return path


def test_fit_moves_every_item_by_exactly_the_offset(tmp_path):
    pcbnew = pytest.importorskip("pcbnew")
    p = _build(pcbnew, str(tmp_path / "s.kicad_pcb"))
    b = pcbnew.LoadBoard(p)
    before = SH.snapshot(b)
    rep = SH.fit(b)
    dx, dy = rep["offset"]
    assert rep["moved"] and (dx, dy) != (0, 0) and dx == int(dx) and dy == int(dy)
    after = SH.snapshot(b)
    assert len(before) == len(after) > 10
    E = (pcbnew.FromMM(dx), pcbnew.FromMM(dy))
    for (k, a), (k2, c) in zip(before, after):
        assert k == k2
        if k[0] == "GetGridOrigin":                  # unset (0, 0) means "no origin"
            assert a == c == (0, 0)
            continue
        assert (c[0] - a[0], c[1] - a[1]) == E, k
    assert rep["counts"]["footprints"] == 2 and rep["counts"]["zones"] == 1
    # outline inside the usable area, clear of the block, still its own size
    bb, _ = SH.outline_bbox_swig(b)
    w, h = SH.PAPERS["A4"]
    assert not SH.off_sheet(bb, w, h)
    assert bb == pytest.approx(rep["bbox_after"])
    # relative positions inside the board unchanged: footprints keep their spacing
    fps = {f.GetReference(): f.GetPosition() for f in b.GetFootprints()}
    assert (fps["R2"].x - fps["R1"].x, fps["R2"].y - fps["R1"].y) == (
        pcbnew.FromMM(15.25), pcbnew.FromMM(7.5))
    # a second fit is a no-op
    assert SH.fit(b)["status"] == "already fitted"


def test_fit_file_roundtrip_changes_paper_when_needed(tmp_path):
    pcbnew = pytest.importorskip("pcbnew")
    p = _build(pcbnew, str(tmp_path / "s.kicad_pcb"))
    out = str(tmp_path / "o.kicad_pcb")
    rep = SH.fit_file(p, out=out, paper="A5")            # 50 x 30: fits A5? 
    assert os.path.exists(out)
    pg = SH.read_paper(out)
    assert pg["paper"] == rep["paper"]
    f = R._facts_swig(out)
    assert f["page"]["paper"] == rep["paper"] and not SH.off_sheet(
        f["outline_bbox"], pg["w_mm"], pg["h_mm"])
    assert R.check_sheet(f, {}) == []
    assert BF.facts_from_file(out)["outline_bbox"] == pytest.approx(f["outline_bbox"], abs=0.1)


def test_swig_and_file_backends_agree_on_the_new_facts(tmp_path):
    pcbnew = pytest.importorskip("pcbnew")
    p = _build(pcbnew, str(tmp_path / "s.kicad_pcb"))
    a, f = R._facts_swig(p), BF.facts_from_file(p)
    assert a["page"] == f["page"]
    assert a["outline_bbox"] == pytest.approx(f["outline_bbox"], abs=0.1)
    assert [x for x in a["outline_bbox"]] == pytest.approx([-10.05, -8.05, 40.05, 22.05], abs=0.01)
