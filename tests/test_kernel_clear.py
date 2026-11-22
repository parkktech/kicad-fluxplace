"""K9: clearance engine, one boundary case per violation type: need - 1 um refuses, need and need + 1 um accept.

Fallback floors (no project): clearance 0.2, hole clearance 0.25, edge 0.5, via 0.5 / 0.3, annular 0.1 (mm).
"""
import pytest

from fluxplace import kernel as K

pytestmark = pytest.mark.skipif(not K.available(), reason="run kernel/build.sh")

HEAD4 = ('(kicad_pcb (version 20260206) (generator "pcbnew")\n'
         '(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
         '(gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts") (uuid "12121212-1212-4121-8121-121212121212"))\n')
D = [-1e-3, 0.0, 1e-3]          # mm: need - 1 um, need, need + 1 um


def _tr(x0, y0, x1, y1, net, w=0.2, uuid="t", layer="F.Cu"):
    return (f'(segment (start {x0} {y0}) (end {x1} {y1}) (width {w}) (layer "{layer}") (net "{net}") '
            f'(uuid "{uuid}"))\n')


def _via(x, y, net, size=0.6, drill=0.3, uuid="v"):
    return f'(via (at {x} {y}) (size {size}) (drill {drill}) (net "{net}") (uuid "{uuid}"))\n'


def _run(tmp_path, body, sets=(), dru=None):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(HEAD4 + body + ")\n")
    if dru is not None:
        (tmp_path / "b.kicad_dru").write_text(dru)
    with K.Kernel(str(path)) as k:
        for s in sets:
            k.call("set_floors", **s)
        return k.call("check")


def test_clearance_boundary(tmp_path):
    for d in D:
        body = _tr(10, 10, 20, 10, "N1", uuid="a") + _tr(10, 10.4 + d, 20, 10.4 + d, "N2", uuid="b")
        r = _run(tmp_path, body)
        bad = [v for v in r["violations"] if v["type"] == "clearance"]
        assert bool(bad) == (d < 0), (d, r["violations"])
        if bad:
            assert bad[0]["need"] == pytest.approx(0.2) and bad[0]["rule"] == "net class"


def test_short_and_same_net_never_violates(tmp_path):
    r = _run(tmp_path, _tr(10, 10, 20, 10, "N1", uuid="a") + _tr(10, 10.1, 20, 10.1, "N2", uuid="b"))
    assert [v["type"] for v in r["violations"]] == ["short"]
    r = _run(tmp_path, _tr(10, 10, 20, 10, "N1", uuid="a") + _tr(10, 10.1, 20, 10.1, "N1", uuid="b"))
    assert r["violations"] == [] and r["verdict"] == "accept"


def test_hole_clearance_boundary(tmp_path):
    # via hole radius 0.15, track radius 0.1, floor 0.25: centre distance 0.5 is the boundary
    for d in D:
        body = _via(15, 15, "N1", uuid="v") + _tr(10, 15.5 + d, 20, 15.5 + d, "N2", uuid="t")
        r = _run(tmp_path, body)
        assert ("hole clearance" in [v["type"] for v in r["violations"]]) == (d < 0), (d, r["violations"])


def test_hole_to_hole_boundary(tmp_path):
    # drill radius 0.15 each, floor 0.25: centre distance 0.55 is the boundary (same net: copper is exempt)
    for d in D:
        body = _via(15, 15, "N1", uuid="v1") + _via(15.55 + d, 15, "N1", uuid="v2")
        r = _run(tmp_path, body)
        assert ("hole to hole" in [v["type"] for v in r["violations"]]) == (d < 0), (d, r["violations"])


def test_edge_boundary(tmp_path):
    # the outline's left side is x = 0 (r = 0); floor 0.5; track radius 0.1: centre x = 0.6 is the boundary
    for d in D:
        body = _tr(0.6 + d, 40, 5, 40, "N1", uuid="t")
        r = _run(tmp_path, body)
        assert ("edge" in [v["type"] for v in r["violations"]]) == (d < 0), (d, r["violations"])


def test_keepout_boundary(tmp_path):
    ko = ('(zone (net 0) (net_name "") (layers "F.Cu") (uuid "66666666-6666-4666-8666-666666666666") '
          '(name "ko") (keepout (tracks not_allowed) (vias allowed) (pads allowed) (copperpour allowed) '
          '(footprints allowed)) (polygon (pts (xy 60 0) (xy 70 0) (xy 70 10) (xy 60 10))))\n')
    for d in D:
        body = ko + _tr(50, 5, 59.9 - d, 5, "N1", uuid="t")   # gap = 59.9 - end.x
        r = _run(tmp_path, body)
        assert ("keepout" in [v["type"] for v in r["violations"]]) == (d < 0), (d, r["violations"])


def test_width_via_size_drill_annular_boundaries(tmp_path):
    for d in D:
        r = _run(tmp_path, _tr(10, 10, 20, 10, "N1", w=0.15 + d, uuid="t"), sets=[{"track": 0.15}])
        assert ("width" in [v["type"] for v in r["violations"]]) == (d < 0), ("width", d)
        r = _run(tmp_path, _via(30, 30, "N1", size=0.5 + d, drill=0.3, uuid="v"))
        assert ("via size" in [v["type"] for v in r["violations"]]) == (d < 0), ("via size", d)
        r = _run(tmp_path, _via(30, 30, "N1", size=0.6, drill=0.3 + d, uuid="v"), sets=[])
        assert ("drill" in [v["type"] for v in r["violations"]]) == (d < 0), ("drill", d)
        r = _run(tmp_path, _via(30, 30, "N1", size=0.5 + 2 * d, drill=0.3, uuid="v"))
        assert ("annular" in [v["type"] for v in r["violations"]]) == (d < 0), ("annular", d)


def test_local_pad_clearance_wins_over_class(tmp_path):
    # pad half size 0.3 (top edge y = 50.3); track radius 0.1: gap = y - 50.4; local clearance 0.3 is the boundary
    pad = ('(footprint "R" (layer "F.Cu") (at 30 50 0) (property "Reference" "R1") '
           '(pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net "N1") (clearance 0.3)))\n')
    for d in D:
        r = _run(tmp_path, pad + _tr(25, 50.4 + 0.3 + d, 35, 50.4 + 0.3 + d, "N2", uuid="t"))
        cl = [v for v in r["violations"] if v["type"] == "clearance"]
        assert bool(cl) == (d < 0), (d, r["violations"])
        if cl:
            assert cl[0]["need"] == pytest.approx(0.3)


def test_dru_lowers_and_raises(tmp_path):
    dru = ('(version 1)\n(rule low (condition "A.NetName == \'LO\' || B.NetName == \'LO\'") '
           '(constraint clearance (min 0.1mm)))\n'
           '(rule high (condition "A.NetName == \'HI\'") (constraint clearance (min 0.4mm)))\n')
    # gap 0.15 between LO and N2: class 0.2 refuses, the lowering rule (0.1) accepts
    r = _run(tmp_path, _tr(10, 10, 20, 10, "LO", uuid="a") + _tr(10, 10.35, 20, 10.35, "N2", uuid="b"), dru=dru)
    assert not [v for v in r["violations"] if v["type"] == "clearance"], r["violations"]
    # gap 0.3 between HI and N2: class 0.2 accepts, the raising rule (0.4) refuses
    r = _run(tmp_path, _tr(10, 10, 20, 10, "HI", uuid="a") + _tr(10, 10.5, 20, 10.5, "N2", uuid="b"), dru=dru)
    cl = [v for v in r["violations"] if v["type"] == "clearance"]
    assert cl and cl[0]["need"] == pytest.approx(0.4) and cl[0]["rule"] == "high"


def test_need_op_reports_the_rule(tmp_path):
    path = tmp_path / "n.kicad_pcb"
    path.write_text(HEAD4 + _tr(10, 10, 20, 10, "N1", uuid="a") + _tr(10, 11, 20, 11, "N2", uuid="b") + ")\n")
    with K.Kernel(str(path)) as k:
        items = {i["uuid"]: i["id"] for i in k.call("items", kinds=["track"])["items"]}
        n = k.call("need", a=items["a"], b=items["b"], layer="F.Cu")
    assert n["mm"] == pytest.approx(0.2) and n["rule"] == "net class"
