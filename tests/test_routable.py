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
