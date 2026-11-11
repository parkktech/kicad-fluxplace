"""boardfile.py — the no-pcbnew board reader (KiCad 11 has no SWIG module).

The first half needs nothing but the text. The last test is the contract:
where pcbnew exists, both backends must produce the same facts.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from fluxplace import boardfile as BF
from fluxplace import review as R

BOARD = '''(kicad_pcb (version 20260206) (generator "pcbnew") (generator_version "10.0")
 (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
 (gr_rect (start 0 0) (end 30 20) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
 (footprint "R_0402_1005Metric" (layer "F.Cu") (at 10 10 90)
  (property "Reference" "R1") (property "Value" "10k")
  (fp_rect (start -1 -0.5) (end 1 0.5) (stroke (width 0) (type default)) (layer "F.CrtYd"))
  (pad "1" smd roundrect (at -0.5 0 90) (size 0.5 0.6) (layers "F.Cu") (net "SIG"))
  (pad "2" smd roundrect (at 0.5 0 90) (size 0.5 0.6) (layers "F.Cu") (net ""))
  (model "${KICAD10_3DMODEL_DIR}/R_0402.step"))
 (footprint "TestPoint" (layer "B.Cu") (at 20 10)
  (property "Reference" "J1") (property "Value" "TP")
  (pad "1" thru_hole circle (at 0 0) (size 1.5 1.5) (drill 0.8) (layers "*.Cu") (net "SIG")))
 (segment (start 10 10.5) (end 20 10.5) (width 0.2) (layer "F.Cu") (net "SIG"))
 (via (at 20 10.5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "SIG"))
 (via blind (at 15 10.5) (size 0.45) (drill 0.2) (layers "F.Cu" "In1.Cu") (net "SIG"))
 (zone (net "GND") (layers "F&B.Cu") (hatch edge 0.5))
 (zone (net "GND") (layer "In1.Cu") (hatch edge 0.5))
)'''


@pytest.fixture
def facts(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(BOARD)
    return BF.facts_from_file(str(p))


def test_parse_keeps_empty_strings_and_escapes():
    assert BF.parse('(a "" (b "x\\"y") c)') == ["a", "", ["b", 'x"y'], "c"]


def test_board_basics(facts, tmp_path):
    assert facts["copper_layers"] == 3 and facts["backend"] == "file"
    assert [round(v, 2) for v in facts["size_mm"]] == [30.1, 20.1]
    assert BF.board_version(str(tmp_path / "b.kicad_pcb")) == (20260206, "10.0")


def test_footprint_pads_nets_side_and_rotation(facts):
    r1, j1 = facts["parts"]["R1"], facts["parts"]["J1"]
    assert r1["pads"] == {"1": "SIG", "2": ""} and r1["side"] == "F"
    assert j1["side"] == "B" and j1["tht"] and not r1["tht"]
    assert r1["value"] == "10k" and r1["models"][0].endswith("R_0402.step")
    # 90 deg: the courtyard's long axis is now vertical
    x0, y0, x1, y1 = r1["bbox"]
    assert round(x1 - x0, 2) == 1.0 and round(y1 - y0, 2) == 2.0
    assert sorted(facts["net_pads"]["SIG"]) == [("J1", "1"), ("R1", "1")]
    assert facts["pth_pads"] == [{"ref": "J1", "pad": "1", "drill_mm": 0.8,
                                  "dia_mm": 1.5, "x": 20.0, "y": 10.0}]


def test_tracks_vias_and_planes(facts):
    t = facts["net_tracks"]["SIG"]
    assert round(t["length"], 3) == 10.0 and t["vias"] == 2
    assert facts["via_type_counts"] == {"through": 1, "blind": 1}
    assert sorted(facts["plane_layers"]) == ["B.Cu", "F.Cu", "In1.Cu"]   # F&B.Cu expands
    assert "F.Cu" in facts["layer_track_mm"]


def test_old_format_numbered_nets(tmp_path):
    p = tmp_path / "old.kicad_pcb"
    p.write_text('(kicad_pcb (version 20240108) (layers (0 "F.Cu" signal) (31 "B.Cu" signal))'
                 ' (net 0 "") (net 5 "VBUS")'
                 ' (footprint "X" (layer "F.Cu") (at 0 0) (property "Reference" "U1")'
                 '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 5 "VBUS")))'
                 ' (segment (start 0 0) (end 3 4) (width 0.2) (layer "F.Cu") (net 5)))')
    f = BF.facts_from_file(str(p))
    assert f["parts"]["U1"]["pads"] == {"1": "VBUS"}
    assert round(f["net_tracks"]["VBUS"]["length"], 3) == 5.0


def test_review_runs_on_file_facts_without_pcbnew(facts):
    out = R.run(facts, cons={"env": {"temp_min_c": -40, "temp_max_c": 85}})
    assert isinstance(out, list)


def test_backend_selection(monkeypatch):
    monkeypatch.setenv("FLUXPLACE_BACKEND", "file")
    assert R.board_backend() == "file"
    assert R.board_backend("swig") == "swig"


def _norm(x):
    if isinstance(x, dict):
        return {k: _norm(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_norm(v) for v in x]
    return round(x, 2) if isinstance(x, float) else x


def test_file_backend_matches_swig(tmp_path):
    """The contract. Any board named in FLUXPLACE_TEST_BOARD is compared too."""
    pytest.importorskip("pcbnew")
    import pcbnew
    boards = [b for b in [os.environ.get("FLUXPLACE_TEST_BOARD")] if b and os.path.exists(b)]
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    net = pcbnew.NETINFO_ITEM(board, "SIG")
    board.Add(net)
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("R1")
    fp.SetValue("10k")
    board.Add(fp)
    for i, x in enumerate((-0.5, 0.5)):
        pad = pcbnew.PAD(fp)
        pad.SetNumber(str(i + 1))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.5), pcbnew.FromMM(0.6)))
        pad.SetLayerSet(pcbnew.PAD.SMDMask())
        pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), 0))
        if i == 0:
            pad.SetNet(net)
        fp.Add(pad)
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(10)))
    fp.SetOrientationDegrees(90)
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(10.5)))
    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(20), pcbnew.FromMM(10.5)))
    t.SetWidth(pcbnew.FromMM(0.2))
    t.SetLayer(pcbnew.F_Cu)
    t.SetNet(net)
    board.Add(t)
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(20), pcbnew.FromMM(10.5)))
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetNet(net)
    board.Add(v)
    p = str(tmp_path / "synthetic.kicad_pcb")
    pcbnew.SaveBoard(p, board)
    boards.append(p)
    for b in boards:
        a, f = R._facts_swig(b), BF.facts_from_file(b)
        assert set(a["parts"]) == set(f["parts"])
        for ref in a["parts"]:
            pa, pf = a["parts"][ref], f["parts"][ref]
            for k in pa:
                if k == "bbox":
                    assert max(abs(x - y) for x, y in zip(pa[k], pf[k])) < 0.1, (b, ref)
                else:
                    assert _norm(pa[k]) == _norm(pf[k]), (b, ref, k)
        for k in ("copper_layers", "plane_layers", "via_type_counts", "dup_bare_pads",
                  "mpn_map"):
            assert _norm(a[k]) == _norm(f[k]), (b, k)
        assert {n: sorted(v) for n, v in a["net_pads"].items()} == \
               {n: sorted(v) for n, v in f["net_pads"].items()}
        assert set(a["net_tracks"]) == set(f["net_tracks"])
        for n in a["net_tracks"]:
            assert abs(a["net_tracks"][n]["length"] - f["net_tracks"][n]["length"]) < 0.01
            assert a["net_tracks"][n]["vias"] == f["net_tracks"][n]["vias"]
        assert len(a["vias"]) == len(f["vias"]) and len(a["track_geom"]) == len(f["track_geom"])
        assert all(abs(x - y) < 0.2 for x, y in zip(a["size_mm"], f["size_mm"]))
