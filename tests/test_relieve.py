"""relieve (fluxplace/relieve.py) on synthetic boards.  Pure text boards, no pcbnew; the subprocess test runs the CLI twice."""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import boardwrite as BW, planroute as PL, relieve as RL, routediag as RD

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _board(tmp, body, name="b.kicad_pcb", edge=None):
    p = tmp / name
    p.write_text(PB.HEAD + (edge or PB.EDGE) + body + ")\n")
    return str(p)


def _sealed_codec(tmp):
    """A fine-pitch dual-row IC whose west pins have no lane and no via site: one tall foreign pad (part W0, movable) sits in the
    escape field.  Three pins are sealed by W0.1 in the predictor (checked by test_sealed_fixture_is_really_sealed)."""
    body = PB.column_ic("U1", 50, 40, [f"A{i}" for i in range(8)], pitch=0.5)
    body += PB.fp("W0", 46.8, 40, [PB.pad("1", 0, 0, "WN", 0.6, 12.0)])
    return _board(tmp, body)


def test_sealed_fixture_is_really_sealed(tmp_path):
    p = _sealed_codec(tmp_path)
    R = PL.run(RD.load(p), {}, None, ignore_routing=True)
    assert R["comp"]["sealed"] >= 1 and any(f.get("blocker", "").startswith("W0") for f in R["findings"] if f["code"] == "PLAN_PIN_SEALED")


def test_sealed_pin_freed_by_nudging_a_movable_blocker(tmp_path):
    p = _sealed_codec(tmp_path)
    S = RL.run(p, {}, None, str(tmp_path / "out.kicad_pcb"), rounds=10, log=lambda m: None)
    assert S["input"]["components"]["sealed"] >= 1
    assert S["output"]["components"]["sealed"] < S["input"]["components"]["sealed"]
    assert S["moves"] and S["guard"]["ok"]                 # the pin was freed by a move of a movable part (U1 rotation or W0 nudge)
    assert S["output"]["score"] < S["input"]["score"]


def test_fixed_blocker_gets_no_move_and_an_owner_row(tmp_path):
    p = _sealed_codec(tmp_path)
    S = RL.run(p, {"placement": {"fixed": ["W0"]}}, None, str(tmp_path / "out.kicad_pcb"), rounds=10, log=lambda m: None)
    assert all(r["ref"] != "W0" for r in S["moves"]) and S["guard"]["ok"]      # the fixed blocker never moves
    if S["output"]["components"]["sealed"]:
        assert any(r["kind"] == "owner" and "W0" in r["text"] for r in S["owner_rows"])


def test_clean_board_is_never_made_worse(tmp_path):
    p = _board(tmp_path, PB.fp("R1", 20, 20, [PB.pad("1", 0, 0, "N1", 0.5, 0.5)]) + PB.fp("R2", 30, 20, [PB.pad("1", 0, 0, "N1", 0.5, 0.5)]))
    S = RL.run(p, {}, None, str(tmp_path / "out.kicad_pcb"), log=lambda m: None)
    assert S["kind"] == "input" and S["parts_moved"] == 0
    assert open(p).read() == open(tmp_path / "out.kicad_pcb").read()
    assert S["guard"]["ok"]


def test_output_is_byte_identical_under_different_hash_seeds(tmp_path):
    p = _sealed_codec(tmp_path)
    outs = []
    for seed in ("1", "2"):
        o = tmp_path / f"o{seed}.kicad_pcb"
        env = dict(os.environ, PYTHONHASHSEED=seed)
        code = ("import sys; sys.path.insert(0, %r); from fluxplace import relieve as RL; "
                "RL.run(%r, {}, None, %r, rounds=4, log=lambda m: None)" % (ROOT, p, str(o)))
        subprocess.run([sys.executable, "-c", code], check=True, env=env, timeout=600)
        outs.append((o.read_bytes(), (tmp_path / f"o{seed}.kicad_pcb.relieve.json").read_bytes()))
    assert outs[0] == outs[1]


def test_out_must_differ_from_input(tmp_path):
    p = _board(tmp_path, PB.fp("R1", 20, 20, [PB.pad("1", 0, 0, "N1")]))
    try:
        RL.run(p, {}, None, p, log=lambda m: None)
    except SystemExit:
        return
    raise AssertionError("relieve wrote over its input")


def test_growth_is_off_by_default_and_bounded_when_allowed(tmp_path):
    p = _sealed_codec(tmp_path)
    S0 = RL.run(p, {"placement": {"fixed": ["W0", "U1"]}}, None, str(tmp_path / "oa.kicad_pcb"), rounds=5, log=lambda m: None)
    assert S0["growth"]["chosen"] is None and all(pr["phase"] == "table" for pr in S0["growth"]["probes"])
    assert S0["output"]["components"]["sealed"] >= 1                      # fixed blockers: no move frees the pin, so growth is the next option
    S1 = RL.run(p, {"placement": {"fixed": ["W0", "U1"]}}, None, str(tmp_path / "ob.kicad_pcb"), rounds=5, may_grow=3, sides=["L", "T"],
                log=lambda m: None)
    grown = [pr for pr in S1["growth"]["probes"] if pr["phase"] != "table"]
    assert grown and all(pr["mm"] <= 3 and pr["side"] in ("L", "T") for pr in grown)   # probes stay inside the allowed sides and size
    assert all(pr["phase"] == "max" for pr in grown)                                 # no side passes at 3 mm: no bisection
    assert S1["growth"]["chosen"] in (None, "L", "T") and S1["guard"]["ok"]


# ---- fixes-d #1-#3: routed parts stay put, the guard checks geometry, the ledger holds applied moves only
def _positions(path):
    """per ref: its origin and every pad's absolute centre (so a rotation shows as a change too)."""
    B = RD.load(path)
    return {r: (tuple(round(v, 3) for v in B["parts"][r]["pos"]),
                tuple(sorted((str(q["num"]), round(q["x"], 3), round(q["y"], 3)) for q in B["parts"][r]["pads"])))
            for r in B["parts"]}


def _routed_codec(tmp):
    """the sealed codec plus a track of net WN that ends on W0's pad: W0 is a routed part."""
    body = PB.column_ic("U1", 50, 40, [f"A{i}" for i in range(8)], pitch=0.5)
    body += PB.fp("W0", 46.8, 40, [PB.pad("1", 0, 0, "WN", 0.6, 12.0)])
    body += ('(segment (start 46.8 34.0) (end 46.8 40.0) (width 0.2) (layer "F.Cu") (net "WN") '
             '(uuid "00000000-0000-0000-0000-0000000000a1"))\n')
    return _board(tmp, body)


def test_routed_part_is_fixed_unless_allowed(tmp_path):
    p = _routed_codec(tmp_path)
    assert RL.routed_refs(RD.load(p)) == ["W0"]
    S = RL.run(p, {}, None, str(tmp_path / "out.kicad_pcb"), rounds=10, log=lambda m: None)
    assert all(r["ref"] != "W0" for r in S["moves"])
    assert _positions(p)["W0"] == _positions(str(tmp_path / "out.kicad_pcb"))["W0"]
    assert S["removed_copper"] == [] and S["guard"]["ok"]


def test_move_routed_removes_copper_and_lists_it(tmp_path):
    p = _routed_codec(tmp_path)
    S = RL.run(p, {}, None, str(tmp_path / "out.kicad_pcb"), rounds=10, log=lambda m: None, allow_move_routed=True)
    moved = {r["ref"] for r in S["moves"]}
    if "W0" in moved:
        assert any(c["net"] == "WN" for c in S["removed_copper"]) and S["guard"]["ok"]
        assert "removed copper" in S["text"]


def test_guard_checks_geometry_not_counts(tmp_path):
    """the same copper with its pad moved away: segment and via counts are unchanged, but the track end is detached."""
    p = _routed_codec(tmp_path)
    text = open(p).read()
    moved = BW.apply_moves(text, {"W0": (46.8 + 1.0, 40.0, 0.0)})[0]
    q = str(tmp_path / "moved.kicad_pcb")
    open(q, "w").write(moved)
    ok, probs = RL.guard(p, q)
    assert not ok and any("no longer on W0.1" in x for x in probs), probs
    assert RL.guard(p, p) == (True, [])


def test_applied_log_drops_flipped_parts(tmp_path):
    e = {"refs": ["A", "B"], "kind": "k", "reason": "r", "from": {"A": [0, 0, 0], "B": [1, 1, 0]},
         "to": {"A": [1, 0, 0], "B": [2, 1, 0]}, "before": 1, "after": 2}
    out = RL.applied_log([e], {"B"})
    assert out[0]["refs"] == ["A"] and set(out[0]["from"]) == {"A"} and set(out[0]["to"]) == {"A"}
    assert RL.applied_log([dict(e, refs=["B"])], {"B"}) == []


def test_reported_moves_match_positions_in_the_written_board(tmp_path):
    """parts_moved / moves are the parts whose position really differs between the input and the written output."""
    p = _sealed_codec(tmp_path)
    out = str(tmp_path / "out.kicad_pcb")
    S = RL.run(p, {}, None, out, rounds=10, log=lambda m: None)
    before, after = _positions(p), _positions(out)
    differ = {r for r in before if before[r] != after.get(r)}
    assert set(r["ref"] for r in S["moves"]) <= differ
    assert S["parts_moved"] == len({r["ref"] for r in S["moves"]})


def test_guard_failure_writes_copy_of_input(tmp_path, monkeypatch):
    p = _sealed_codec(tmp_path)
    out = tmp_path / "out.kicad_pcb"
    real = RL.guard

    def refuse_moved(path_in, path_out, removed=None):
        if os.path.basename(path_out) != "input.kicad_pcb":
            return False, ["injected: track end detached"]
        return real(path_in, path_out, removed)
    monkeypatch.setattr(RL, "guard", refuse_moved)
    S = RL.run(p, {}, None, str(out), rounds=10, log=lambda m: None)
    assert S["guard"]["rejected"] == ["injected: track end detached"] and S["guard"]["ok"]
    assert out.read_text() == open(p).read()
    from fluxplace import plancli
    monkeypatch.setattr(plancli, "_ctx", lambda a: ({}, None))
    ns = type("A", (), {"board": p, "out": str(out), "rounds": 10, "may_grow": None, "grow_sides": None, "json": None,
                        "allow_move_routed": False})()
    assert plancli.cmd_relieve(ns) == 1
    assert out.read_text() == open(p).read()
