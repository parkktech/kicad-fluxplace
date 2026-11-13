"""boardtext edits and the via-centre snap (synthetic boards, text connectivity)."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
from fluxplace import boardtext as BT, snapvias as SV, routeconn as RC, routediag as RD

RC.PY = "/nonexistent/python3"


def seg(a, b, net="N0", layer="F.Cu", w=0.15, extra=""):
    return f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) (layer "{layer}"){extra} (net "{net}"))\n'


def via(x, y, net="N0", size=0.45):
    return f'(via (at {x} {y}) (size {size}) (drill 0.2) (layers "F.Cu" "B.Cu") (net "{net}"))\n'


def mk(tmp_path, body, name="s.kicad_pcb"):
    base = G.two("RA0", 10, 8, "N0", "P0") + G.two("RB0", 30, 8, "N0", "Q0")
    return G.board(tmp_path, base + body, name=name, outline=(0, 0, 100, 100))


def test_boardtext_scan_remove_strip(tmp_path):
    p = mk(tmp_path, seg((9.5, 8), (20, 8)) + seg((20, 8), (29.5, 8), extra=" (locked yes)") + via(20, 8))
    t = open(p).read()
    items = BT.scan(t)
    assert [k[0] for _i, _j, k, _b in items] == ["s", "s", "v"] and [BT.is_locked(b) for *_x, b in items] == [False, True, False]
    kill = [k for _i, _j, k, b in items if not BT.is_locked(b)]
    t2 = BT.remove_blocks(t, kill)
    assert [k[0] for _i, _j, k, _b in BT.scan(t2)] == ["s"] and t2.count("(footprint") == 2
    t3 = BT.strip_pad_nets(t, "N0", {("RA0", "1")})
    (tmp_path / "x.kicad_pcb").write_text(t3)
    B = RD.load(str(tmp_path / "x.kicad_pcb"))
    assert sorted((q["ref"], q["num"]) for q in B["pads"] if q["net"] == "N0") == [("RA0", "1")]
    assert BT.append_blocks("(a\n)\n", ["(b)"]).count("(b)") == 1


def test_snap_moves_end_to_centre_and_keeps_connectivity(tmp_path):
    body = seg((9.5, 8), (20.1, 8.1)) + via(20, 8) + seg((20, 8), (29.5, 8), layer="B.Cu") + via(29.5, 8)
    p = mk(tmp_path, body)
    t, n, nv = SV.snap_text(open(p).read())
    assert n == 1 and nv == 1 and "(end 20 8)" in t
    q = tmp_path / "o.kicad_pcb"
    q.write_text(t)
    assert RC.text_open(RD.load(p)) == RC.text_open(RD.load(str(q)))
    assert SV.snap_text(t)[1] == 0                                       # idempotent


def test_snap_other_net_and_joint_and_zero_length(tmp_path):
    # an end inside a via of ANOTHER net stays; a joint end moves together; a stub inside one via disappears
    body = via(20, 8, "N1") + seg((9.5, 8), (20.1, 8.1)) + via(40, 8) + seg((9.5, 9), (40.1, 8.1)) + \
        seg((40.1, 8.1), (40.15, 8.0)) + seg((40.15, 8.0), (50, 8))
    t, n, nv = SV.snap_text(open(mk(tmp_path, body)).read())
    assert "(end 20.1 8.1)" in t                                          # foreign via: untouched
    assert "(end 40 8)" in t and "(start 40 8)" in t                      # the joint moved with the end
    body = via(40, 8) + seg((40.1, 8.1), (40.05, 8.0)) + seg((40.05, 8.0), (50, 8))
    t, n, nv = SV.snap_text(open(mk(tmp_path, body)).read())
    assert t.count("(segment") == 1                                       # zero-length stub removed
