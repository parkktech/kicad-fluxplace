"""Global router (globalroute.py) and plan-route (planroute.py) on synthetic boards.  Pure text: any interpreter."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import constraints as C, escapeplan as EP, fluxmap as FM, globalroute as GR, netclass as NC
from fluxplace import planroute as PL, routediag as RD

PAIRCONS = {}


def channel_board(tmp, n_nets, gap=1.0, walls=True, pair=False):
    """n_nets 2-pad nets run from x=10 to x=90 across a channel `gap` mm tall between two keep-out walls."""
    body = ""
    if walls:
        y0, y1 = 40 - gap / 2, 40 + gap / 2
        body += PB.keepout(40, 0, 60, y0, "wallN") + PB.keepout(40, y1, 60, 80, "wallS")
    names = []
    for i in range(n_nets):
        names.append(f"SIG{i}")
    if pair:
        names = ["ETH_P0_P", "ETH_P0_N"]
    L = "".join(PB.fp(f"L{i}", 10, 40 + (i - (len(names) - 1) / 2) * 0.8, [PB.pad("1", 0, 0, n, 0.5, 0.5)]) for i, n in enumerate(names))
    R = "".join(PB.fp(f"R{i}", 90, 40 + (i - (len(names) - 1) / 2) * 0.8, [PB.pad("1", 0, 0, n, 0.5, 0.5)]) for i, n in enumerate(names))
    return PB.write(tmp, body + L + R)


def run(path, **kw):
    B = RD.load(path)
    return PL.run(B, {}, None, ignore_routing=True, **kw)


def test_narrow_channel_overflows_and_names_the_cell(tmp_path):
    R = run(channel_board(tmp_path, 16, gap=1.0))
    ov = [f for f in R["findings"] if f["code"] == "PLAN_OVERFLOW"]
    assert ov and R["gr"].summary()["overflow"] > 0
    f = ov[0]
    assert 38 <= f["at"][1] <= 42 and 38 <= f["at"][0] <= 62 and f["demand"] > f["capacity"]
    assert f["cells_xy"] and f["nets"] and f["suggestion"]
    assert PL.verdict(R)["result"] == "FAIL"


def test_removing_the_obstacle_clears_it(tmp_path):
    R = run(channel_board(tmp_path, 16, gap=1.0, walls=False))
    assert not [f for f in R["findings"] if f["code"] == "PLAN_OVERFLOW"]
    assert PL.verdict(R)["result"] == "ok"
    R2 = run(channel_board(tmp_path, 16, gap=16.0))              # a wide channel also passes
    assert not [f for f in R2["findings"] if f["code"] == "PLAN_OVERFLOW"]


def test_pair_stays_together_in_a_wide_channel_and_splits_in_a_pinched_one(tmp_path):
    R = run(channel_board(tmp_path, 2, gap=6.0, pair=True))
    assert [n for n in R["gr"].nets.values() if n["pair"]] and not R["gr"].pair_origin
    assert not [f for f in R["findings"] if f["code"] == "PLAN_PAIR_SPLIT"]
    R = run(channel_board(tmp_path, 2, gap=0.5, pair=True), iterations=8)
    assert [f for f in R["findings"] if f["code"] == "PLAN_PAIR_SPLIT"]


def test_score_is_worse_with_the_wall(tmp_path):
    a = run(channel_board(tmp_path, 16, gap=1.0))["score"]
    b = run(channel_board(tmp_path, 16, gap=1.0, walls=False))["score"]
    assert a > b + 20


def test_deterministic(tmp_path):
    p = channel_board(tmp_path, 12, gap=1.0)
    a, b = run(p), run(p)
    assert a["score"] == b["score"] and json.dumps(PL.plan_section(a), sort_keys=True) == json.dumps(PL.plan_section(b), sort_keys=True)


def test_plan_compare_prints_both_verdicts(tmp_path, capsys):
    import cli
    pa, pb = tmp_path / "a", tmp_path / "b"
    pa.mkdir(); pb.mkdir()
    A = channel_board(pa, 16, gap=1.0)
    Bd = channel_board(pb, 16, gap=1.0, walls=False)
    maps = []
    for p in (A, Bd):
        try:
            cli.main(["plan-route", p, "--ignore-routing", "-q"])
        except SystemExit:
            pass
        maps.append(FM.default_path(p))
    capsys.readouterr()
    cli.main(["plan-compare", maps[0], maps[1]])
    out = capsys.readouterr().out
    assert "PLAN_SCORE" in out and "plan-compare: B is better" in out and "PLAN_OVERFLOW" in out
