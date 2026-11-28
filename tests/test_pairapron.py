"""Pair escape apron (W16 BT1-3 at the hub: a crystal seated 0.70 mm in front of four USB pairs' pins left three pairs unlaid).
The placement gate fails before any routing, the legaliser keeps bodies out, parts of the pair itself are exempt."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import pcbgen as G
from fluxplace import escreserve as ES
from fluxplace import gapmap as GP
from fluxplace import geom as GM
from fluxplace import legalise as LG
from fluxplace import netclass as NC
from fluxplace import preroute as PR
from fluxplace import readychecks as RC
from fluxplace import routediag as RD

PAIRS2 = ["USB1_DM", "USB1_DP", "USB2_DM", "USB2_DP", "S4", "S5", "S6", "S7"]
PAIRS1 = ["USB1_DM", "USB1_DP", "S2", "S3", "S4", "S5", "S6", "S7"]
EDGE = 40 + 1.3 + 0.35                                    # outer edge of the hub's pin row (pads 0.25 x 0.7 at y + 1.3)


def hub(nets, x=50, y=40):
    """0.5 mm pitch part, one pin row on its bottom edge."""
    n = len(nets)
    pads = [G.pad(str(i + 1), (i - (n - 1) / 2) * 0.5, 1.3, net, 0.25, 0.7) for i, net in enumerate(nets)]
    return G.fp("U1", x, y, pads, court=(-n * 0.25 - 0.5, -1.5, n * 0.25 + 0.5, 1.7), locked=True) + G.two("U2", 20, 20, "USB1_DM", "USB1_DP") \
        + G.two("U3", 20, 25, "USB2_DM", "USB2_DP")


def xtal(room, x=48.75, n1="XI", n2="XO", ref="Y1"):
    """Two-pad part whose pads (0.5 high) start `room` mm in front of the hub's pins, under pins 1-4."""
    return G.two(ref, x, EDGE + room + 0.25, n1, n2, court=(-0.9, -0.45, 0.9, 0.45))


def floors():
    """(clearance, via) the gate uses on these boards, and the apron depth of one / two via rows."""
    from fluxplace import routerules as RR
    R = RR.rules({}, None, B={"track": 0.2, "clearance": 0.2})
    return R["clearance"], R["via"]


CLR, VIA = 0.2, 0.45
D1, D2 = CLR + (VIA + CLR), CLR + 2 * (VIA + CLR)


def findings(tmp, body):
    B = RD.load(G.board(tmp, body))
    return B, PR.pair_escape_blocked(B, NC.classify(B, {}, None), {})


def test_part_in_front_of_two_neighbouring_pairs_fails_the_placement_gate_with_the_move(tmp_path):
    B, f = findings(tmp_path, hub(PAIRS2) + xtal(D2 - 0.6))
    assert (max(B["clearance"], floors()[0]), floors()[1]) == (CLR, VIA)
    assert [x["code"] for x in f] == ["PAIR_ESCAPE_BLOCKED"] and f[0]["level"] == "FAIL" and f[0]["refs"] == ["U1", "Y1"]
    assert f[0]["value"] == 2 and f[0]["pins"] == ["U1.1", "U1.2", "U1.3", "U1.4"]
    need = ES.pair_aprons(B, NC.classify(B, {}, None), CLR, VIA)[0]["depth"]
    assert abs(need - D2) < 1e-6                                   # two pairs in the chain: a via row each
    assert f[0]["moves"] == [{"ref": "Y1", "dir": "S", "mm": 0.6}]
    all_f, card = PR.run(B, {}, None)                              # the gate carries it, and the score counts it
    assert any(x["code"] == "PAIR_ESCAPE_BLOCKED" for x in all_f) and card["counts"]["PAIR_ESCAPE_BLOCKED"] == 1


def test_part_moved_out_of_the_apron_is_clean(tmp_path):
    assert findings(tmp_path, hub(PAIRS2) + xtal(D2 + 0.01))[1] == []


def test_lone_pair_needs_one_via_row(tmp_path):
    assert findings(tmp_path, hub(PAIRS1) + xtal(D1 + 0.05))[1] == []
    B, f = findings(tmp_path, hub(PAIRS1) + xtal(D1 - 0.2))
    assert len(f) == 1 and "1 via row" in f[0]["msg"]
    assert findings(tmp_path, hub(PAIRS2) + xtal(D1 + 0.05))[1] != []               # the same room with a neighbour pair is short


def test_part_of_the_pair_itself_and_a_part_beside_the_pins_are_no_blockers(tmp_path):
    assert findings(tmp_path, hub(PAIRS1) + xtal(0.3, n1="USB1_DM", n2="OUT", ref="R1"))[1] == []      # series part on the pair
    assert findings(tmp_path, hub(PAIRS1) + xtal(0.3, x=51.5))[1] == []                               # in front of plain pins


def test_each_pair_of_a_chain_adds_a_via_row(tmp_path):
    nets = ["USB1_DM", "USB1_DP", "USB2_DM", "USB2_DP", "USB3_DM", "USB3_DP", "S6", "S7"]
    body = hub(nets) + G.two("U4", 20, 30, "USB3_DM", "USB3_DP")
    B = RD.load(G.board(tmp_path, body))
    ap = ES.pair_aprons(B, NC.classify(B, {}, None), CLR, VIA)
    assert [(a["nets"][0], a["rows"]) for a in ap] == [("USB1_DM", 3), ("USB2_DM", 3), ("USB3_DM", 3)]
    assert all(abs(a["depth"] - (CLR + 3 * (VIA + CLR))) < 1e-6 for a in ap)
    assert findings(tmp_path, body + xtal(D2 + 0.05))[1] != [] and findings(tmp_path, body + xtal(CLR + 3 * (VIA + CLR) + 0.01))[1] == []


def test_ready_and_gapmap_own_the_code():
    assert "PAIR_ESCAPE_BLOCKED" in RC.PLACE_CODES and "PAIR_ESCAPE_BLOCKED" in PR.CODES
    assert GP.lookup("PAIR_ESCAPE_BLOCKED") == ("pin-escape", "floorplan")


def test_legaliser_keeps_a_body_out_of_the_apron_and_leaves_the_pairs_own_part(tmp_path):
    body = hub(PAIRS2) + xtal(1.0) + xtal(1.0, x=49.75, n1="USB2_DM", n2="OUT", ref="R1")
    B = RD.load(G.board(tmp_path, body))
    rsv = ES.for_legalise(B, {}, None, movable={"Y1", "R1"})
    ap = [r for r in rsv["rects"] if r["hard"] and r["why"].startswith("pair escape apron of U1")]
    assert len(ap) == 2 and all("R1" in r["allow"] for r in ap if "USB2" in r["why"])
    tg = {r: (*B["parts"][r]["pos"], 0.0) for r in ("Y1", "R1")}
    res, _rep = LG.legalise(B, tg, fixed={"U1"}, reserve=rsv)
    court = GM.moved(B["parts"]["Y1"], *res["Y1"])[1]
    assert not any(GM.overlap(court, r["rect"]) for r in ap)
    usb2 = next(r for r in ap if "USB2" in r["why"])
    assert GM.overlap(GM.moved(B["parts"]["R1"], *res["R1"])[1], usb2["rect"])     # allowed: it stays in its pair's apron
