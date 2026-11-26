"""W40: tree-flow current solve of a rail (powerflow.solve) and the overlay rule of power.check."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import power as PW  # noqa: E402
from fluxplace import powerflow as PF  # noqa: E402


def seg(x1, y1, x2, y2, w=0.2, layer="F.Cu"):
    return {"layer": layer, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "width": w, "len": ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5}


def pad(ref, x, y, role="load", amps=0.0):
    return {"ref": ref, "x": x, "y": y, "layers": {"F.Cu"}, "box": (x - 0.3, y - 0.3, x + 0.3, y + 0.3), "role": role, "amps": amps}


def test_branched_rail_tree_flow_is_the_sum_of_the_loads_behind_each_branch():
    #  S --(0)-- J --(1)-- A (0.5 A)      J is the junction at x=10; the trunk carries A + B
    #                 \--(2)-- B (0.3 A)
    segs = [seg(0, 0, 10, 0), seg(10, 0, 20, 0), seg(10, 0, 10, 10)]
    pads = [pad("S", 0, 0, "source", None), pad("A", 20, 0, amps=0.5), pad("B", 10, 10, amps=0.3)]
    r = PF.solve(segs, [], pads)
    f = r["flow"]
    assert [round(x["amps"], 6) for x in f] == [0.8, 0.5, 0.3]
    assert all(x["mode"] == "tree" for x in f)
    assert f[0]["loads"] == 2 and f[1]["loads"] == 1


def test_t_junction_splits_the_segment_where_a_branch_lands_on_its_side():
    # the trunk is one long segment; the branch ends on its middle: the far half carries only what lies behind it
    segs = [seg(0, 0, 20, 0), seg(10, 0, 10, 8)]
    pads = [pad("S", 0, 0, "source", None), pad("A", 20, 0, amps=0.2), pad("B", 10, 8, amps=0.4)]
    f = PF.solve(segs, [], pads)["flow"]
    assert round(f[0]["amps"], 6) == 0.6          # worst piece of the trunk (near the source)
    assert round(f[1]["amps"], 6) == 0.4


def test_dangling_stub_carries_nothing():
    segs = [seg(0, 0, 10, 0), seg(5, 0, 5, 3)]
    pads = [pad("S", 0, 0, "source", None), pad("A", 10, 0, amps=0.5)]
    f = PF.solve(segs, [], pads)["flow"]
    assert f[1]["amps"] == 0 and f[1]["mode"] == "dangling"
    assert round(f[0]["amps"], 6) == 0.5


def test_mesh_is_bounded_by_what_it_feeds_and_the_loop_is_solved():
    # a square loop S-a-L-b-S; L draws 1 A; two equal paths: each carries 0.5 A, the bound for each is 1 A
    segs = [seg(0, 0, 10, 0), seg(10, 0, 10, 10), seg(0, 0, 0, 10), seg(0, 10, 10, 10)]
    pads = [pad("S", 0, 0, "source", None), pad("L", 10, 10, amps=1.0)]
    f = PF.solve(segs, [], pads)["flow"]
    assert all(x["mode"] == "mesh-solve" for x in f)
    assert all(abs(x["amps"] - 0.5) < 1e-3 for x in f)
    # a thin path in parallel with a wide one takes less than the whole load
    segs[0] = seg(0, 0, 10, 0, w=0.1)
    f = PF.solve(segs, [], pads)["flow"]
    assert f[0]["amps"] < 0.5 < f[2]["amps"] <= 1.0


def test_mesh_bound_when_a_load_is_unknown_and_unknown_stays_on_its_own_branch():
    segs = [seg(0, 0, 10, 0), seg(10, 0, 20, 0), seg(10, 0, 10, 10)]
    pads = [pad("S", 0, 0, "source", None), pad("A", 20, 0, amps=0.5), pad("U", 10, 10, amps=None)]
    f = PF.solve(segs, [], pads)["flow"]
    assert f[0]["amps"] is None and f[0]["unk"] == ["U"]       # the trunk is upstream of U: unknown
    assert f[2]["amps"] is None and f[2]["unk"] == ["U"]
    assert f[1]["amps"] == 0.5 and f[1]["unk"] == []           # the branch to A is not
    # loop with an unknown load: not solved, bounded by the whole loop (unknown again)
    loop = [seg(0, 0, 10, 0), seg(10, 0, 10, 10), seg(0, 0, 0, 10), seg(0, 10, 10, 10)]
    g = PF.solve(loop, [], [pad("S", 0, 0, "source", None), pad("U", 10, 10, amps=None)])["flow"]
    assert all(x["amps"] is None and x["unk"] == ["U"] for x in g)


def test_cap_by_the_source_total_and_open_copper_uses_its_own_loads():
    segs = [seg(0, 0, 10, 0), seg(30, 0, 40, 0)]
    pads = [pad("S", 0, 0, "source", None), pad("A", 10, 0, amps=3.0), pad("B", 40, 0, amps=1.0)]
    f = PF.solve(segs, [], pads, cap=2.0)["flow"]
    assert f[0]["amps"] == 2.0
    assert f[1]["mode"] == "unreached" and f[1]["amps"] == 1.0
    f = PF.solve(segs, [], [pad("A", 10, 0, amps=3.0), pad("B", 40, 0, amps=1.0)])["flow"]
    assert [x["mode"] for x in f] == ["nosource", "nosource"]


def test_via_joins_layers_and_pour_is_one_node():
    segs = [seg(0, 0, 10, 0), seg(10, 0, 20, 0, layer="B.Cu")]
    pads = [pad("S", 0, 0, "source", None), dict(pad("A", 20, 0, amps=0.4), layers={"B.Cu"})]
    f = PF.solve(segs, [(10, 0, 0.3)], pads)["flow"]
    assert [round(x["amps"], 6) for x in f] == [0.4, 0.4]
    zones = [{"layer": "F.Cu", "contains": lambda x, y: 4 <= x <= 12 and -2 <= y <= 2}]
    segs = [seg(0, 0, 5, 0), seg(11, 0, 20, 0)]                    # both ends inside the pour: joined through it
    f = PF.solve(segs, [], [pad("S", 0, 0, "source", None), pad("A", 20, 0, amps=0.2)], zones)["flow"]
    assert [round(x["amps"], 6) for x in f] == [0.2, 0.2]


def test_overlaid_narrow_piece_is_not_counted_twice():
    rows = [{"layer": "F.Cu", "width": 0.127, "x1": 0, "y1": 0, "x2": 5, "y2": 0},
            {"layer": "F.Cu", "width": 0.2, "x1": 0, "y1": 0, "x2": 5, "y2": 0},
            {"layer": "B.Cu", "width": 0.127, "x1": 0, "y1": 0, "x2": 5, "y2": 0}]
    assert PW._overlaid(rows[0], rows) is True          # a wider unlocked piece lies over the locked one
    assert PW._overlaid(rows[1], rows) is False         # the widest is the one that counts
    assert PW._overlaid(rows[2], rows) is False         # other layer


def test_pad_amps_sources_and_unknowns():
    p = lambda ref, val="", pin="1": (ref, "1", None, val, pin)            # noqa: E731
    rows = [{"net": "N", "to": ["D9"], "current_a": 0.0034}]
    assert PW._pad_amps(p("C1"), {"C1": 1}, rows, "U1", 1.0, 3.3)[0] == 0.0
    assert PW._pad_amps(p("R1", "1k"), {"R1": 2}, rows, "U1", 1.0, 3.3)[0] == 3.3 / 1000
    assert PW._pad_amps(p("D9"), {"D9": 1}, rows, "U1", 1.0, 3.3)[0] == 0.0034
    assert PW._pad_amps(p("D8"), {"D8": 1}, rows, "U1", 1.0, 3.3)[0] is None
    assert PW._pad_amps(p("R2", "0R"), {"R2": 2}, rows, "U1", 1.0, 3.3)[0] is None


# ---- a rung widens its own rail only, and never makes its own measure worse ---------------------------------------
def _measure(board, cons):
    from fluxplace import boardfile
    out = PW.check(boardfile.facts_from_file(board), cons, {}, board=board)
    return {f["refs"][0]: round(f["value"]) for f in out if f["code"] == "POWER_WIDTH"}


def test_rung_touches_only_its_nets_and_a_widened_locked_track_does_not_raise_its_measure(tmp_path):
    import pytest
    pytest.importorskip("pcbnew")
    import pcbnew
    from test_routerules_syn import _board
    cons = {"power": {"VA": {"max_current_ma": 2000, "source": "U1"}, "VB": {"max_current_ma": 2000, "source": "U3"}}}
    src = _board(str(tmp_path / "in.kicad_pcb"), tracks=[(2, 6, 18, 6, "VA"), (2, 26, 18, 26, "VB")],
                 pads=[("U1", 2, 6, "VA"), ("U2", 18, 6, "VA"), ("U3", 2, 26, "VB"), ("U4", 18, 26, "VB")])
    b = pcbnew.LoadBoard(src)
    for t in b.GetTracks():
        t.SetLocked(True)
    pcbnew.SaveBoard(src, b)
    base = _measure(src, cons)
    assert set(base) == {"VA", "VB"} and base["VA"] == base["VB"]
    out = str(tmp_path / "out.kicad_pcb")
    PW.widen(src, cons, out, nets=["VA"])
    b = pcbnew.LoadBoard(out)
    vb = sorted(pcbnew.ToMM(t.GetWidth()) for t in b.GetTracks() if t.GetNetname() == "VB")
    assert vb == [0.15]                                   # the other rail is untouched
    now = _measure(out, cons)
    assert now["VB"] == base["VB"]                       # ... and its measure is the same
    assert now.get("VA", 0) <= base["VA"]                # the overlay (still narrow or not) is not counted on top of the locked track
