"""w109: a rail load of unknown current is a
kickoff finding (power.unknown_loads, readychecks.load_current_line)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fluxplace import power as PW  # noqa: E402


def test_unknown_load_is_named_by_the_kickoff_check(tmp_path):
    pytest.importorskip("pcbnew")
    from test_power_ladder import _br_board, CONS_BR
    from fluxplace import readychecks as KO
    p = _br_board(tmp_path, extra_pad=("U3", 14, 4, "VB"))
    assert PW.unknown_loads(p, CONS_BR, layers=2) == {"VB": ["U3"]}
    line = KO.load_current_line(p, CONS_BR)
    assert line["status"] == "WARN" and "U3" in line["detail"]
    ok = KO.load_current_line(_br_board(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path), CONS_BR)
    assert ok["status"] == "PASS"


# ---- the plane as a resistive sheet in the rail solve -------------------------------------------------------------

def _thin_trace_rail():
    segs = [{"layer": "F.Cu", "x1": 0.0, "y1": 0.0, "x2": 20.0, "y2": 0.0, "width": 0.15, "t_mm": 0.035}]
    pads = [{"ref": "U1", "x": 0.0, "y": 0.0, "layers": {"F.Cu"}, "box": None, "role": "source", "amps": None},
            {"ref": "U2", "x": 20.0, "y": 0.0, "layers": {"F.Cu"}, "box": None, "role": "load", "amps": 1.0}]
    return segs, [(0.0, 0.0, 0.3), (20.0, 0.0, 0.3)], pads


def test_trace_in_parallel_with_a_plane_carries_only_its_small_share():
    from fluxplace import powerflow as PF
    segs, vias, pads = _thin_trace_rail()
    alone = PF.solve(segs, [], pads, cap=1.0)["flow"][0]
    assert alone["amps"] == pytest.approx(1.0) and alone["mode"] == "tree"          # no plane: the tree solver, the trace carries the load
    plane = {"layer": "In1.Cu", "t_mm": 0.035, "contains": lambda x, y: True}
    both = PF.solve(segs, vias, pads, zones=[plane], cap=1.0)["flow"][0]
    # trace 0.0655 ohm vs plane ~0.65 mohm: ~1 % of the amp
    assert both["amps"] < 0.03 and both["mode"] == "mesh-solve"


def test_plane_islands_are_not_joined():
    from fluxplace import powerflow as PF
    segs, vias, pads = _thin_trace_rail()
    plane = {"layer": "In1.Cu", "t_mm": 0.035, "contains": lambda x, y: True, "island": lambda x, y: 0 if x < 10 else 1}
    both = PF.solve(segs, vias, pads, zones=[plane], cap=1.0)["flow"][0]
    assert both["amps"] == pytest.approx(1.0, rel=0.01)                              # two islands: the trace is the only path


# ---- padvias rung: via-in-pad from a narrow trunk's pad into the plane -------------------------------------------

def test_padvias_rung_is_registered_first_for_power_width():
    from fluxplace import gaps as GP
    assert GP.DISPATCH["power-width"][0] == "padvias" and GP.RUNGS["padvias"] and "padvias" in GP.SPLIT_RUNGS


def test_pad_in_the_zone_needs_no_trace_the_solve_sees_the_fill_contact():
    from fluxplace import powerflow as PF
    segs, _v, pads = _thin_trace_rail()
    for p in pads:
        p["layers"] = {"F.Cu"}
    plane = {"layer": "F.Cu", "t_mm": 0.035, "contains": lambda x, y: True}      # both pads stand in a same-net F.Cu fill
    assert PF.solve(segs, [], pads, zones=[plane], cap=1.0)["flow"][0]["amps"] < 0.03
    assert PF.solve(segs, [], pads, zones=[dict(plane, layer="In1.Cu")], cap=1.0)["flow"][0]["amps"] == pytest.approx(1.0)   # a fill on another layer: no pad contact


def _trunk_board(path):
    """4-layer board: a big source pad (L1.2) tied to a VB plane on In1 only by a 0.15 mm track to a via outside the pad; a load pad U2 with its own via."""
    import pcbnew as P
    b = P.BOARD()
    b.SetCopperLayerCount(4)
    net = P.NETINFO_ITEM(b, "VB")
    b.Add(net)

    def pad(ref, x, y, w, h):
        fp = P.FOOTPRINT(b)
        fp.SetReference(ref)
        fp.SetPosition(P.VECTOR2I(P.FromMM(x), P.FromMM(y)))
        b.Add(fp)
        pd = P.PAD(fp)
        pd.SetShape(P.PAD_SHAPE_RECTANGLE)
        pd.SetAttribute(P.PAD_ATTRIB_SMD)
        ls = P.LSET()
        ls.AddLayer(P.F_Cu)
        pd.SetLayerSet(ls)
        pd.SetSize(P.VECTOR2I(P.FromMM(w), P.FromMM(h)))
        pd.SetPosition(P.VECTOR2I(P.FromMM(x), P.FromMM(y)))
        pd.SetNumber("2")
        pd.SetNet(net)
        fp.Add(pd)

    def via(x, y):
        v = P.PCB_VIA(b)
        v.SetPosition(P.VECTOR2I(P.FromMM(x), P.FromMM(y)))
        v.SetWidth(P.FromMM(0.6))
        v.SetDrill(P.FromMM(0.3))
        v.SetNet(net)
        b.Add(v)
    pad("L1", 10, 10, 2.0, 6.0)
    pad("U2", 30, 10, 1.0, 1.0)
    via(13, 10)
    via(30, 11.5)
    for (x1, y1, x2, y2) in ((10, 10, 13, 10), (30, 10, 30, 11.5)):
        t = P.PCB_TRACK(b)
        t.SetStart(P.VECTOR2I(P.FromMM(x1), P.FromMM(y1)))
        t.SetEnd(P.VECTOR2I(P.FromMM(x2), P.FromMM(y2)))
        t.SetWidth(P.FromMM(0.15))
        t.SetLayer(P.F_Cu)
        t.SetNet(net)
        b.Add(t)
    z = P.ZONE(b)
    z.SetLayer(P.In1_Cu)
    z.SetNetCode(net.GetNetCode())
    ch = P.SHAPE_LINE_CHAIN()
    for x, y in ((0, 0), (40, 0), (40, 20), (0, 20)):
        ch.Append(P.FromMM(x), P.FromMM(y))
    ch.SetClosed(True)
    z.Outline().AddOutline(ch)
    b.Add(z)
    P.ZONE_FILLER(b).Fill(b.Zones())
    P.SaveBoard(path, b)
    return path


TRUNK_CONS = {"power": {"VB": {"max_current_ma": 3000, "max_v": 5.0, "source": "L1"}},
              "dcdrop": [{"net": "VB", "from": "L1", "to": ["U2"], "current_a": 3.0, "drop_mv_max": 100.0}]}


def test_padvias_repairs_new_trunk_copper_but_never_inherited_copper(tmp_path):
    pytest.importorskip("pcbnew")
    import pcbnew as P
    from fluxplace import drcguard as DG
    src = _trunk_board(str(tmp_path / "in.kicad_pcb"))
    nv = lambda f: sum(1 for t in P.LoadBoard(f).GetTracks() if t.GetClass() == "PCB_VIA")
    out0 = str(tmp_path / "inherited.kicad_pcb")
    r0 = PW.add_pad_vias(src, TRUNK_CONS, out0, nets={"VB"}, only_new_vs=src)           # the input already had this copper: untouched
    assert not os.path.exists(out0) and not any(v.get("added") for v in r0["pads"].values())
    out1 = str(tmp_path / "new.kicad_pcb")
    r1 = PW.add_pad_vias(src, TRUNK_CONS, out1, nets={"VB"})                               # laid by this run (no baseline): repaired
    assert os.path.exists(out1) and nv(out1) > nv(src) and sum(v["added"] for v in r1["pads"].values()) >= 1

