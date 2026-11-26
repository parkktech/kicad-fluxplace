"""Pinned-part conflict resolution (pins.py) and through-board hole legality (legality.Legal.hole_issues). Synthetic boards only."""
import json, os, random, subprocess, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import geom as GM, intent as IT, legality as LGL, pins as PN, routediag as RD
import pcbgen as g


def conn(ref, x, y, court, net, n=2, side="F"):
    return g.fp(ref, x, y, [g.pad(str(i + 1), -1.0 + 2.0 * i / max(1, n - 1), 0, f"{net}{i}", 0.6, 0.6) for i in range(n)],
                court=court, side=side, name="Conn")


def hole(ref, x, y, drill=3.0, side="F", court=None):
    return g.fp(ref, x, y, [f'(pad "" np_thru_hole circle (at 0 0) (size {drill} {drill}) (drill {drill}) (layers "*.Cu"))'],
                court=court, side=side, name="Standoff")


def load(tmp_path, body, name="b.kicad_pcb"):
    return g.board(tmp_path, body, name=name)


def run(path, cons, tmp_path):
    return PN.prepare(path, cons, None, str(tmp_path / "wd"), log=lambda s: None)


# ------------------------------------------------------------------ D1: fixed vs lock.at
D1_BODY = (conn("J1", 30, 3.5, (-6, -3, 6, 3), "P") + conn("J2", 33, 3.5, (-3, -3, 3, 3), "A") +
           g.two("R1", 70, 50, "A0", "P0", court=(-1, -0.5, 1, 0.5)) + g.two("R2", 20, 60, "A1", "P1", court=(-1, -0.5, 1, 0.5)))
D1_CONS = {"placement": {"fixed": ["J1"]}, "lock": {"at": {"J2": [33, 3.5, "F"]}},
           "intent": {"edge": [{"refs": ["J1"], "side": "top", "hard": True}]}}


def test_fixed_part_yields_to_lock_at_with_minimal_displacement(tmp_path):
    path = load(tmp_path, D1_BODY)
    B0 = RD.load(path)
    assert LGL.Legal(B0, D1_CONS).issues()["overlap"] == ["J1/J2"]
    out, cons, R = run(path, D1_CONS, tmp_path)
    assert [d["anchor"] for d in R.decisions] == ["J1"] and not R.unresolved
    d = R.decisions[0]
    assert d["winner"] == "J2" and d["rank"] == 1 and d["winner_rank"] == 0 and "lower precedence" in d["why"]
    assert d["from"][:2] == [30, 3.5] and d["to"][1] == 3.5 and 6.0 <= d["distance"] <= 7.0     # slid along the top edge, to the left
    B = RD.load(out)
    assert B["parts"]["J2"]["pos"] == (33, 3.5)                                 # the explicit coordinate never moves
    assert LGL.count(LGL.Legal(B, D1_CONS).issues()) == 0
    assert all(r["met"] is not False for r in IT.check(B, cons) if r["hard"])   # the edge rule is kept
    assert "J1" not in cons["placement"]["fixed"] and "J1" in cons["floorplan"]["movable"]
    assert cons["floorplan"]["stay"]["refs"] == ["J1"] and "J1" in D1_CONS["placement"]["fixed"]      # the input is untouched
    assert os.path.exists(str(tmp_path / "wd" / "pins.json"))


def test_no_pinned_conflict_changes_nothing(tmp_path):
    path = load(tmp_path, conn("J1", 30, 3.5, (-6, -3, 6, 3), "P") + conn("J2", 60, 3.5, (-3, -3, 3, 3), "A"))
    out, cons, R = run(path, D1_CONS, tmp_path)
    assert out == path and cons is D1_CONS and R is None


# ------------------------------------------------------------------ D2: a bottom hole in a top part's courtyard
D2_BODY = (conn("S1", 60, 40, (-10, -5, 10, 5), "M", n=3, side="B") + hole("H1", 40, 40, side="B") + hole("H2", 80, 40, side="B")
           + conn("J3", 40, 38, (-4, -4, 4, 4), "T"))
D2_CONS = {"placement": {"fixed": ["S1"]}, "lock": {"at": {"J3": [40, 38, "F"]}},
           "intent": {"group": [{"name": "m2", "refs": ["S1", "H1", "H2"]}]}}


def test_hole_of_a_bottom_part_is_a_collision_through_the_board(tmp_path):
    B = RD.load(load(tmp_path, D2_BODY))
    L = LGL.Legal(B, D2_CONS)
    assert L.issues()["hole"] == ["H1/J3"] and L.issues()["overlap"] == []     # courtyards are per side; the hole is not
    f = [x for x in LGL.collisions(B, D2_CONS) if x["pair"] == ["H1", "J3"]]
    assert f and f[0]["level"] == "FAIL" and "hole_in_courtyard" in f[0]["kinds"]
    # a hypothetical pose of the standoff (the placers' call): inside = illegal, outside = legal
    p = B["parts"]["H1"]
    for x, ok in ((40, False), (20, True)):
        pads, court = GM.moved(p, x, 40, 0)
        assert (not [k for k, _d in L.part_issues("H1", court, pads) if k == "hole"]) is ok
    # and the other way round: moving the top part onto a bottom standoff
    q = B["parts"]["J3"]
    pads, court = GM.moved(q, 80, 40, 0)
    assert [("hole", "H2")] == [(k, d) for k, d in L.part_issues("J3", court, pads) if k == "hole"]


def test_group_yields_as_one_rigid_body(tmp_path):
    path = load(tmp_path, D2_BODY)
    B0 = RD.load(path)
    rel0 = {r: (B0["parts"][r]["pos"][0] - B0["parts"]["S1"]["pos"][0], B0["parts"][r]["pos"][1] - B0["parts"]["S1"]["pos"][1])
            for r in ("H1", "H2")}
    out, cons, R = run(path, D2_CONS, tmp_path)
    assert [d["unit"] for d in R.decisions] == [["H1", "H2", "S1"]] and not R.unresolved
    assert R.decisions[0]["rank"] == 2 and R.decisions[0]["winner"] == "J3"
    B = RD.load(out)
    for r in ("H1", "H2"):
        rel = (B["parts"][r]["pos"][0] - B["parts"]["S1"]["pos"][0], B["parts"][r]["pos"][1] - B["parts"]["S1"]["pos"][1])
        assert rel == pytest.approx(rel0[r], abs=1e-3)
    assert B["parts"]["J3"]["pos"] == (40, 38) and LGL.Legal(B, D2_CONS).issues()["hole"] == []
    assert not [x for x in LGL.collisions(B, D2_CONS) if x["level"] == "FAIL"]
    assert 3.0 <= R.decisions[0]["distance"] <= 6.0
    assert set(cons["floorplan"]["movable"]) == {"S1", "H1", "H2"}


def test_group_may_opt_out_of_rigid(tmp_path):
    B = RD.load(load(tmp_path, D2_BODY))
    on = IT.items(B, D2_CONS)["group"][0]
    off = IT.items(B, {"intent": {"group": [{"name": "m2", "refs": ["S1", "H1"], "rigid": False}]}})["group"][0]
    assert on["rigid"] is True and off["rigid"] is False
    assert "intent:m2" in IT.effective(D2_CONS)["placement"]["group"]
    assert "group" not in IT.effective({"intent": {"group": [{"name": "m2", "refs": ["S1", "H1"], "rigid": False}]}}).get("placement", {})


# ------------------------------------------------------------------ same precedence
SAME_CONS = {"lock": {"at": {"J1": [30, 3.5, "F"], "J2": [33, 3.5, "F"]}}}


def test_two_explicit_pins_stop_with_pin_conflict_and_three_options_each(tmp_path):
    path = load(tmp_path, D1_BODY)
    out, cons, R = run(path, SAME_CONS, tmp_path)
    assert out == path and not R.decisions and len(R.unresolved) == 1
    u = R.unresolved[0]
    assert sorted(u["options"]) == ["J1", "J2"] and all(len(v) == 3 for v in u["options"].values())
    assert all(o["distance"] > 0 for v in u["options"].values() for o in v)
    assert u["geometry"][0]["kind"] in ("overlap", "pad_short") and any("PIN_CONFLICT" in l for l in PN.report(R))
    assert "J1" in SAME_CONS["lock"]["at"]                                      # nothing was edited


def test_auto_moves_the_smaller_displacement_and_records_it(tmp_path):
    path = load(tmp_path, D1_BODY)
    c = {"lock": SAME_CONS["lock"], "placement": {"resolve_pin_conflicts": "auto"}}
    out, cons, R = run(path, c, tmp_path)
    assert len(R.decisions) == 1 and not R.unresolved
    d = R.decisions[0]
    assert "resolve_pin_conflicts = auto" in d["why"] and d["rank"] == 0 and d["winner_rank"] == 0
    assert d["anchor"] not in cons["lock"]["at"] and d["anchor"] in cons["floorplan"]["movable"]
    assert LGL.count(LGL.Legal(RD.load(out), c).issues()) == 0


# ------------------------------------------------------------------ determinism
_SCRIPT = '''
import json, sys
sys.path.insert(0, {root!r}); sys.path.insert(0, {tests!r})
from fluxplace import pins as PN
out, cons, R = PN.prepare({path!r}, {cons!r}, None, {wd!r}, log=lambda s: None)
print(json.dumps([R.decisions, R.unresolved], sort_keys=True, default=str))
'''


def test_resolution_is_deterministic_across_runs_and_hash_seeds(tmp_path):
    path = load(tmp_path, D2_BODY)
    res = []
    for i, seed in enumerate(("1", "2", "random")):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        code = _SCRIPT.format(root=os.path.join(os.path.dirname(__file__), ".."), tests=os.path.dirname(__file__), path=path,
                              cons=D2_CONS, wd=str(tmp_path / f"wd{i}"))
        res.append(subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout)
    assert res[0] == res[1] == res[2] and json.loads(res[0])[0]


# ------------------------------------------------------------------ one legality for the placer and the gate
def _random_board(rng, n=7):
    body = ""
    for i in range(n):
        x, y = rng.uniform(10, 90), rng.uniform(10, 70)
        side = rng.choice("FB")
        kind = rng.choice(("conn", "hole", "tht"))
        if kind == "conn":
            body += conn(f"C{i}", x, y, (-3, -2, 3, 2), f"N{i}", side=side)
        elif kind == "hole":
            body += hole(f"H{i}", x, y, side=side, court=rng.choice((None, (-2, -2, 2, 2))))
        else:
            body += g.fp(f"T{i}", x, y, [g.pad("1", -1, 0, f"T{i}a", 1.6, 1.6, typ="thru_hole").replace("(size", "(drill 0.9) (size"),
                                         g.pad("2", 1, 0, f"T{i}b", 1.6, 1.6, typ="thru_hole").replace("(size", "(drill 0.9) (size")],
                         court=(-3, -2, 3, 2), name="Hdr")
    return body


def test_placer_legality_and_the_gate_cannot_disagree(tmp_path):
    rng = random.Random(7)
    legal = illegal = 0
    for k in range(40):
        B = RD.load(g.board(tmp_path, _random_board(rng, 4 + k % 4), name=f"r{k}.kicad_pcb", outline=(0, 0, 100, 80)))
        L = LGL.Legal(B, {})
        iss = L.issues()
        bad = [x for x in LGL.collisions(B, {}) if x["level"] == "FAIL"]
        placer_bad = bool(iss["overlap"] or iss["hole"] or iss["pad_short"])
        assert placer_bad == bool(bad) or (not placer_bad and not bad) or placer_bad and not bad and not iss["hole"], (k, iss, bad)
        if not placer_bad:
            assert not bad                                              # "placer says legal" => "gate says no collision"
            legal += 1
        else:
            gate_pairs = {tuple(x["pair"]) for x in LGL.collisions(B, {})}
            for key in ("overlap", "hole"):
                for pr in iss[key]:
                    assert tuple(sorted(pr.split("/"))) in gate_pairs      # and every placer violation is a gate pair
            illegal += 1
    assert legal >= 3 and illegal >= 3


def test_hole_clearance_to_another_nets_copper_and_hole_to_hole(tmp_path):
    body = (hole("H1", 30, 30, 2.0) + conn("J1", 32.2, 30, (-0.5, -0.5, 0.5, 0.5), "A", n=1)
            + hole("H2", 60, 30, 2.0) + hole("H3", 62.1, 30, 2.0))
    B = RD.load(load(tmp_path, body))
    L = LGL.Legal(B, {})
    kinds = {(r, o, k) for r in B["parts"] for o, k, _e in L.hole_issues(r)}
    assert ("H1", "J1", "hole_clearance") in kinds and ("J1", "H1", "hole_clearance") in kinds
    assert ("H2", "H3", "hole_clearance") in kinds                              # 0.1 mm between the drills < the 0.25 hole clearance
    assert L.issues()["hole"] == ["H1/J1", "H2/H3"]


# ------------------------------------------------------------------ the floorplan keeps a released unit near its seat
def test_stay_term_only_with_released_units(tmp_path):
    from fluxplace import floorplan2 as F2, modules as MD, routediag as RD2
    path = load(tmp_path, D1_BODY)
    out, cons, R = run(path, D1_CONS, tmp_path)
    for c, expect in ((D1_CONS, False), (cons, True)):
        B = RD2.load(out)
        P = F2.Plan(B, c, None, MD.build(B, c, None, arrange=False), log=lambda s: None)
        assert ("stay" in P.cost_parts()) is expect


def test_review_reports_each_released_pin(tmp_path):
    from fluxplace import review as RV
    path = load(tmp_path, D1_BODY)
    out, cons, R = run(path, D1_CONS, tmp_path)
    PN.write(PN.sidecar_path(out), R)
    f = RV.check_pins({"board_path": out})
    assert [x["code"] for x in f] == ["PIN_RELEASED"] and f[0]["level"] == "WARN" and f[0]["refs"] == ["J1"]
    PN.write(PN.sidecar_path(path), PN.Resolver(RD.load(path), SAME_CONS, None).run())
    f = RV.check_pins({"board_path": path})
    assert [x["code"] for x in f] == ["PIN_CONFLICT"] and f[0]["level"] == "FAIL"


def test_floorplan_command_resolves_pins_first(tmp_path, capsys, monkeypatch):
    import cli
    from fluxplace import floorplan2 as FP2
    path = load(tmp_path, D1_BODY)
    cf = tmp_path / "c.toml"
    cf.write_text('[placement]\nfixed = ["J1"]\n[lock.at]\nJ2 = [33, 3.5, "F"]\n')
    seen = {}

    def once(b, cons, spec, grow, args, text_in, wd, tag, log=print):
        seen.update(board=b, cons=cons)
        res = {"tag": tag, "grow": list(grow), "escaped": 1, "required": 1, "moved_modules": 0, "movable": 1,
               "source": {"cross": 0, "wl": 1.0}, "annealed": {"cross": 0, "wl": 1.0}, "secs": 0.0, "construct_failed": [],
               "legality_new": {}, "fanout_ok": True, "rounds": [], "board": b, "blocked": [], "metrics": {"RF_EXT": None, "pair_spans": []}}
        return res, None, RD.load(b)
    monkeypatch.setattr(FP2, "floorplan_once", once)
    monkeypatch.setattr(FP2, "edge_order", lambda B, cons: ["L", "R", "T", "B"])
    monkeypatch.setattr(FP2, "measured", lambda p, c, s: {"cross": 0, "wl": 1.0})      # as the stub's own source/annealed: the never-worse gate sees no change
    monkeypatch.setattr(FP2, "span_summary", lambda s: "-")
    monkeypatch.setattr(FP2, "plan_summary", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("skipped")))
    out = str(tmp_path / "o.kicad_pcb")
    cli.main(["floorplan", path, "--out", out, "--constraints", str(cf), "--assume-defaults"])
    assert "PIN_RELEASED J1" in capsys.readouterr().out
    B = RD.load(seen["board"])                                           # the search starts from the resolved board
    assert not LGL.Legal(B, {}).issues()["overlap"] and B["parts"]["J2"]["pos"] == (33, 3.5)
    assert "J1" in seen["cons"]["floorplan"]["movable"] and "J1" not in seen["cons"]["placement"]["fixed"]
    assert json.load(open(PN.sidecar_path(out)))["decisions"][0]["anchor"] == "J1"
    # two explicit pins: the stage fails before anything is placed
    cf.write_text('[lock.at]\nJ1 = [30, 3.5, "F"]\nJ2 = [33, 3.5, "F"]\n')
    with pytest.raises(SystemExit) as e:
        cli.main(["floorplan", path, "--out", out, "--constraints", str(cf), "--assume-defaults"])
    assert e.value.code == 1 and "PIN_CONFLICT" in capsys.readouterr().out


def test_relieve_engine_never_moves_a_group_member_alone(tmp_path):
    from types import SimpleNamespace
    from fluxplace import planplace as PP
    B = RD.load(load(tmp_path, D2_BODY))
    me = SimpleNamespace(B=B, rigid_groups=[["S1", "H1", "H2"]])
    P = B["parts"]
    rigid = lambda dx, dy, d=0.0: {r: (P[r]["pos"][0] + dx, P[r]["pos"][1] + dy, P[r]["angle"] + d) for r in ("S1", "H1", "H2")}
    ok = lambda poses, flip=None: PP.PlanPlacer._rigid_ok(me, poses, flip)
    assert ok(rigid(3, 4)) and ok({"J3": (1, 1, 0)})
    assert not ok({"H1": (41, 40, 0)})                                                  # one member alone
    assert not ok({k: v for k, v in rigid(3, 4).items() if k != "H2"})                  # an incomplete group
    bad = rigid(3, 4)
    bad["H2"] = (bad["H2"][0] + 1, bad["H2"][1], bad["H2"][2])
    assert not ok(bad)                                                                  # not one rigid motion
    assert not ok(rigid(0, 0), flip="H1")                                               # never flipped alone


def test_movable_parts_on_the_new_seat_are_moved_off(tmp_path):
    path = load(tmp_path, D1_BODY + g.two("R3", 24, 3.5, "Q0", "Q1", court=(-1, -0.5, 1, 0.5)))     # sits where J1 will land
    out, cons, R = run(path, D1_CONS, tmp_path)
    assert R.evicted == ["R3"] and RD.load(out)["parts"]["R3"]["pos"] != (24, 3.5)
    B = RD.load(out)
    assert LGL.count(LGL.Legal(B, D1_CONS).issues()) == 0 and not [x for x in LGL.collisions(B, D1_CONS) if x["level"] == "FAIL"]
    far = RD.load(path)["parts"]["R2"]["pos"]
    assert RD.load(out)["parts"]["R2"]["pos"] == far                          # parts that collide with nothing stay where they are
