"""Breakout: escape templates derived from footprint geometry, kept free by placement, gated on the whole board, laid as copper."""
import json
import math
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import pcbgen as G
from fluxplace import breakout as BO
from fluxplace import escreserve as ES
from fluxplace import floorplan2 as F2
from fluxplace import modules as MD
from fluxplace import routediag as RD

PR = {"track": 0.127, "clr": 0.127, "via": 0.45, "drill": 0.2}


def conn(ref, x, y, n=20, pitch=0.4, **kw):
    """two-row board-to-board connector: 0.7 x 0.2 pads, columns at x = -1.5 / +1.5, nets A<i> / B<i>."""
    pads = []
    for i in range(n):
        pads.append(G.pad(f"{i + 1}", -1.5, (i - (n - 1) / 2) * pitch, f"A{i}", 0.7, 0.2))
        pads.append(G.pad(f"{n + i + 1}", 1.5, (i - (n - 1) / 2) * pitch, f"B{i}", 0.7, 0.2))
    return G.fp(ref, x, y, pads, court=(-2.2, -(n * pitch / 2 + 0.4), 2.2, n * pitch / 2 + 0.4), **kw)


def qfn(ref, x, y, per=8, pitch=0.5):
    """square QFN: `per` pads a side (0.25 x 0.8), a 3.2 x 3.2 thermal pad on GND."""
    half = (per - 1) * pitch / 2
    pads = [G.pad("EP", 0, 0, "GND", 3.2, 3.2)]
    for i in range(per):
        o = (i - (per - 1) / 2) * pitch
        pads += [G.pad(f"{i + 1}", -2.4, o, f"L{i}", 0.8, 0.25), G.pad(f"{per + i + 1}", o, 2.4, f"M{i}", 0.25, 0.8),
                 G.pad(f"{2 * per + i + 1}", 2.4, o, f"R{i}", 0.8, 0.25), G.pad(f"{3 * per + i + 1}", o, -2.4, f"T{i}", 0.25, 0.8)]
    return G.fp(ref, x, y, pads, court=(-3.2, -3.2, 3.2, 3.2))


def wall(ref="C1", x=46.5, y=40.25):
    """a single 0.5 mm pad standing 0.7 mm in front of a left-side pin of qfn("U1", 50, 40): the lanes of a pin row that has no other way out."""
    return G.fp(ref, x, y, [G.pad("1", 0, 0, "X", 0.5, 0.5), G.pad("2", 0, 2.0, "Y", 0.5, 0.5)], court=(-0.4, -0.4, 0.4, 2.4))


def load(tmp, body, **kw):
    return RD.load(G.board(tmp, body, **kw))


def test_two_row_fine_pitch_connector_has_a_template_for_every_pin(tmp_path):
    B = load(tmp_path, conn("J1", 50, 40))
    res = BO.solve(B, {}, None)
    assert res["dense"] == ["J1"] and len(res["pins"]) == 40 and not res["failed"]
    t = res["templates"]["J1"]
    assert sorted(s["k"] for s in t["sides"]) == [0, 2]
    d, c = PR["via"], PR["clr"]
    for s in t["sides"]:
        us = [-(y) for y in sorted(p["pad"][1] for p in t["pins"] if p["side"] == s["k"])]
        us = sorted(us)
        wl = BO._spread(us, d / 2 + c + PR["track"] / 2 + 0.003 + 0.004)
        gap = min(b - a for a, b in zip(wl, wl[1:]))
        m = math.ceil((d + c) / gap - 1e-6)
        drow = math.sqrt((d + c) ** 2 - gap ** 2) + 0.02
        jog = max(abs(a - b) for a, b in zip(wl, us))
        depth = max(d / 2 + c, 0.15 + jog + d / 2 + c) + (m - 1) * drow + d / 2 + c
        assert s["rows"] == m == 2 and abs(s["depth"] - depth) < 2e-3
    # every via keeps its clearance to every other via and to every lane of another net
    vias = [(p["via"], p["net"]) for p in t["pins"] if p.get("via")]
    assert len(vias) == 40
    for i, (a, na) in enumerate(vias):
        for b, nb in vias[i + 1:]:
            assert math.hypot(a[0] - b[0], a[1] - b[1]) >= d + c - 1e-6


def test_template_is_never_below_the_floors(tmp_path):
    B = load(tmp_path, conn("J1", 50, 40))
    pr = BO.params({"fab": {"min_via_dia_mm": 0.5, "min_clearance_mm": 0.15}}, B)
    assert pr["via"] >= 0.5 and pr["clr"] >= 0.15
    res = BO.solve(B, {"fab": {"min_via_dia_mm": 0.5, "min_clearance_mm": 0.15}}, None)
    for p in res["pins"].values():
        assert p["width"] >= pr["track"]
    assert all(BO.params({}, B)[k] >= PR[k] - 1e-9 for k in PR)


def test_part_inside_the_breakout_is_illegal_and_the_legaliser_moves_it(tmp_path):
    cap = dict(court=(-0.9, -0.45, 0.9, 0.45))
    B = load(tmp_path, conn("J1", 50, 40, locked=True) + G.two("C1", 52.6, 40, "X", "Y", **cap))
    rsv = ES.for_legalise(B, {}, None, movable={"C1"})
    assert ("C1", "HARD first escape row of J1") in ES.crowding(B, rsv)
    hard = [r for r in rsv["rects"] if r["hard"] and r["owner"] == "J1"]
    assert any(r["rect"][2] - r["rect"][0] >= 1.4 for r in hard)             # the template's depth (> 1.4 mm), not a fixed 1 row


def _plan(tmp_path, body, cons):
    B = RD.load(G.board(tmp_path, body))
    M = MD.build(B, cons, None, arrange=False)
    P = F2.Plan(B, cons, None, M, log=lambda *a, **k: None)
    bcl, regs = F2.breakout_claims(B, cons, None, M, P)
    P.set_claims(bcl)
    P.set_breakouts(regs)
    P.freeze_baseline()
    return B, M, P


def _ic(ref, x, y):
    return G.fp(ref, x, y, [G.pad(str(k + 1), 0, k * 1.0, f"Q{k}", 0.6, 0.6) for k in range(6)], court=(-1, -1, 1, 6))


def test_placement_refuses_another_part_in_the_breakout(tmp_path):
    cons = {"placement": {"fixed": ["J1"]}}
    B, M, P = _plan(tmp_path, conn("J1", 50, 40, locked=True) + _ic("C1", 52.6, 37.5), cons)
    i = P.idx[M["of"]["C1"]]
    assert P.claims_ok(i) is False                      # its pad sits in the lane region of J1's right side
    ok, _ = P.try_pose(i, 60.0, 40.0, 0)
    assert ok and P.claims_ok(i)
    assert not P.try_pose(i, 52.6, 40.0, 0)[0]


def test_two_dense_parts_are_not_placed_breakout_to_breakout(tmp_path):
    cons = {"placement": {"fixed": ["J1"]}}
    body = conn("J1", 30, 40, locked=True) + conn("J2", 35.5, 40)
    B, M, P = _plan(tmp_path, body, cons)
    i = P.idx[M["of"]["J2"]]
    assert not any(P.L.issues().get(k) for k in ("overlap", "pad_short"))      # no courtyard / pad conflict: only the breakouts meet
    assert P.claims_ok(i) is False
    P.set_breakouts([])
    P.set_claims({})
    assert P.claims_ok(i) is True


def test_placer_prefers_a_pose_whose_breakout_faces_open_board(tmp_path):
    cons = {"placement": {"fixed": ["U9"]}}
    body = conn("J1", 3.2, 20) + G.fp("U9", 60, 20, [G.pad(str(k + 1), 0, k * 0.8, f"A{k}", 0.5, 0.5) for k in range(8)], court=(-1, -1, 1, 7), locked=True)
    B, M, P = _plan(tmp_path, body, cons)
    i = P.idx[M["of"]["J1"]]
    def put(x, y, th):
        P.X[i], P.Y[i], P.T[i] = x, y, th
        P.apply(i, x, y, th)
        return P.breakout_pen()
    p_edge = put(3.2, 20.0, 0)
    assert p_edge > 0.4                                  # the left breakout runs into the board edge
    p_open = put(20.0, 20.0, 0)
    assert p_open < 0.05                                 # open board on both sides
    p_turn = put(8.0, 20.0, 90)                          # turned: the breakouts face up and down, open board
    assert p_turn < 0.05 < p_edge
    assert P.cost_parts()["breakout"] == P.bo_w * p_turn and P.total(P.cost_parts()) > 0
    put(3.2, 20.0, 0)
    cost_edge = P.total(P.cost_parts())
    put(8.0, 20.0, 90)
    assert P.total(P.cost_parts()) < cost_edge + 3.0 * P.bo_w     # the preference is in the objective the annealer minimises


def test_whole_board_gate_catches_a_pin_the_isolated_solve_passes(tmp_path):
    B = load(tmp_path, qfn("U1", 50, 40) + wall())
    iso = BO.solve(B, {}, None, others=False)
    assert not iso["failed"] and len(iso["pins"]) == 32
    ok, res, lines = BO.gate(B, {}, None)
    assert not ok and res["failed"]
    assert all(f["ref"] == "U1" for f in res["failed"])
    assert any("C1." in f["blocker"] for f in res["failed"])
    assert any(l.startswith("BREAKOUT_PIN_BLOCKED U1.") and "short by" in l for l in lines)
    # a named-pin waiver passes the gate and says so
    names = [f"U1.{f['pin']}" for f in res["failed"]]
    ok2, res2, lines2 = BO.gate(B, {"breakout": {"waive": names}}, None)
    assert ok2 and any("WAIVED" in l for l in lines2) and len(res2["waived"]) == len(names)


def test_gate_file_fails_and_records_the_report(tmp_path, capsys):
    path = G.board(tmp_path, qfn("U1", 50, 40) + wall())
    rp = str(tmp_path / "r.json")
    assert BO.gate_file(path, {}, None, rp) == 1
    d = json.load(open(rp))["breakout"]
    assert d["ok"] is False and d["failed"] and "U1" in d["templates"]
    assert "BREAKOUT_PIN_BLOCKED" in capsys.readouterr().out


def test_repair_moves_the_blocking_part_out_and_the_gate_passes(tmp_path):
    path = G.board(tmp_path, qfn("U1", 50, 40) + wall())
    r = BO.repair(path, {}, None)
    assert r["before"] > 0 and r["after"] == 0 and "C1" in r["moved"] and r["moved"]["C1"][3] > 0
    assert BO.gate_file(path, {}, None) == 0


def test_qfn_with_a_thermal_pad(tmp_path):
    B = load(tmp_path, qfn("U1", 50, 40))
    res = BO.solve(B, {}, None)
    assert res["dense"] == ["U1"] and not res["failed"]
    assert len(res["pins"]) == 32 and ("U1", "EP") not in res["pins"]            # the thermal pad leaves through its own vias (fanout)
    assert sorted(s["k"] for s in res["templates"]["U1"]["sides"]) == [0, 1, 2, 3]
    assert all(s["depth"] > 0.8 for s in res["templates"]["U1"]["sides"])


def test_lay_writes_locked_lanes_and_vias(tmp_path):
    path = G.board(tmp_path, conn("J1", 50, 40))
    B = RD.load(path)
    res = BO.solve(B, {}, None)
    for p in res["pins"].values():
        p["pts"] = [p["pad"]] + p["pts"]
    txt, ns, nv = BO.lay_text(open(path).read(), res["pins"], 0.2)
    assert nv == 40 and ns >= 80
    out = tmp_path / "laid.kicad_pcb"
    out.write_text(txt)
    B2 = RD.load(str(out))
    assert len(B2["vias"]) == 40 and len(B2["segs"]) == ns and all(s["locked"] for s in B2["segs"])
    again = BO.solve(B2, {}, None)                       # laid copper is attached: nothing left to plan
    assert not again["failed"] and not again["pins"]


def test_determinism_across_hash_seeds(tmp_path):
    path = G.board(tmp_path, conn("J1", 50, 40) + qfn("U1", 70, 40) + wall("C1", 66.5))
    code = ("import sys,json; sys.path.insert(0, %r)\nfrom fluxplace import breakout as BO, routediag as RD\n"
            "r = BO.solve(RD.load(%r), {}, None)\n"
            "print(json.dumps([sorted((list(k), v['pts'], v['via'], v['row']) for k, v in r['pins'].items()), [f['msg'] for f in r['failed']]], sort_keys=True))\n"
            % (os.path.join(os.path.dirname(__file__), ".."), path))
    outs = []
    for seed in ("0", "1", "4242"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        outs.append(subprocess.run(["/usr/bin/python3", "-c", code], capture_output=True, text=True, env=env).stdout)
    assert outs[0] and outs[0] == outs[1] == outs[2]


def test_relieve_never_keeps_a_move_that_seals_a_pin(tmp_path):
    from fluxplace import relieve as RV
    free = RD.load(G.board(tmp_path, qfn("U1", 50, 40) + wall(x=60.0), name="a.kicad_pcb"))
    walled = RD.load(G.board(tmp_path, qfn("U1", 50, 40) + wall(), name="b.kicad_pcb"))
    h0, h1 = RV.hard_state(free, {}, None), RV.hard_state(walled, {}, None)
    assert h0["breakout_failed"] == [] and h1["breakout_failed"]
    assert RV.hard_ok(h0, h1) and not RV.hard_ok(h1, h0)


def test_a_pin_walled_in_outward_leaves_the_other_way(tmp_path):
    # a 0.5 mm pad 0.8 mm in front of J1.7 (left column): every outward lane of its neighbours' rows stays, the pin itself turns round
    wall1 = G.fp("C1", 47.2, 38.0, [G.pad("1", 0, 0, "X", 0.5, 0.5), G.pad("2", 0, 2.0, "Y", 0.5, 0.5)], court=(-0.4, -0.4, 0.4, 2.4))
    B = load(tmp_path, conn("J1", 50, 40) + wall1)
    res = BO.solve(B, {}, None)
    alt = [r for r in res["pins"].values() if r.get("alt") is not None]
    assert alt and not res["failed"]
