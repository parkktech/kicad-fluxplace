"""W46: rail branches routed at each connection's D71da width before the plain step; the plain step skips rail nets."""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import railbranch as RB, krtchain as K, power as PW  # noqa: E402

CONS = {"power": {"V5": {"max_current_ma": 3000, "max_v": 5.0, "pour": True, "source": "U1"}},
        "dcdrop": [{"net": "V5", "from": "U1", "to": "U2", "current_a": 1.0}]}
R = PW.rail(CONS, "V5", layers=6)
TRK = 0.15


def test_width_per_current():
    w1 = RB.conn_width(R, 1.0, TRK)
    assert w1 == pytest.approx(PW._ceil05(PW.ipc2221_width_mm(1.0, R["rise_c"], R["oz_outer"])), abs=1e-6) and w1 > 0.15
    assert RB.conn_width(R, 0.0, TRK) == 0.15                       # a decap: the minimum
    assert RB.conn_width(R, None, TRK) == RB.conn_width(R, 3.0, TRK) > w1    # unknown load: the rail total (flagged by the caller)


def test_ladder_only_proven_necks():
    lad = RB.conn_ladder(CONS, "V5", R, 1.0, 0.5, TRK)              # 0.5 mm neck at 1 A: narrower rungs are proven
    assert lad[0][1] is None and len(lad) > 1 and all(p is not None and p <= R["rise_c"] for _w, p in lad[1:])
    assert [w for w, _p in lad] == sorted({w for w, _p in lad}, reverse=True)
    far = RB.conn_ladder(CONS, "V5", R, 1.0, 40.0, TRK)             # 40 mm run at 1 A: nothing narrower is proven
    assert [w for w, _p in far] == [lad[0][0]]


def _fake(monkeypatch, comps, routable):
    """plan() over a fake world: comps = {key: (amps, ladder)}; the router 'connects' the pads whose class it is called for when routable."""
    state = {"open": dict(comps), "calls": []}

    def plan(board, cons, nets, track, nl=None, ctx=None):
        return {"V5": {"rail": R, "main": 5, "comps": [{"pads": [k], "amps": a, "how": "t", "unknown": [], "len_mm": 0.5, "ladder": lad}
                                                       for k, (a, lad) in state["open"].items()]}}
    monkeypatch.setattr(RB, "plan", plan)
    monkeypatch.setattr(RB, "hold", lambda s, d, h: (open(d, "w").write("h"), state.setdefault("held", []).append(dict(h))))
    monkeypatch.setattr(RB, "unhold", lambda p, h: None)

    def mk(s, o, lay, widths, ns):
        return ["py", "route.py", s, o, widths, lay]

    def runner(cmd, log, cap_s=0):
        w = cmd[4]["V5"]
        held = state["held"][-1]
        state["calls"].append((w, sorted(k for k in state["open"] if k not in held)))
        open(cmd[3], "w").write("o")
        if routable(w):
            for k in [k for k in state["open"] if k not in held]:
                del state["open"][k]
        return 0, 0.1
    return state, mk, runner


def test_branches_widest_first_decap_at_minimum(tmp_path, monkeypatch):
    big = [(0.25, None)]
    cap = [(0.15, None)]
    state, mk, runner = _fake(monkeypatch, {("U2", "1"): (1.0, big), ("C1", "1"): (0.0, cap)}, lambda w: True)
    (tmp_path / "in.kicad_pcb").write_text("x")
    rec = {}
    RB.route(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "out.kicad_pcb"), CONS, ["V5"], TRK, 6, ["F.Cu", "B.Cu"], mk, runner, lambda: 100.0, rec)
    assert [c[0] for c in state["calls"]] == [0.25, 0.15]               # the 1 A load at its D71da width, the decap at the minimum
    assert state["calls"][0][1] == [("U2", "1")] and state["held"][0] == {("C1", "1"): "V5"}   # the decap is held while the wide class routes
    assert {r["pads"][0][0]: r["width"] for r in rec["V5"]["routed"]} == {"U2": 0.25, "C1": 0.15}
    assert not rec["V5"].get("open")


def test_unroutable_wide_branch_stays_open_never_signal_width(tmp_path, monkeypatch):
    state, mk, runner = _fake(monkeypatch, {("U2", "1"): (1.0, [(0.25, None)])}, lambda w: False)
    (tmp_path / "in.kicad_pcb").write_text("x")
    rec = {}
    RB.route(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "out.kicad_pcb"), CONS, ["V5"], TRK, 6, ["F.Cu", "B.Cu"], mk, runner, lambda: 100.0, rec)
    assert [c[0] for c in state["calls"]] == [0.25]                      # no 0.15 mm retry
    assert rec["V5"]["open"][0]["pads"] == [("U2", "1")] and "no proven width" in rec["V5"]["open"][0]["reason"]
    assert RB.summary(rec) == (0, 0, 1)


def test_proven_neck_down_is_taken_and_recorded(tmp_path, monkeypatch):
    lad = [(0.25, None), (0.15, 4.0)]
    state, mk, runner = _fake(monkeypatch, {("U2", "1"): (1.0, lad)}, lambda w: w < 0.2)
    (tmp_path / "in.kicad_pcb").write_text("x")
    rec = {}
    RB.route(str(tmp_path / "in.kicad_pcb"), str(tmp_path / "out.kicad_pcb"), CONS, ["V5"], TRK, 6, ["F.Cu", "B.Cu"], mk, runner, lambda: 100.0, rec)
    assert [c[0] for c in state["calls"]] == [0.25, 0.15]
    assert rec["V5"]["necked"][0]["proof_rise_c"] == 4.0 and rec["V5"]["routed"][0]["width"] == 0.15


def test_plain_step_skips_rail_nets(tmp_path, monkeypatch):
    from test_krtchain import _ctx
    c, calls, runner = _ctx(tmp_path, monkeypatch, [0, 0, 0])
    c.cons["power"] = {"X": {"max_current_ma": 500, "max_v": 3.3}}
    monkeypatch.setattr(K.RC, "open_by_net", lambda b, **k: {})
    monkeypatch.setattr(K, "_all_nets", lambda b: ["USB_DP", "USB_DM", "X", "Y"])
    monkeypatch.setattr(RB, "route", lambda s, o, *a, **k: (open(o, "w").write("y"), 0)[1])
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    plain = calls[-1]
    nets = plain[plain.index("--nets") + 1:plain.index("--power-nets")] if "--power-nets" in plain else plain[plain.index("--nets") + 1:plain.index("--layers")]
    assert nets == ["Y"]                                               # X is a rail: not a plain net
    assert "rails-branch" in [s["step"] for s in r["steps"]]


def _board(path, tracks):
    import pcbnew
    b = pcbnew.BOARD()
    ni = pcbnew.NETINFO_ITEM(b, "N")
    b.Add(ni)
    for i, (ref, x) in enumerate((("U1", 0), ("R1", 3), ("C1", 6))):
        fp = pcbnew.FOOTPRINT(b)
        fp.SetReference(ref)
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), 0))
        p = pcbnew.PAD(fp)
        p.SetNumber("1")
        p.SetShape(pcbnew.PAD_SHAPE_RECT)
        p.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.8), pcbnew.FromMM(0.8)))
        p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        p.SetLayerSet(pcbnew.LSET.AllCuMask())
        fp.Add(p)
        p.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), 0))
        p.SetNet(ni)
        b.Add(fp)
    for x1, x2 in tracks:
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x1), 0))
        t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x2), 0))
        t.SetWidth(pcbnew.FromMM(0.5))
        t.SetLayer(pcbnew.F_Cu)
        t.SetNet(ni)
        b.Add(t)
    pcbnew.SaveBoard(path, b)


def test_plan_real_board_finds_open_connections_and_widths(tmp_path):
    pytest.importorskip("pcbnew")
    p = str(tmp_path / "b.kicad_pcb")
    _board(p, [(0, 3)])                                              # U1-R1 joined; C1 open
    cons = {"power": {"N": {"max_current_ma": 2000, "max_v": 5.0, "source": "U1"}}, "dcdrop": []}
    pl = RB.plan(p, cons, ["N"], 0.15, 6)["N"]
    assert pl["main"] == 2 and [c["pads"] for c in pl["comps"]] == [[("C1", "1")]]
    assert pl["comps"][0]["amps"] == 0.0 and pl["comps"][0]["ladder"][0][0] == 0.15      # a decap branch: the minimum
    assert pl["comps"][0]["len_mm"] == pytest.approx(3.0, abs=0.1)
    # hold/unhold: the pad leaves the net for a router call and comes back
    h = str(tmp_path / "h.kicad_pcb")
    RB.hold(p, h, {("C1", "1"): "N"})
    import pcbnew
    assert {pp.GetNetname() for pp in pcbnew.LoadBoard(h).GetPads() if pp.GetParentFootprint().GetReference() == "C1"} == {"N~hold"}
    RB.unhold(h, {("C1", "1"): "N"})
    assert {pp.GetNetname() for pp in pcbnew.LoadBoard(h).GetPads()} == {"N"}
