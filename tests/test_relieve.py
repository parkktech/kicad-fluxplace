"""relieve (fluxplace/relieve.py) on synthetic boards.  Pure text boards, no pcbnew; the subprocess test runs the CLI twice."""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import planroute as PL, relieve as RL, routediag as RD

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
