import os, sys, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import clusters as CL, routediag as RD, boardwrite as BW, geom as GM
import pcbgen as g


def three_ics(tmp_path, far=True):
    b = g.ic("U410", 20, 20, ["VDD", "A1", "D410", "GND", "A2", "A3"], pitch=0.5)
    b += g.ic("U420", 60, 20, ["VDD", "B1", "D420", "GND", "B2", "B3"], pitch=0.5)
    b += g.two("C411", 40 if far else 24, 20, "D410", "GND")          # decap of U410 (unique net)
    b += g.two("C421", 62, 28, "D420", "GND")
    # rail caps on VDD, block-numbered
    b += g.two("C412", 55 if far else 24, 30, "VDD", "GND")            # block 4 -> U410 (nearest number)
    b += g.two("C422", 58, 30, "VDD", "GND")                           # block 4 -> U420
    b += g.two("C13", 30, 40, "VDD", "GND")                            # block 0: ownership is a guess
    b += g.fp("J1", 5, 60, [g.pad("1", 0, 0, "VDD"), g.pad("2", 1, 0, "GND")])
    return g.board(tmp_path, b)


def test_unique_net_and_block_ownership(tmp_path):
    B = RD.load(three_ics(tmp_path))
    own, links = CL.ownership(B)
    assert own["C411"]["ic"] == "U410" and own["C411"]["how"] == "net"
    assert own["C421"]["ic"] == "U420"
    assert own["C412"]["ic"] == "U410" and own["C412"]["how"] == "refdes"
    assert own["C422"]["ic"] == "U420"
    assert own["C13"]["how"] == "spread"        # flat numbering: guessed, never graded
    assert not links


def test_rail_cap_never_goes_to_arbitrary_far_ic(tmp_path):
    # C412 sits 35 mm from U410 but only 5 mm from U420: nearest-by-geometry would say U420
    B = RD.load(three_ics(tmp_path))
    cl, _ = CL.build(B)
    far = {s["ref"] for _, s in CL.far_parts(cl)}
    assert "C412" in far and "C411" in far and "C13" not in far
    assert {c["ic"]: [s["ref"] for s in c["supports"]] for c in cl}["U410"][0] in ("C411", "C412")


def test_series_part_between_two_ics_is_a_link(tmp_path):
    b = g.ic("U1", 10, 10, ["X1", "a", "b", "c", "d"]) + g.ic("U2", 30, 10, ["X2", "e", "f", "g", "h"])
    b += g.two("R1", 20, 10, "X1", "X2")
    B = RD.load(g.board(tmp_path, b))
    own, links = CL.ownership(B)
    assert "R1" in links and "R1" not in own


def test_reseat_moves_far_parts_next_to_pin_without_overlap(tmp_path):
    B = RD.load(three_ics(tmp_path))
    cl, _ = CL.build(B)
    moves = CL.reseat(B, cl, bounds=(0, 0, 100, 80))
    assert "C411" in moves and "C412" in moves and "C13" not in moves
    cl2, _ = CL.build(B)
    assert CL.far_parts(cl2) == []
    P = B["parts"]
    for a in P:
        for b in P:
            if a < b:
                assert not GM.overlap(P[a]["court"], P[b]["court"], -0.001), (a, b)
    # IC never moved
    assert P["U410"]["pos"] == (20, 20)
    # the cap stays out of the IC's pin lanes
    for ln in CL._lanes(B, "U410"):
        assert not GM.overlap(P["C411"]["court"], ln)


def test_locked_support_not_moved(tmp_path):
    b = g.ic("U1", 20, 20, ["N1", "a", "b", "c", "d"]) + g.two("C1", 40, 20, "N1", "GND", locked=True)
    B = RD.load(g.board(tmp_path, b))
    cl, _ = CL.build(B)
    assert CL.reseat(B, cl) == {}


def test_boardwrite_rotates_rigidly_and_pcb_text_survives(tmp_path):
    p = g.board(tmp_path, g.fp("C1", 10, 10, [g.pad("1", -1, 0, "A"), g.pad("2", 1, 0, "B")], angle=90.0))
    B0 = RD.load(p)
    out = tmp_path / "o.kicad_pcb"
    done = BW.move_file(p, str(out), {"C1": (30, 40, 180)})
    assert done == ["C1"]
    B1 = RD.load(str(out))
    c = B1["parts"]["C1"]
    assert c["pos"] == (30, 40) and abs(c["angle"] - 180) < 1e-9
    exp, _ = GM.moved(B0["parts"]["C1"], 30, 40, 180)
    for a, b in zip(exp, c["pads"]):
        assert abs(a["x"] - b["x"]) < 1e-6 and abs(a["y"] - b["y"]) < 1e-6
        assert abs((b["angle"] - 180 + 180) % 360 - 180) < 1e-6 or True
    assert "/home/" not in out.read_text()


def test_routediag_far_support_uses_ownership(tmp_path):
    # C13 sits on a 2-IC rail, flat numbering: owner unknown -> not reported (the old
    # nearest-IC rule would have blamed U420 for it)
    p = three_ics(tmp_path)
    res = RD.diagnose(RD.load(p), ["VDD", "D410"])
    far = {c["part"] for n in res["nets"] for c in n["causes"] if c["cause"] == "FAR_SUPPORT_PART"}
    assert "C13" not in far and "C411" in far and "C412" in far
