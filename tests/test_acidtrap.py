"""acidtrap: acute same-net junctions (ACID_TRAP), copper necks (SLIVER), and the
DRC-guarded chamfer fix. The fix test drives the real pcbnew + kicad-cli path and
is skipped when either is missing."""
import math
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace import acidtrap as AT                        # noqa: E402


def _seg(net, x1, y1, x2, y2, layer="F.Cu"):
    return {"net": net, "layer": layer, "kind": "track",
            "x1": x1, "y1": y1, "x2": x2, "y2": y2}


def _wedge(deg, net="N1", layer="F.Cu"):
    """Two 10 mm tracks leaving (10, 10) at `deg` apart."""
    r = math.radians(deg)
    return [_seg(net, 10, 10, 20, 10, layer),
            _seg(net, 10, 10, 10 + 10 * math.cos(r), 10 + 10 * math.sin(r), layer)]


def test_seeded_acute_junction_is_caught_and_right_angled_is_not():
    j = AT.acute_junctions(_wedge(30))
    assert len(j) == 1 and j[0]["net"] == "N1" and j[0]["deg"] == pytest.approx(30, abs=0.01)
    assert (j[0]["x"], j[0]["y"]) == (10, 10)
    assert AT.acute_junctions(_wedge(90)) == []
    assert AT.acute_junctions(_wedge(59.9))                 # just under the default 60
    assert AT.acute_junctions(_wedge(60.1)) == []
    assert AT.acute_junctions(_wedge(30), min_deg=20) == []  # threshold from the profile


def test_different_nets_and_three_way_junctions_are_not_acid_traps():
    assert AT.acute_junctions([_seg("A", 10, 10, 20, 10), _seg("B", 10, 10, 18, 12)]) == []
    three = _wedge(30) + [_seg("N1", 10, 10, 19, 4)]          # three tracks at one point
    assert AT.acute_junctions(three) == []


def test_different_layers_are_not_acid_traps():
    segs = [_seg("N1", 10, 10, 20, 10, "F.Cu"), _seg("N1", 10, 10, 18, 12, "B.Cu")]
    assert AT.acute_junctions(segs) == []


def _square(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def test_neck_in_zone_fill_is_a_sliver_and_a_wide_pour_is_not():
    # two 3 mm pads joined by a 0.05 mm neck: the neck is the sliver
    neck = [(0, 0), (3, 0), (3, 1.475), (3.5, 1.475), (3.5, 0), (7, 0), (7, 3),
            (3.5, 3), (3.5, 1.525), (3, 1.525), (3, 3), (0, 3)]
    sl = AT.slivers([{"layer": "F.Cu", "net": "GND", "outline": neck, "holes": []}], 0.127)
    assert sl and all(s["net"] == "GND" for s in sl)
    assert any(abs(s["x"] - 3.25) < 0.4 for s in sl), sl
    wide = [{"layer": "F.Cu", "net": "GND", "outline": _square(0, 0, 10, 10),
             "holes": [_square(3, 3, 6, 6)]}]
    assert AT.slivers(wide, 0.127) == []


KICAD_CLI = shutil.which("kicad-cli")


@pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not installed")
def test_chamfer_fix_removes_the_acid_trap_and_keeps_drc_not_worse(tmp_path):
    import pcbnew as pn
    from fluxplace import boardfile as BF, tune as TU
    b = pn.BOARD()
    b.SetCopperLayerCount(2)
    net = pn.NETINFO_ITEM(b, "N1")
    b.Add(net)
    r = math.radians(30)
    for ex, ey in ((20, 10), (10 + 10 * math.cos(r), 10 + 10 * math.sin(r))):
        t = pn.PCB_TRACK(b)
        t.SetStart(pn.VECTOR2I(pn.FromMM(10), pn.FromMM(10)))
        t.SetEnd(pn.VECTOR2I(pn.FromMM(ex), pn.FromMM(ey)))
        t.SetWidth(pn.FromMM(0.2))
        t.SetLayer(pn.F_Cu)
        t.SetNet(net)
        b.Add(t)
    path = str(tmp_path / "acid.kicad_pcb")
    b.Save(path)
    before = AT.acute_junctions(BF.facts_from_file(path)["track_segs"])
    assert len(before) == 1
    v0, u0 = TU.drc_counts(path, KICAD_CLI)
    n = AT.fix_junctions(path, kicad_cli=KICAD_CLI, log=lambda m: None)
    assert n == 1, "fix was rejected by the DRC guard"
    after = AT.acute_junctions(BF.facts_from_file(path)["track_segs"])
    assert after == []
    v1, u1 = TU.drc_counts(path, KICAD_CLI)
    assert v1 <= v0 and u1 <= u0
    # connectivity: the chamfer keeps the wedge's two tracks joined on net N1
    segs = BF.facts_from_file(path)["track_segs"]
    assert len(segs) == 3 and all(s["net"] == "N1" for s in segs)


def test_sliver_is_off_by_default_and_says_not_evaluated_on_file_backend():
    from fluxplace import review as R
    poly = [{"layer": "F.Cu", "net": "GND", "outline": _square(0, 0, 10, 10), "holes": []}]
    on = {"review": {"slivers": True}}
    assert R.check_slivers({"zone_polys": poly}, None) == []          # off by default
    assert R.check_slivers({"zone_polys": poly}, on) == []            # wide pour: clean
    info = R.check_slivers({"track_segs": []}, on)                    # file backend
    assert [f["level"] for f in info] == [R.INFO] and "not evaluated" in info[0]["msg"]
