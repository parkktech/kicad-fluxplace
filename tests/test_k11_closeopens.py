"""k11 (W16 route-9 close, 12 opens): the rung changes that closed them on the stored board, unit-tested with stubs."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import critroute as CR, gaps as GP, handroute as HR, routediag as RD, lockpolicy as LP  # noqa: E402


def _conn(net, x0=10.0, x1=30.0):
    return {"net": net, "a": {"kind": "pad", "x": x0, "y": 20.0, "layer": "F.Cu"}, "b": {"kind": "pad", "x": x1, "y": 25.0, "layer": "F.Cu"},
            "rules": {"class": "Default", "width": 0.2, "clearance": 0.2, "via_dia": 0.6, "via_drill": 0.3}}


def _board(tmp_path, name="b.kicad_pcb"):
    p = str(tmp_path / name)
    open(p, "w").write("base")
    return p


def test_search_escalates_window_then_grid_and_a_refused_lay_takes_the_next_path(tmp_path, monkeypatch):
    """W16 BT4_RF: no path in the 2 mm window (window too small); BT_SMA: node cap hit at 0.05 mm (long). BT_SW_RFC: the first
    path was refused by the lay (escape lock): the next search step's path is laid."""
    calls, lays = [], []

    def suggest(board, net, conn=None, pitch=0.05, margin=2.0, max_nodes=0, **kw):
        calls.append((pitch, margin))
        if (pitch, margin) == (0.05, 2.0):
            return {"found": False, "reason": "no grid path (searched 400 nodes)"}
        return {"found": True, "path": f"F.Cu: 10,20 {margin},25", "length_mm": 5.0}

    def lay(board, net, path, out=None, **kw):
        lays.append(path)
        if len(lays) == 1:
            return {"accepted": False, "reason": "lock blocks the last escape of U1.4"}
        open(out, "w").write("laid")
        return {"accepted": True}

    state = {"n": 1}
    monkeypatch.setattr(HR, "open_nets", lambda board, constraints=None, **kw: {"connections": [_conn("RF1")] if state["n"] else []})
    monkeypatch.setattr(HR, "_model", lambda board, cons=None, **kw: None)
    monkeypatch.setattr(HR, "suggest", suggest)
    monkeypatch.setattr(HR, "lay", lay)

    def measure(p, kicad_cli="kicad-cli"):
        if open(p).read() == "laid":
            state["n"] = 0
        return {"opens": state["n"], "shorts": 0}
    monkeypatch.setattr(GP, "measure", measure)
    monkeypatch.setattr(GP, "signal_layers", lambda b: ["F.Cu", "B.Cu"])
    r = CR.apply(_board(tmp_path), str(tmp_path / "o.kicad_pcb"), None, None, {}, None, crit=[{"net": "RF1", "class": "clock"}])
    assert calls[:3] == [(0.05, 2.0), (0.05, 6.0), (0.1, 3.0)]
    assert len(lays) == 2 and r["kept"] and "refused" not in r["lines"][0]


def test_rf_open_free_goes_to_rfroute_without_stripping_and_before_krt(tmp_path, monkeypatch):
    """krt refuses RF nets ("no plain net"); an RF-class open-free / open-gap is laid by rfroute, its copper kept."""
    assert GP.DISPATCH["open-free"].index("rfroute") < GP.DISPATCH["open-free"].index("krt")
    assert GP.DISPATCH["open-gap"].index("rfroute") < GP.DISPATCH["open-gap"].index("krt")
    seen = {}

    def apply(base, out, actions, cp, cons, attempts, crit=None):
        seen["base"], seen["crit"] = base, crit
        open(out, "w").write("x")
        return {"kept": [1], "open_nets": []}
    monkeypatch.setattr(CR, "apply", apply)
    b = _board(tmp_path)
    gaps = [{"kind": "open-free", "class": "rf", "nets": ["BT_SMA"]}, {"kind": "open-free", "class": "plain", "nets": ["X"]},
            {"kind": "open-gap", "class": "rf", "nets": ["BT_SW_RF1"]}]
    r = GP._r_rfroute(b, str(tmp_path / "o.kicad_pcb"), gaps, {"cons": {}, "cons_path": None})
    assert r["status"] == "ran" and seen["base"] == b                  # the board itself, not a stripped copy
    assert [c["net"] for c in seen["crit"]] == ["BT_SMA", "BT_SW_RF1"] and {c["class"] for c in seen["crit"]} == {"rf"}
    assert GP._r_rfroute(b, str(tmp_path / "p.kicad_pcb"), gaps[1:2], {"cons": {}})["status"] == "no rf net"


def test_pourisland_ties_every_island_of_the_net_in_one_rung(tmp_path, monkeypatch):
    from fluxplace import pourisland as P
    n = {"k": 0}

    def repair(cur, tmp, net, constraints=None):
        if n["k"] < 3:
            n["k"] += 1
            open(tmp, "w").write(f"tied {n['k']}")
    monkeypatch.setattr(P, "repair", repair)
    r = GP._r_pourisland(_board(tmp_path), str(tmp_path / "o.kicad_pcb"), [{"kind": "open-pour", "nets": ["GND"]}],
                         {"work": str(tmp_path), "cons_path": None})
    assert r["wrote"] and n["k"] == 3 and open(tmp_path / "o.kicad_pcb").read() == "tied 3"


def test_relay_strips_a_stub_only_net_and_lays_it_pad_to_pad(tmp_path, monkeypatch):
    """W16 BT3_DM: two pad stubs (1 mm); a via on the middle of a stub was refused (track_not_centered_on_via): strip, lay from the pads."""
    b = str(tmp_path / "b.kicad_pcb")
    open(b, "w").write("(kicad_pcb)")
    segs = [{"net": "LEG", "a": (0, 0), "b": (0.5, 0)}, {"net": "LEG", "a": (9, 0), "b": (9.5, 0)},
            {"net": "LONG", "a": (0, 0), "b": (20, 0)}]
    monkeypatch.setattr(RD, "load", lambda p: {"segs": segs, "vias": []})
    from fluxplace import boardwrite as BW
    stripped = {}
    monkeypatch.setattr(BW, "strip_nets", lambda text, nets: stripped.setdefault("nets", list(nets)) and (text, 2))
    monkeypatch.setattr(CR, "apply", lambda base, out, *a, crit=None: open(out, "w").write("x") and {"kept": [1], "open_nets": []})
    gaps = [{"kind": "open-free", "class": "pair", "nets": ["LEG"]}, {"kind": "open-free", "class": "plain", "nets": ["LONG"]}]
    r = GP._r_relay(b, str(tmp_path / "o.kicad_pcb"), gaps, {"cons": {}, "cons_path": None})
    assert r["status"] == "ran" and stripped["nets"] == ["LEG"]       # LONG (20 mm of copper) is not stripped
    assert GP._r_relay(b, str(tmp_path / "p.kicad_pcb"), gaps[1:], {"cons": {}})["status"] == "no stub net"


def test_unseal_moves_the_sealing_via_away_then_lays(tmp_path, monkeypatch):
    """W16 HPL_PRI_N (D71fm): the end sits between two foreign vias (search expands 280 nodes); the closer movable via is moved
    away from the end with its track ends, then the connection is laid. A via whose net is hard-locked is never moved."""
    b = _board(tmp_path)
    rec = {"net": "N", "a": {"x": 10.0, "y": 10.0}, "b": {"x": 40.0, "y": 40.0}}
    vias = [{"net": "HARD", "x": 10.3, "y": 10.0, "d": 0.46, "drill": 0.2}, {"net": "SOFT", "x": 10.0, "y": 10.5, "d": 0.46, "drill": 0.2}]
    monkeypatch.setattr(RD, "load", lambda p: {"segs": [], "vias": vias})
    monkeypatch.setattr(LP, "level", lambda n, cons, names=None: {"HARD": LP.HARD, "SOFT": LP.SOFT}.get(n, LP.FREE))
    monkeypatch.setattr(HR, "_model", lambda board, cons=None, **kw: board)
    monkeypatch.setattr(HR, "open_nets", lambda board, constraints=None, **kw: {"connections": [rec]})
    monkeypatch.setattr(GP, "_via_probe", lambda M, v, nx, ny: True)
    moved = []

    def move(board, out, x, y, nx, ny):
        moved.append(((x, y), (nx, ny)))
        open(out, "w").write("moved")
        return 3
    monkeypatch.setattr(GP, "_move_via", move)

    def search(HRm, board, net, i, cp, layers, avoid, width, r, M, vc):
        if open(board).read() == "moved":
            yield {"found": True, "path": "F.Cu: 10,10 40,40"}
        else:
            yield {"found": False, "reason": "no grid path at width 0.15 (searched 280 nodes, pitch 0.05)"}
    monkeypatch.setattr(CR, "search", search)
    monkeypatch.setattr(HR, "lay", lambda board, net, path, out=None, **kw: open(out, "w").write("laid") and {"accepted": True})
    r = GP._r_unseal(b, str(tmp_path / "o.kicad_pcb"), [{"kind": "open-free", "class": "plain", "nets": ["N"]}], {"cons": {"x": 1}})
    assert r["laid"] == ["N"] and r["moved"][0]["net"] == "SOFT"
    (fx, fy), (tx, ty) = moved[0]
    assert (fx, fy) == (10.0, 10.5) and ty > fy                           # moved away from the end, never the HARD via
    assert open(tmp_path / "o.kicad_pcb").read() == "laid"
