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


def test_ai_act_gets_read_tools_and_a_whole_board_copper_file(tmp_path, monkeypatch):
    """k11 / lead finding (fix-1 ai rung: 7 of 10 gave up on a 4 mm window without full-board geometry): the model is called with
    read-only tools in the ai dir, where a JSON-lines dump of the whole board's copper sits."""
    from fluxplace import decider as DC
    b = str(tmp_path / "b.kicad_pcb")
    open(b, "w").write("(kicad_pcb)")
    monkeypatch.setattr(RD, "load", lambda p: {"segs": [{"net": "N", "layer": "F.Cu", "a": (1, 2), "b": (3, 4), "w": 0.15}],
                                               "vias": [{"net": "GND", "x": 5, "y": 6, "d": 0.46, "drill": 0.2}],
                                               "pads": [{"ref": "U1", "num": "1", "net": "N", "x": 1, "y": 2, "ex": 0.1, "ey": 0.2, "layers": {"F.Cu"}}]})
    seen = {}

    def call(text, cons, m, tools=None, cwd=None, timeout=None, **kw):
        seen.update(tools=tools, cwd=cwd, text=text)
        return '{"actions": [{"do": "give_up", "why": "x"}]}', ""
    monkeypatch.setattr(DC, "call_model", call)
    monkeypatch.setattr(DC, "_models", lambda cons: ["m"])
    d = str(tmp_path / "ai")
    env = DC.act({"board": b, "gap": "g"}, {}, d, "k")
    assert env and seen["tools"] == DC.TOOLS and seen["cwd"] == d
    files = [f for f in os.listdir(d) if f.startswith("board-")]
    assert files and files[0] in seen["text"]
    rows = [l for l in open(os.path.join(d, files[0])) if l.strip()]
    assert len(rows) == 3 and '"kind":"via"' in rows[1]
    seen.clear()
    DC.act({"board": b, "gap": "g2"}, {"ai": {"tools": False}}, d, "k2")
    assert seen["tools"] is None                                          # [ai] tools = false: the old text-only call


def test_ai_window_grows_with_the_run_and_long_paths_fit():
    rec = {"a": {"x": 0, "y": 0}, "b": {"x": 60, "y": 0}}
    assert GP.ai_margin(rec) == 9.0 and GP.ai_margin({"a": {"x": 0, "y": 0}, "b": {"x": 3, "y": 0}}) == 2.0
    assert GP.ai_margin({"straight_mm": 200, **rec}) == 10.0
    path = "F.Cu: " + " ".join(f"{10 + i * 0.5:.4f},{20 + (i % 2) * 0.5:.4f}" for i in range(100))
    assert 800 < len(path) <= GP.AI_PATH_CHARS
    v, why = GP._ai_action({"do": "path", "net": "BT_SMA", "path": path}, ["F.Cu", "B.Cu"])
    assert v is not None, why


def test_rip_locked_of_a_soft_protected_net_is_kept_rf_and_gated_are_refused(tmp_path):
    """k11: HPL_PRI_N is walled by locked HPL_OUT / SENSE_ADC_N (audio, SOFT): the AI may rip them (re-routed in the same
    transaction, guard re-checks); an RF or gated-pair net (HARD) stays refused."""
    import json
    act = str(tmp_path / "a.json")
    ids = ["0123abcd"]
    json.dump({"actions": [{"do": "rip_locked", "net": "AUD", "ids": ids}]}, open(act, "w"))
    b = str(tmp_path / "missing.kicad_pcb")
    a = GP.load_ai_action(act, b, protect={"AUD", "RF1"}, soft={"AUD"})
    assert not a["refused"] and a["actions"][0]["net"] == "AUD"
    json.dump({"actions": [{"do": "rip_locked", "net": "RF1", "ids": ids}]}, open(act, "w"))
    a = GP.load_ai_action(act, b, protect={"AUD", "RF1"}, soft={"AUD"})
    assert a["refused"] and "rf or gated pair" in str(a["dropped"])


def _unseal_world(tmp_path, monkeypatch, search_fn):
    b = _board(tmp_path)
    rec = {"net": "N", "a": {"x": 10.0, "y": 10.0}, "b": {"x": 40.0, "y": 40.0}}
    vias = [{"net": "SOFT", "x": 10.0, "y": 10.5, "d": 0.46, "drill": 0.2}]
    monkeypatch.setattr(RD, "load", lambda p: {"segs": [], "vias": vias})
    monkeypatch.setattr(LP, "level", lambda n, cons, names=None: LP.SOFT)
    monkeypatch.setattr(HR, "_model", lambda board, cons=None, **kw: board)
    monkeypatch.setattr(HR, "open_nets", lambda board, constraints=None, **kw: {"connections": [rec]})
    monkeypatch.setattr(GP, "_via_probe", lambda M, v, nx, ny: True)
    monkeypatch.setattr(GP, "_move_via", lambda board, out, x, y, nx, ny: open(out, "w").write("moved") and 1)
    monkeypatch.setattr(CR, "search", search_fn)
    return b


def test_unseal_stops_after_moves_that_unseal_but_find_no_path(tmp_path, monkeypatch):
    """k16 (HPL_PRI_N took 33 min over 10 legal moves): two moves that open the pocket yet find no path end the net's tries."""
    n = {"moves": 0}

    def search(HRm, board, net, i, cp, layers, avoid, width, r, M, vc):
        if open(board).read() == "moved":
            n["moves"] += 1
            yield {"found": False, "reason": "no grid path (searched 90000 nodes)"}
        else:
            yield {"found": False, "reason": "no grid path (searched 280 nodes)"}
    b = _unseal_world(tmp_path, monkeypatch, search)
    r = GP._r_unseal(b, str(tmp_path / "o.kicad_pcb"), [{"kind": "open-free", "class": "plain", "nets": ["N"]}], {"cons": {"x": 1}})
    assert n["moves"] == GP.UNSEAL_OPEN_FAILS and r["status"] == "no change" and r["stopped"] == ["N"]


def test_unseal_net_time_cap(tmp_path, monkeypatch):
    """k16: past the per-net wall-clock cap no further move is tried."""
    n = {"moves": 0}

    def search(HRm, board, net, i, cp, layers, avoid, width, r, M, vc):
        n["moves"] += open(board).read() == "moved"
        yield {"found": False, "reason": "no grid path (searched 280 nodes)"}
    b = _unseal_world(tmp_path, monkeypatch, search)
    r = GP._r_unseal(b, str(tmp_path / "o.kicad_pcb"), [{"kind": "open-free", "class": "plain", "nets": ["N"]}],
                     {"cons": {"x": 1}, "unseal_net_s": -1})
    assert n["moves"] == 0 and r["stopped"] == ["N"]
