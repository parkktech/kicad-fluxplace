"""Incremental placement on a routed board (w74-incplace): new off-board parts are seated in free space, every other byte
stays, the stages that would re-place or re-lay copper are skipped, the open nets are routed, a locked part never moves."""
import math
import os
import re
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as g  # noqa: E402
from golden import harness as H  # noqa: E402

pcbnew = pytest.importorskip("pcbnew")
from fluxplace import incplace as IP, routediag as RD, constraints as C, geom as GM  # noqa: E402

CONS = '[placement]\nsides = "front"\nfixed = ["R1","R2","J1"]\n'
TRACK = '(segment (start 10.5 10) (end 19.5 10) (width 0.2) (layer "F.Cu") (net "B"))\n'


def routed(tmp, cons=CONS, extra="", outline=(0, 0, 40, 30), existing=None):
    """R1 - R2 - J1 locked and routed (net B), + 3 new parts off the board on a new net N1 (and nets A, B, C of the old ones)."""
    old = existing or (g.two("R1", 10, 10, "A", "B", locked=True) + g.two("R2", 20, 10, "B", "C", locked=True)
                       + g.fp("J1", 30, 20, [g.pad("1", 0, 0, "A", 0.8, 0.8), g.pad("2", 2, 0, "C", 0.8, 0.8), g.pad("3", 4, 0, "", 0.8, 0.8)], locked=True))
    new = g.two("R3", 60, 10, "C", "N1") + g.two("R4", 60, 15, "N1", "A") + g.two("R5", 60, 20, "N1", "B")
    p = H.write_board(tmp, old + TRACK + new + extra, outline=outline)
    cp = tmp / "cons.toml"
    cp.write_text(cons)
    return p, str(cp)


def blocks(path):
    """{ref: footprint text} and the text without any footprint (tracks, zones, outline ...)."""
    t = open(path).read()
    fps = {m.group(1): m.group(0) for m in re.finditer(r'\(footprint "X".*?\(property "Reference" "(\w+)"\).*?\)\)\n', t, re.S)}
    rest = t
    for b in fps.values():
        rest = rest.replace(b, "")
    return fps, rest


def test_detect(tmp_path):
    p, cp = routed(tmp_path)
    B, cons = RD.load(p), C.load(cp)
    d = IP.detect(B, cons)
    assert d["incremental"] and d["free"] == ["R3", "R4", "R5"] and not d["drift"]
    q, cq = routed(tmp_path / "b", extra=g.two("R6", 30, 5, "A", "C"))
    d2 = IP.detect(RD.load(q), C.load(cq))
    assert not d2["incremental"] and "R6" in d2["why"]                                       # an unlocked part on the board: not this case
    r, cr = routed(tmp_path / "c")
    txt = open(r).read().replace(TRACK, "")
    open(r, "w").write(txt)
    assert not IP.detect(RD.load(r), C.load(cr))["incremental"]                            # no copper: the normal floorplan


def test_place_seats_new_parts_and_changes_nothing_else(tmp_path):
    p, cp = routed(tmp_path)
    out = str(tmp_path / "out.kicad_pcb")
    moves, _back = IP.place(p, out, C.load(cp))
    assert set(moves) == {"R3", "R4", "R5"}
    f0, rest0 = blocks(p)
    f1, rest1 = blocks(out)
    assert rest0 == rest1                                                                   # tracks, zones, outline, header: byte-identical
    assert all(f0[r] == f1[r] for r in ("R1", "R2", "J1"))                                  # existing footprints: byte-identical
    B = RD.load(out)
    mine = ("R3", "R4", "R5")
    for r in mine:
        assert GM.in_outline(B, B["parts"][r]["court"], 0.3), r
    for i, a in enumerate(B["parts"]):
        for b in list(B["parts"])[i + 1:]:
            assert not GM.overlap(B["parts"][a]["court"], B["parts"][b]["court"]), (a, b)
    for r in mine:                                                                         # no pad on the existing track
        for q in B["parts"][r]["pads"]:
            rect = (q["x"] - q["ex"] - 0.2, q["y"] - q["ey"] - 0.2, q["x"] + q["ex"] + 0.2, q["y"] + q["ey"] + 0.2)
            assert not GM.seg_hits_rect((10.5, 10), (19.5, 10), rect), r
    near = min(abs(B["parts"]["R3"]["pos"][0] - B["parts"][x]["pos"][0]) for x in ("R2", "J1"))
    assert near < 15                                                                        # seated by its partners, not at the board corner


def test_intent_near_pulls(tmp_path):
    cons = CONS + '[[intent.near]]\nrefs = ["R3"]\nto = "J1"\nmax_mm = 6\nhard = false\n'
    p, cp = routed(tmp_path, cons=cons)
    out = str(tmp_path / "out.kicad_pcb")
    IP.place(p, out, C.load(cp))
    B = RD.load(out)
    assert math.dist(B["parts"]["R3"]["pos"], B["parts"]["J1"]["pos"]) < 9


def test_refuses_when_a_locked_part_would_move(tmp_path):
    p, cp = routed(tmp_path, cons=CONS + '[lock.at]\nR1 = [11, 10]\n')                      # the row disagrees with the locked part
    with pytest.raises(IP.IncError, match=r"R1.*would move|would move.*R1"):
        IP.place(p, str(tmp_path / "o.kicad_pcb"), C.load(cp))
    from fluxplace import pipeline as P

    class A:
        constraints = cp
    with pytest.raises(SystemExit, match="REFUSED.*lock.at"):
        P._apply_lock_at(A, str(tmp_path / "W"), p)


def test_refuses_when_no_seat_without_moving_a_locked_part(tmp_path):
    old = (g.fp("R1", 3, 4, [g.pad("1", -0.5, 0, "A"), g.pad("2", 0.5, 0, "B")], locked=True, court=(-2, -3, 2, 3))
           + g.fp("R2", 9, 4, [g.pad("1", -0.5, 0, "B"), g.pad("2", 0.5, 0, "C")], locked=True, court=(-2, -3, 2, 3)))
    p, cp = routed(tmp_path, existing=old, outline=(0, 0, 12, 8), cons='[placement]\nsides = "front"\nfixed = ["R1","R2"]\n')
    txt = open(p).read().replace(TRACK, '(segment (start 3.5 4) (end 8.5 4) (width 0.2) (layer "F.Cu") (net "B"))\n')
    txt = re.sub(r'(\(footprint "X" \(layer "F.Cu"\) \(at 60 10 0.0\) \(property "Reference" "R3"\))', r'\1 (fp_rect (start -3 -3) (end 3 3) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))', txt)
    open(p, "w").write(txt)
    before = open(p).read()
    with pytest.raises(IP.IncError, match=r"no legal seat for R3.*R[12]"):
        IP.place(p, str(tmp_path / "o.kicad_pcb"), C.load(cp))
    assert open(p).read() == before and not os.path.exists(tmp_path / "o.kicad_pcb")


@pytest.mark.skipif(not shutil.which("kicad-cli"), reason="the stage gates measure with kicad-cli")
def test_pipeline_floorplan_to_review(tmp_path):
    """The whole path: --from floorplan --to review on the routed board. The placement-only stages are skipped, existing parts and
    tracks keep their pose and geometry, the new net is routed (no open left), review runs."""
    import json
    import cli
    from fluxplace import pipeline as P, boardwrite as BW
    p, cp = routed(tmp_path)
    work = str(tmp_path / "W")
    a = cli.build_parser().parse_args(["pipeline", p, "--flow", "constructive", "--from", "floorplan", "--to", "review",
                                       "--workdir", work, "--constraints", cp])
    P.run(a)
    st = json.load(open(os.path.join(work, "state.json")))
    v = {k: x["verdict"] for k, x in st["stages"].items()}
    assert v["floorplan"] == "pass" and st.get("incremental")
    assert all(v[k] == "na" for k in IP.SKIP_STAGES if k != "modules")      # modules runs before floorplan, outside this range
    assert v["route"] == "pass" and "review" in v, v
    final = st["stages"]["silk"].get("board") or st["board"]
    b0, b1 = pcbnew.LoadBoard(p), pcbnew.LoadBoard(final)
    b1.BuildConnectivity()
    assert b1.GetConnectivity().GetUnconnectedCount(True) == 0
    pos = lambda b: {f.GetReference(): (f.GetPosition().x, f.GetPosition().y, round(f.GetOrientationDegrees(), 3)) for f in b.GetFootprints()}
    p0, p1 = pos(b0), pos(b1)
    assert all(p1[r] == p0[r] for r in ("R1", "R2", "J1"))
    assert set(BW.net_items(p)["B"]) <= set(BW.net_items(final)["B"])                          # the shipped track is still there, same geometry


def _packed(tmp, cons):
    """12 x 8 board whose front is filled by two locked parts; the new R3 has a 6 x 6 courtyard that fits nowhere on the front."""
    old = (g.fp("R1", 3, 4, [g.pad("1", -0.5, 0, "A"), g.pad("2", 0.5, 0, "B")], locked=True, court=(-2, -3, 2, 3))
           + g.fp("R2", 9, 4, [g.pad("1", -0.5, 0, "B"), g.pad("2", 0.5, 0, "C")], locked=True, court=(-2, -3, 2, 3)))
    p, cp = routed(tmp, existing=old, outline=(0, 0, 12, 8), cons=cons)
    txt = open(p).read().replace(TRACK, '(segment (start 3.5 4) (end 8.5 4) (width 0.2) (layer "F.Cu") (net "B"))\n')
    txt = re.sub(r'(\(footprint "X" \(layer "F.Cu"\) \(at 60 10 0.0\) \(property "Reference" "R3"\))', r'\1 (fp_rect (start -2.5 -2.5) (end 2.5 2.5) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))', txt)
    open(p, "w").write(txt)
    return p, cp


def test_back_side_seat_when_the_policy_allows(tmp_path):
    p, cp = _packed(tmp_path, '[placement]\nsides = "both"\nfixed = ["R1","R2"]\n[placement.heights]\n"R*" = 0.6\n')
    out = str(tmp_path / "o.kicad_pcb")
    moves, back = IP.place(p, out, C.load(cp))
    assert "R3" in back
    f0, rest0 = blocks(p)
    f1, rest1 = blocks(out)
    assert rest0 == rest1 and f0["R1"] == f1["R1"] and f0["R2"] == f1["R2"]
    B = RD.load(out)
    assert B["parts"]["R3"]["side"] == "B.Cu" and GM.in_outline(B, B["parts"]["R3"]["court"], 0.3)
    assert all(q["layers"] == {"B.Cu"} for q in B["parts"]["R3"]["pads"])


def test_back_side_refused_by_the_front_policy(tmp_path):
    p, cp = _packed(tmp_path, '[placement]\nsides = "front"\nfixed = ["R1","R2"]\n')
    with pytest.raises(IP.IncError, match=r"no legal seat for R3.*back side not allowed"):
        IP.place(p, str(tmp_path / "o.kicad_pcb"), C.load(cp))


@pytest.mark.skipif(not shutil.which("kicad-cli"), reason="the stage gates measure with kicad-cli")
def test_swapped_connector_pad_on_foreign_copper_is_stripped_and_routed(tmp_path):
    """J1 was swapped in place (its third pin is new, net N9) and a shipped track of net B ends on that pin: the pipeline hands that copper
    to the re-net strip; the rest of the shipped copper stays and the board ends fully connected."""
    import json
    import cli
    from fluxplace import pipeline as P
    p, cp = routed(tmp_path)
    txt = open(p).read().replace('(pad "3" smd rect (at 4 0 0.0) (size 0.8 0.8) (layers "F.Cu"))', '(pad "3" smd rect (at 4 0 0.0) (size 0.8 0.8) (layers "F.Cu") (net "N9"))')
    txt = txt.replace(TRACK, TRACK + '(segment (start 34 17) (end 34 20) (width 0.2) (layer "F.Cu") (net "B"))\n')
    txt = txt.replace('(pad "1" smd rect (at -0.5 0 0.0) (size 0.5 0.5) (layers "F.Cu") (net "N1")))\n(footprint "X" (layer "F.Cu") (at 60 15',
                      '(pad "1" smd rect (at -0.5 0 0.0) (size 0.5 0.5) (layers "F.Cu") (net "N9")))\n(footprint "X" (layer "F.Cu") (at 60 15')
    open(p, "w").write(txt)
    assert 'net "N9"' in txt.split('"J1"')[1].split("(footprint")[0]
    work = str(tmp_path / "W")
    a = cli.build_parser().parse_args(["pipeline", p, "--flow", "constructive", "--from", "floorplan", "--to", "route",
                                       "--workdir", work, "--constraints", cp])
    P.run(a)
    st = json.load(open(os.path.join(work, "state.json")))
    assert st.get("renet") and "B" in st["renet"]["nets"] and any("J1" in x for x in st["renet"]["pads"])
    b = pcbnew.LoadBoard(st["board"])
    b.BuildConnectivity()
    assert b.GetConnectivity().GetUnconnectedCount(True) == 0


def test_route_layers_come_from_the_board_not_a_fixed_stack(tmp_path):
    """Ledger 2026-11-27: the pipeline's --layers default was a 6-layer signal set; on this 4-copper-layer board the router was handed
    In3.Cu and laid a segment there whenever a net needed a layer change (about 1 run in 3): KRT graded it routed, the net was open."""
    import cli
    from fluxplace import pipeline as P
    p, cp = routed(tmp_path)
    a = cli.build_parser().parse_args(["pipeline", p, "--workdir", str(tmp_path / "W"), "--constraints", cp])
    assert a.layers is None                                              # no project's stack is baked into the parser
    cu = RD.load(p)["copper"]
    lay = P.route_layers(p, None, C.load(cp))
    assert lay and set(lay) <= set(cu) and {cu[0], cu[-1]} <= set(lay)
    assert P.route_layers(p, [cu[-1], cu[0]], None) == [cu[0], cu[-1]]    # an explicit list is kept, in stack order
    with pytest.raises(SystemExit, match="not a copper layer"):
        P.route_layers(p, ["F.Cu", "In7.Cu"], None)
