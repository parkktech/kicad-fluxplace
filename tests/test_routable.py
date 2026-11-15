import os, subprocess, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import routable as RB, routediag as RD, preroute as PR, geom as GM, legalise as LG, boardwrite as BW
import pcbgen as g

ROOT = os.path.join(os.path.dirname(__file__), "..")


def val(ref, x, y, a, b, **kw):
    return g.two(ref, x, y, a, b, court=(-1.0, -0.5, 1.0, 0.5), **kw)


def conn(ref, x, y, nets, angle=0.0):
    return g.fp(ref, x, y, [g.pad(str(i + 1), 0, (i - 2.5) * 2, n, 0.8, 0.6) for i, n in enumerate(nets)],
                angle=angle, court=(-1, -7, 1, 7))


def mk(tmp_path):
    b = conn("J1", 5, 40, ["A", "CH1", "C", "D", "E", "F"]) + conn("J2", 95, 40, ["G", "CH3", "I", "J", "K", "L"])
    b += g.ic("U1", 80, 70, ["A", "B", "C", "VDD", "GND", "Z1"], pitch=1.27, court=(-3, -3, 3, 3))
    b += g.ic("U2", 20, 10, ["G", "H", "I", "VDD", "GND", "Z2"], pitch=1.27, court=(-3, -3, 3, 3))
    b += val("C1", 90, 10, "VDD", "GND")             # decap of U1's VDD? VDD is on 2 ICs: guessed
    b += val("C2", 70, 15, "Z1", "GND")              # support of U1 (unique net), far away
    b += val("R1", 15, 70, "Z2", "VCC")
    b += val("R5", 50, 40, "CH1", "x1") + val("R6", 52, 40, "x1", "CH3")   # chain between the two connectors
    b += g.fp("MH1", 50, 5, [g.pad("1", 0, 0, "", typ="np_thru_hole")], court=(-1, -1, 1, 1), locked=True)
    return g.board(tmp_path, b)


def test_routable_moves_support_next_to_ic_keeps_fixed_and_overlap_free(tmp_path):
    p = mk(tmp_path)
    B = RD.load(p)
    mv = RB.place(B, {"placement": {"fixed": ["J1", "J2"]}}, None, log=lambda m: None)
    P = B["parts"]
    assert "J1" not in mv and "J2" not in mv and "MH1" not in mv
    assert P["J1"]["pos"] == (5, 40)
    # C2 and R1 now sit beside their ICs
    def d(a, ic):
        pa = [q for q in P[a]["pads"] if q["net"] in ("Z1", "Z2")][0]
        pb = [q for q in P[ic]["pads"] if q["net"] == pa["net"]][0]
        return ((pa["x"] - pb["x"]) ** 2 + (pa["y"] - pb["y"]) ** 2) ** .5
    assert d("C2", "U1") < 5 and d("R1", "U2") < 5
    # nothing overlaps and nothing left the outline
    M = LG._Model(B, {}, set(), B["bbox"])
    rep = LG.verify(B, M, {r: (*P[r]["pos"], P[r]["angle"]) for r in P}, B["bbox"])
    assert rep["outside"] == [] and rep["overlaps"] == []


def test_routable_chain_is_ordered_along_the_end_to_end_line(tmp_path):
    p = mk(tmp_path)
    B = RD.load(p)
    cls = RB.NC.classify(B)
    fixed = {"J1", "J2"}
    ch = RB.chains(B, cls, fixed, {})
    assert ["J1", "R5", "R6", "J2"] in ch
    mv = RB.place(B, {"placement": {"fixed": ["J1", "J2"]}}, None, log=lambda m: None)
    P = B["parts"]
    assert P["J1"]["pos"][0] < P["R5"]["pos"][0] < P["R6"]["pos"][0] < P["J2"]["pos"][0]


def test_routable_improves_the_scorecard_and_is_deterministic(tmp_path):
    p = mk(tmp_path)
    f0, c0 = PR.run(RD.load(p))
    out = []
    for i in range(2):
        B = RD.load(p)
        mv = RB.place(B, {"placement": {"fixed": ["J1", "J2"]}}, None, log=lambda m: None)
        o = tmp_path / f"o{i}.kicad_pcb"
        BW.move_file(p, str(o), mv)
        out.append(o.read_text())
    assert out[0] == out[1]
    f1, c1 = PR.run(RD.load(str(tmp_path / "o0.kicad_pcb")))
    assert c1["counts"]["SUPPORT_PART_FAR"] < c0["counts"]["SUPPORT_PART_FAR"]
    assert c1["weighted_ratsnest_mm"] <= c0["weighted_ratsnest_mm"]


def test_cli_place_routable(tmp_path):
    p = mk(tmp_path)
    out = tmp_path / "r.kicad_pcb"
    r = subprocess.run([sys.executable, os.path.join(ROOT, "cli.py"), "place", "--board", p, "--strategy",
                        "routable", "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0 and "BEFORE" in r.stdout and "AFTER" in r.stdout, r.stderr
    assert os.path.exists(out)


def test_chain_detection_movable_ends_three_pad_nets_and_a_hub(tmp_path):
    # controller U1 (hub: SPI to U9 and J9) -> transceiver U2 -> choke L1 -> fixed connector J1;
    # the line pair is 3-pad (choke, connector, TVS D1); the transceiver also has a supply-enable net
    b = g.ic("U1", 20, 20, ["TXD", "RXD", "SCK", "MOSI", "CS", "X"]) + g.ic("U9", 20, 50, ["SCK", "MOSI", "a", "b", "c", "d"])
    b += g.fp("J9", 5, 50, [g.pad("1", 0, 0, "SCK"), g.pad("2", 1, 0, "MOSI"), g.pad("3", 2, 0, "CS")])
    b += g.ic("U2", 50, 20, ["TXD", "RXD", "XH", "XL", "EN", "e"])
    b += g.fp("L1", 70, 20, [g.pad("1", 0, 0, "XH"), g.pad("2", 1, 0, "XL"), g.pad("3", 0, 1, "CH"), g.pad("4", 1, 1, "CL")])
    b += g.fp("J1", 95, 20, [g.pad("1", 0, 0, "CH"), g.pad("2", 1, 0, "CL")], locked=True)
    b += g.fp("D1", 85, 30, [g.pad("1", 0, 0, "CH"), g.pad("2", 1, 0, "CL"), g.pad("3", 2, 0, "GND")])
    B = RD.load(g.board(tmp_path, b))
    cls = RB.NC.classify(B)
    fixed = {"J1"}
    ch = RB.chains(B, cls, fixed, {})
    assert any(c[0] in ("U1", "J1") and set(c) >= {"U1", "U2", "L1", "J1"} for c in ch), ch


REFINE_EVALS = 200     # evaluation budget for the determinism tests (the budget, not the clock, ends them)
_REFINE_CODE = """
import json, sys, tempfile, pathlib
sys.path.insert(0, "__ROOT__"); sys.path.insert(0, "__TESTS__")
import test_routable as T
from fluxplace import refine as RF, routediag as RD
B = RD.load(T.mk(pathlib.Path(tempfile.mkdtemp())))
cons = {"placement": {"fixed": ["J1", "J2"], "refine_max_evals": __EVALS__}}
mv, st = RF.refine(B, cons, None, seed=3, log=lambda m: None, time_limit=600)
print(json.dumps({"moves": sorted(mv.items()), "kept": st["kept"], "evals": st["evals"],
                  "budget": st["budget"], "after": st["score_after"]}, sort_keys=True))
"""


def test_refine_is_local_legal_deterministic_and_improves(tmp_path):
    from fluxplace import refine as RF, legality as LGL
    p = mk(tmp_path)
    cons = {"placement": {"fixed": ["J1", "J2"], "refine_max_evals": REFINE_EVALS}}
    out = []
    for i in range(2):
        B = RD.load(p)
        sc0 = PR.run(B, cons)[1]["score"]
        base = {r: tuple(B["parts"][r]["pos"]) for r in B["parts"]}
        mv, st = RF.refine(B, cons, None, seed=3, log=lambda m: None, time_limit=30)
        assert st["score_after"] <= sc0
        assert set(st["kept"]) <= {"nudge", "rotate", "slide", "swap", "swapc"} and st["kept"]
        assert "J1" not in mv and "J2" not in mv                  # fixed parts never move
        assert LGL.count(LGL.Legal(B, cons).issues()) == 0
        out.append(sorted(mv.items()))
    assert out[0] == out[1]


def test_refine_identical_across_hash_seeds(tmp_path):
    """Same input, three PYTHONHASHSEED values (set/dict iteration order differs per process):
    the refine must return the same placement, kept counts and evaluation count."""
    import json
    code = (_REFINE_CODE.replace("__ROOT__", os.path.abspath(ROOT))
            .replace("__TESTS__", os.path.abspath(os.path.join(ROOT, "tests")))
            .replace("__EVALS__", str(REFINE_EVALS)))
    procs = [subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True,
                              env={**os.environ, "PYTHONHASHSEED": str(h)}) for h in (0, 1, 2)]
    outs = []
    for pr in procs:
        so, se = pr.communicate(timeout=900)
        assert pr.returncode == 0, se[-2000:]
        outs.append(so.strip().splitlines()[-1])
    assert outs[0] == outs[1] == outs[2], outs
    got = json.loads(outs[0])
    assert got["budget"] == "evals" and got["evals"] == REFINE_EVALS, got


def test_refine_identical_under_slowed_run(tmp_path, monkeypatch):
    """A machine several times slower must give the same placement: the budget counts evaluations,
    so a slow run and a fast run stop at the same move. The wall-clock cap is far away here."""
    import time as _time
    from fluxplace import refine as RF, preroute as PRE
    cons = {"placement": {"fixed": ["J1", "J2"], "refine_max_evals": REFINE_EVALS}}

    def go():
        B = RD.load(mk(tmp_path))
        mv, st = RF.refine(B, cons, None, seed=3, log=lambda m: None, time_limit=300)
        return sorted(mv.items()), st["kept"], st["evals"], st["budget"]

    fast = go()
    real = PRE.run

    def slowed(*a, **k):
        _time.sleep(0.05)
        return real(*a, **k)
    monkeypatch.setattr(PRE, "run", slowed)
    slow = go()
    assert slow == fast, (fast[1:], slow[1:])
    assert fast[3] == "evals"
