"""`pairs`: differential pairs laid by construction on synthetic boards (pure text; kicad-cli DRC when it is installed)."""
import json
import math
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import pairgeo as G
from fluxplace import pairs as PR
from fluxplace import routediag as RD

W, GAP, CLR = 0.15, 0.2, 0.127


def duo(ref, x, y, p, n, pitch=0.5, layer="F.Cu", w=0.3, h=0.8):
    pads = [PB.pad("1", 0, 0, p, w, h), PB.pad("2", pitch, 0, n, w, h)]
    return PB.fp(ref, x, y, [q.replace('(layers "F.Cu")', f'(layers "{layer}")') for q in pads])


def lay(tmp, body, cons=None):
    b = PB.write(tmp, body)
    out = str(tmp / "o.kicad_pcb")
    rep = PR.run(b, cons or {}, None, out)
    return rep, RD.load(out), open(out).read()


def clean(B, nets):
    """smallest exact gap of the laid copper of `nets` to every foreign item (an independent check of the result)."""
    I = G.Items.of_board(B)
    worst = 9.0
    for s in B["segs"]:
        if s["net"] in nets:
            worst = min(worst, I.gap_seg(s["a"], s["b"], s["w"], s["layer"], s["net"], 0.5)[0])
    for v in B["vias"]:
        if v["net"] in nets:
            worst = min(worst, I.gap_via(v["x"], v["y"], v["d"], v["net"], 0.5)[0])
    return worst


def coupled_share(B, p, n, pitch):
    """fraction of the P leg's length that runs parallel to an N segment at the pair pitch."""
    tot = cov = 0.0
    N = [s for s in B["segs"] if s["net"] == n]
    for s in B["segs"]:
        if s["net"] != p:
            continue
        L = math.hypot(s["b"][0] - s["a"][0], s["b"][1] - s["a"][1])
        tot += L
        mid = ((s["a"][0] + s["b"][0]) / 2, (s["a"][1] + s["b"][1]) / 2)
        if any(q["layer"] == s["layer"] and abs(G.pt_seg(mid, q["a"], q["b"]) - pitch) < 0.01 for q in N):
            cov += L
    return cov / max(tot, 1e-9)


def test_straight_pair_is_coupled_locked_and_matched(tmp_path):
    rep, B, txt = lay(tmp_path, duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 20, 40, "D_P", "D_N"))
    assert rep["verdict"] == "PAIRS_OK" and rep["complete"] == rep["total"] == 1
    r = rep["pairs"][0]
    assert r["vias"] == 0 and r["layers"] == ["F.Cu"] and r["skew"] <= 0.05
    assert 19.0 < r["length"][0] < 21.0
    segs = [s for s in B["segs"] if s["net"] in ("D_P", "D_N")]
    assert segs and all(s["locked"] and abs(s["w"] - W) < 1e-6 for s in segs)
    assert coupled_share(B, "D_P", "D_N", W + GAP) > 0.9
    assert clean(B, {"D_P", "D_N"}) >= CLR


def test_bend_round_an_obstacle(tmp_path):
    body = duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 40, 30, "D_P", "D_N") + PB.wall("W1", 30, 25, 12)
    rep, B, _ = lay(tmp_path, body)
    r = rep["pairs"][0]
    assert r["status"] == "complete" and r["vias"] == 0
    assert r["skew"] <= r["limit"] and r["skew"] <= 0.3            # corners are compensated with a bump
    assert clean(B, {"D_P", "D_N"}) >= CLR
    assert coupled_share(B, "D_P", "D_N", W + GAP) > 0.8
    # every corner is 45 degrees
    for net in ("D_P", "D_N"):
        ss = [s for s in B["segs"] if s["net"] == net]
        for s in ss:
            dx, dy = abs(s["b"][0] - s["a"][0]), abs(s["b"][1] - s["a"][1])
            assert min(dx, dy) < 2e-3 or abs(dx - dy) < 2e-3


def test_one_layer_change_is_a_matched_via_pair(tmp_path):
    body = duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 30, 30, "D_P", "D_N", layer="B.Cu")
    rep, B, _ = lay(tmp_path, body)
    r = rep["pairs"][0]
    assert r["status"] == "complete" and r["vias"] == 1 and r["layers"] == ["F.Cu", "B.Cu"]
    vp = [v for v in B["vias"] if v["net"] == "D_P"]
    vn = [v for v in B["vias"] if v["net"] == "D_N"]
    assert len(vp) == len(vn) == 1 and vp[0]["locked"]
    d = math.hypot(vp[0]["x"] - vn[0]["x"], vp[0]["y"] - vn[0]["y"])
    assert vp[0]["d"] + CLR <= d < 1.2                             # side by side, a clearance apart
    assert r["skew"] <= 0.3 and clean(B, {"D_P", "D_N"}) >= CLR


def test_series_caps_make_two_sections(tmp_path):
    # driver -> AC caps (C1 on P, C2 on N, side by side) -> receiver: TX_P/TX_N and RX_P/RX_N are separate pair sections
    caps = PB.fp("C1", 30, 20, [PB.pad("1", 0, -0.5, "TX_P", 0.5, 0.5), PB.pad("2", 0, 0.5, "RX_P", 0.5, 0.5)]) + \
        PB.fp("C2", 31, 20, [PB.pad("1", 0, -0.5, "TX_N", 0.5, 0.5), PB.pad("2", 0, 0.5, "RX_N", 0.5, 0.5)])
    body = duo("U1", 30, 10, "TX_P", "TX_N") + caps + duo("J1", 30, 32, "RX_P", "RX_N")
    rep, B, _ = lay(tmp_path, body)
    assert rep["complete"] == rep["total"] == 2
    for r in rep["pairs"]:
        assert r["vias"] == 0 and r["skew"] <= 0.3
        assert [s["from"] for s in r["sections"]] and all(s["ok"] for s in r["sections"])
    assert clean(B, {"TX_P", "TX_N", "RX_P", "RX_N"}) >= CLR


def test_blocked_pair_is_reported_not_dropped(tmp_path):
    cage = PB.keepout(17, 16.5, 23.5, 17) + PB.keepout(17, 23, 23.5, 23.5) + PB.keepout(17, 16.5, 17.5, 23.5) + PB.keepout(23, 16.5, 23.5, 23.5)
    rep, B, txt = lay(tmp_path, duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 60, 40, "D_P", "D_N") + cage)
    assert rep["verdict"] == "PAIRS_WARN" and rep["complete"] == 0
    r = rep["pairs"][0]
    assert r["status"] != "complete" and r["why"] and r["blockers"]
    assert "closest approach" in r["why"]
    assert not [s for s in B["segs"] if s["net"] in ("D_P", "D_N")]            # nothing half laid
    assert "PAIR_NOT_LAID" in PR.format_text(rep)


def test_plan_only_writes_nothing_and_returns_corridors(tmp_path):
    b = PB.write(tmp_path, duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 40, 30, "D_P", "D_N"))
    rep = PR.run(b, {}, None, None, plan_only=True)
    assert rep["plan_only"] and rep["complete"] == 1
    c = rep["pairs"][0]["sections"][0]["corridor"]
    assert c and c[0]["layer"] == "F.Cu" and len(c[0]["pts"]) >= 2 and c[0]["width"] > 2 * W + GAP


def test_width_and_gap_come_from_the_project_net_class(tmp_path):
    b = PB.write(tmp_path, duo("U1", 20, 20, "USB_DP", "USB_DM", pitch=0.8) + duo("J1", 20, 40, "USB_DP", "USB_DM", pitch=0.8))
    json.dump({"net_settings": {"classes": [
        {"name": "Default", "clearance": 0.15, "track_width": 0.2, "diff_pair_width": 0.2, "diff_pair_gap": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
        {"name": "USB90", "clearance": 0.15, "track_width": 0.2, "diff_pair_width": 0.26, "diff_pair_gap": 0.18, "priority": 0}],
        "netclass_patterns": [{"netclass": "USB90", "pattern": "USB_*"}]}}, open(str(tmp_path / "b.kicad_pro"), "w"))
    out = str(tmp_path / "o.kicad_pcb")
    rep = PR.run(b, {}, None, out)
    B = RD.load(out)
    r = rep["pairs"][0]
    assert r["status"] == "complete" and r["class"] == "USB90" and r["width"] == 0.26 and r["gap"] == 0.18
    assert all(abs(s["w"] - 0.26) < 1e-6 for s in B["segs"] if s["net"].startswith("USB_"))
    assert coupled_share(B, "USB_DP", "USB_DM", 0.26 + 0.18) > 0.85
    assert clean(B, {"USB_DP", "USB_DM"}) >= 0.15


@pytest.mark.skipif(not shutil.which("kicad-cli"), reason="kicad-cli not installed")
def test_laid_copper_passes_kicad_drc(tmp_path):
    body = duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 40, 30, "D_P", "D_N", layer="B.Cu") + PB.wall("W1", 30, 25, 12)
    b = PB.write(tmp_path, body)
    json.dump({"board": {"design_settings": {"rules": {"min_clearance": 0.127}}}, "net_settings": {"classes": [
        {"name": "Default", "clearance": 0.15, "track_width": 0.15, "diff_pair_width": 0.15, "diff_pair_gap": 0.2, "via_diameter": 0.45,
         "via_drill": 0.2}]}}, open(str(tmp_path / "b.kicad_pro"), "w"))            # the DRC and the lay use the same project clearance
    out = str(tmp_path / "o.kicad_pcb")
    assert PR.run(b, {}, None, out)["complete"] == 1
    rp = str(tmp_path / "drc.json")
    subprocess.run(["kicad-cli", "pcb", "drc", "--severity-all", "--format", "json", "--output", rp, out], capture_output=True, timeout=120)
    d = json.load(open(rp))
    bad = [v for v in d.get("violations", []) if v["type"] in ("clearance", "shorting_items", "tracks_crossing", "hole_clearance", "track_dangling")]
    assert not bad, bad[:3]
    assert not [u for u in d.get("unconnected_items", []) if "D_" in json.dumps(u)]


def test_pipeline_stage_sits_between_fanout_and_plan_route_and_route_skips_laid_pairs(tmp_path):
    import types
    from fluxplace import pipeline as PP
    from fluxplace import routeconn as RC
    i = PP.CONSTRUCTIVE.index("pairs")
    assert PP.CONSTRUCTIVE[i - 1] == "fanout" and PP.CONSTRUCTIVE[i + 1] == "plan-route" and "pairs" not in PP.STAGES
    body = duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 20, 40, "D_P", "D_N") + \
        PB.fp("R1", 60, 20, [PB.pad("1", 0, 0, "SIG")]) + PB.fp("R2", 60, 30, [PB.pad("1", 0, 0, "SIG")])
    b = PB.write(tmp_path, body)
    c = types.SimpleNamespace(cons={}, spec=None, dir=lambda n, name: (os.makedirs(str(tmp_path / f"{n}-{name}"), exist_ok=True), str(tmp_path / f"{n}-{name}"))[1])
    res = PP.FUNCS["pairs"](c, 5, b)
    assert res["verdict"] == "pass" and res["board"].endswith(os.path.join("5-pairs", "board.kicad_pcb")) and "1/1 complete" in res["line"]
    assert os.path.exists(os.path.join(os.path.dirname(res["board"]), "board.pairs.json"))
    # the route stage's work list is the nets with open connections: the laid pair is not in it, and its copper is locked
    B = RD.load(res["board"])
    todo = sorted(n for n, k in RC.text_open(B).items() if k)
    assert todo == ["SIG"]
    assert all(s["locked"] for s in B["segs"] if s["net"] in ("D_P", "D_N"))
    off = types.SimpleNamespace(cons={"pairlay": {"enabled": False}}, spec=None, dir=c.dir)
    assert PP.FUNCS["pairs"](off, 5, b)["verdict"] == "skipped"


def test_unnamed_drilled_hole_is_an_obstacle(tmp_path):
    hole = '(footprint "H" (layer "F.Cu") (at 20.25 30) (property "Reference" "H1") (pad "" np_thru_hole circle (at 0 0) (size 2 2) (drill 2) (layers "*.Cu")))\n'
    rep, B, _ = lay(tmp_path, duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 20, 40, "D_P", "D_N") + hole)
    assert rep["complete"] == 1
    d = min(G.pt_seg((20.25, 30), s["a"], s["b"]) - s["w"] / 2 for s in B["segs"] if s["net"] in ("D_P", "D_N"))
    assert d >= 1.0 + CLR                                   # copper edge to hole edge


# ---------------------------------------------------------------- round 2: inner layers, via exits, negotiated order, limits
import pcbgen as PG


def gduo(ref, x, y, p, n, pitch=0.5, **kw):
    return PG.fp(ref, x, y, [PG.pad("1", 0, 0, p, 0.3, 0.8), PG.pad("2", pitch, 0, n, 0.3, 0.8)], **kw)


def test_inner_signal_layer_is_used_when_the_outer_layers_are_blocked(tmp_path):
    cage = PB.keepout(15, 15, 25.5, 16) + PB.keepout(15, 24, 25.5, 24.5) + PB.keepout(15, 15, 15.5, 24.5) + PB.keepout(25, 15, 25.5, 24.5)
    b = PG.board(tmp_path, gduo("U1", 20, 20, "D_P", "D_N") + gduo("J1", 50, 40, "D_P", "D_N") + cage)      # F / In1 / B; the rule areas are on F and B
    out = str(tmp_path / "o.kicad_pcb")
    rep = PR.run(b, {}, None, out)
    r = rep["pairs"][0]
    assert rep["signal_layers"] == ["F.Cu", "In1.Cu", "B.Cu"]
    assert r["status"] == "complete" and r["layers"] == ["F.Cu", "In1.Cu", "F.Cu"] and r["vias"] == 2 and r["skew"] <= 0.3
    # with nothing in the way the pair stays on the outer layer (an inner layer costs more)
    b2 = PG.board(tmp_path, gduo("U1", 20, 20, "D_P", "D_N") + gduo("J1", 50, 40, "D_P", "D_N"), name="c.kicad_pcb")
    assert PR.run(b2, {}, None, str(tmp_path / "o2.kicad_pcb"))["pairs"][0]["layers"] == ["F.Cu"]


def test_full_board_zone_makes_an_inner_layer_a_plane(tmp_path):
    z = ('(zone (net 1) (net_name "GND") (layer "In1.Cu") (hatch edge 0.5) (polygon (pts (xy 0 0) (xy 100 0) (xy 100 80) (xy 0 80))))\n')
    part = ('(zone (net 2) (net_name "VCC") (layer "In1.Cu") (hatch edge 0.5) (polygon (pts (xy 0 0) (xy 60 0) (xy 60 80) (xy 0 80))))\n')
    body = gduo("U1", 20, 20, "D_P", "D_N") + gduo("J1", 50, 40, "D_P", "D_N")
    assert PR.pair_layers(RD.load(PG.board(tmp_path, body + z)), {}) == (["F.Cu", "B.Cu"], ["In1.Cu"])
    assert PR.pair_layers(RD.load(PG.board(tmp_path, body + part, name="p.kicad_pcb")), {})[0] == ["F.Cu", "In1.Cu", "B.Cu"]       # a partial fill: still a signal layer
    assert PR.pair_layers(RD.load(PG.board(tmp_path, body + part, name="q.kicad_pcb")), {"preroute": {"plane_layers": ["In1.Cu"]}})[1] == ["In1.Cu"]


def test_inner_layer_width_follows_the_impedance_target(tmp_path):
    stackup = [{"type": "copper", "name": "F.Cu", "thickness": 0.035}, {"type": "prepreg", "thickness": 0.1, "epsilon_r": 4.1},
               {"type": "copper", "name": "In1.Cu", "thickness": 0.015}, {"type": "core", "thickness": 0.2, "epsilon_r": 4.5},
               {"type": "copper", "name": "In2.Cu", "thickness": 0.015}, {"type": "prepreg", "thickness": 0.6, "epsilon_r": 4.4},
               {"type": "copper", "name": "B.Cu", "thickness": 0.035}]
    cu = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    rule = {"w": 0.15, "gap": 0.25, "target": 100}
    geo = PR.layer_geometry(rule, ["F.Cu", "In2.Cu", "B.Cu"], ["In1.Cu"], cu, stackup, 0.1)
    assert geo["F.Cu"] == (0.15, 0.25, "class") and geo["B.Cu"][2] == "class"
    assert geo["In2.Cu"][0] >= 0.15 and geo["In2.Cu"][1] == 0.25 and "100 ohm" in geo["In2.Cu"][2]
    # no target, or no stackup: the class geometry everywhere
    assert PR.layer_geometry(dict(rule, target=None), ["F.Cu", "In2.Cu"], ["In1.Cu"], cu, stackup, 0.1)["In2.Cu"][2] == "class"
    assert PR.layer_geometry(rule, ["F.Cu", "In2.Cu"], ["In1.Cu"], cu, [], 0.1)["In2.Cu"][2] == "class"


def _stub(net, x, y, vx, vy):
    return (f'(segment (start {x} {y}) (end {vx} {vy}) (width 0.15) (locked yes) (layer "F.Cu") (net "{net}"))\n'
            f'(via (at {vx} {vy}) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (locked yes) (net "{net}"))\n')


def test_pair_leaves_through_its_fan_out_via_pair(tmp_path):
    # U1's pins are boxed in on F.Cu; the fan-out stage left a stub + via per leg inside the box
    bars = "".join(PB.fp(f"W{k}", x, y, [PB.pad("1", 0, 0, f"X{k}", w, h)]) for k, (x, y, w, h) in enumerate(
        [(20.25, 19.15, 4.5, 0.3), (20.25, 20.85, 4.5, 0.3), (18.2, 20.0, 0.3, 2.0), (22.3, 20.0, 0.3, 2.0)]))
    fan = _stub("D_P", 20.0, 20.0, 18.8, 20.0) + _stub("D_N", 20.5, 20.0, 21.7, 20.0)
    body = duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 50, 40, "D_P", "D_N", layer="B.Cu") + bars + fan
    rep, B, txt = lay(tmp_path, body)
    r = rep["pairs"][0]
    assert r["status"] == "complete" and r["via_exit"] == ["U1@via"] and r["skew"] <= 0.3
    kept = {(v["x"], v["y"]) for v in B["vias"] if v["net"] in ("D_P", "D_N")}
    assert {(18.8, 20.0), (21.7, 20.0)} <= kept                    # the fan-out vias and stubs stay: the pair leaves through them
    assert sum(1 for s in B["segs"] if s["net"] == "D_P" and s["layer"] == "F.Cu" and s["a"][1] == s["b"][1] == 20.0) >= 1
    assert clean(B, {"D_P", "D_N"}) >= CLR
    off = PR.run(PB.write(tmp_path, body, name="x.kicad_pcb"), {"pairlay": {"via_exit": False, "dogbone": False}}, None, str(tmp_path / "x.o.kicad_pcb"))
    assert off["complete"] == 0 and off["pairs"][0]["why"]


def test_boxed_in_pins_get_a_new_dog_bone_via_pair(tmp_path):
    # no fan-out copper yet (pairs before fan-out): the stage makes its own matched via pair next to the pads
    bars = PB.fp("W1", 20.25, 19.15, [PB.pad("1", 0, 0, "X1", 2.4, 0.3)]) + PB.fp("W2", 20.25, 20.85, [PB.pad("1", 0, 0, "X2", 2.4, 0.3)]) + \
        PB.fp("W3", 20.25, 17.2, [PB.pad("1", 0, 0, "X3", 9.0, 0.3)]) + PB.fp("W4", 20.25, 22.8, [PB.pad("1", 0, 0, "X4", 9.0, 0.3)]) + \
        PB.fp("W5", 15.6, 20.0, [PB.pad("1", 0, 0, "X5", 0.3, 5.9)]) + PB.fp("W6", 24.9, 20.0, [PB.pad("1", 0, 0, "X6", 0.3, 5.9)])
    body = duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 50, 40, "D_P", "D_N", layer="B.Cu") + bars
    rep, B, _ = lay(tmp_path, body, {"pairlay": {"ring_mm": 0.6}})
    r = rep["pairs"][0]
    assert r["status"] == "complete" and clean(B, {"D_P", "D_N"}) >= CLR and r["skew"] <= r["limit"]
    vp = [v for v in B["vias"] if v["net"] == "D_P"]
    vn = [v for v in B["vias"] if v["net"] == "D_N"]
    assert len(vp) == len(vn) >= 1 and all(v["locked"] for v in vp + vn)


def test_terminal_with_pads_on_two_layers_gets_a_via_on_the_odd_pad(tmp_path):
    odd = PB.fp("FB1", 20, 20, [PB.pad("1", 0, 0, "D_P", 0.6, 0.6)]) + \
        PB.fp("FB2", 21.2, 20, [PB.pad("1", 0, 0, "D_N", 0.6, 0.6).replace('(layers "F.Cu")', '(layers "B.Cu")')])
    rep, B, _ = lay(tmp_path, odd + duo("J1", 20, 40, "D_P", "D_N"))
    r = rep["pairs"][0]
    # connected and clean; the audit still says mismatch (the legs cannot have one layer sequence), so it is not counted complete
    assert r["open"] == [0, 0] and r["status"] == "mismatch" and any("via-on" in x for x in r["via_exit"])
    assert len([v for v in B["vias"] if v["net"] in ("D_P", "D_N")]) >= 1 and clean(B, {"D_P", "D_N"}) >= CLR


def test_negotiation_gives_each_pair_its_corridor(tmp_path):
    # three pairs through one wall with three one-pair gaps: alone, all three pick the middle gap
    cons = {"preroute": {"signal_layers": ["F.Cu"]}, "pairlay": {"workers": 2}}
    wall = PB.keepout(30, 0, 30.5, 34.4) + PB.keepout(30, 35.6, 30.5, 39.4) + PB.keepout(30, 40.6, 30.5, 44.4) + PB.keepout(30, 45.6, 30.5, 80)
    body = wall + "".join(duo(f"U{k}", 20, y, f"A{k}_P", f"A{k}_N") + duo(f"J{k}", 40, y, f"A{k}_P", f"A{k}_N") for k, y in enumerate((38.0, 40.0, 42.0)))
    rep, B, _ = lay(tmp_path, body, cons)
    assert rep["complete"] == 3 and rep["negotiation"]["sections"] == 3 and rep["negotiation"]["passes"] >= 1
    assert not rep["negotiation"]["unresolved"]
    assert clean(B, {n for p in rep["pairs"] for n in p["pair"]}) >= CLR
    seq = PR.run(PB.write(tmp_path, body, name="s.kicad_pcb"), dict(cons, pairlay={"negotiate": False}), None, str(tmp_path / "s.o.kicad_pcb"))
    assert seq["negotiation"] is None


def test_blocked_pair_goes_first_and_the_blocker_is_laid_again(tmp_path):
    # one signal layer; PCIE (laid first by kind) runs straight down in front of the only gap the audio pair can use
    cons = {"preroute": {"signal_layers": ["F.Cu"]}}
    wall = PB.keepout(30, 0, 30.5, 39.4) + PB.keepout(30, 40.6, 30.5, 80) + PB.keepout(30.5, 37, 36.5, 37.5) + PB.keepout(30.5, 42.5, 36.5, 43) + \
        PB.keepout(36, 37.5, 36.5, 42.5)
    body = duo("U1", 29.05, 20, "PCIE_TX_P", "PCIE_TX_N") + duo("U2", 29.05, 60, "PCIE_TX_P", "PCIE_TX_N") + \
        duo("U3", 20, 40, "MIC_P", "MIC_N") + duo("U4", 33.5, 40, "MIC_P", "MIC_N") + wall
    one = PR.run(PB.write(tmp_path, body), dict(cons, pairlay={"passes": 0, "negotiate": False}), None, str(tmp_path / "a.kicad_pcb"))
    assert one["complete"] == 1 and one["order"][0].startswith("PCIE")
    bad = [r for r in one["pairs"] if r["status"] != "complete"][0]
    assert bad["pair"][0].startswith("MIC") and any("PCIE_TX" in b for b in bad["blockers"])
    two = PR.run(PB.write(tmp_path, body, name="b2.kicad_pcb"), dict(cons, pairlay={"passes": 3, "negotiate": False}), None, str(tmp_path / "b.kicad_pcb"))
    assert two["complete"] == 2 and two["passes"] >= 1 and two["order"][0].startswith("MIC")
    neg = PR.run(PB.write(tmp_path, body, name="b3.kicad_pcb"), cons, None, str(tmp_path / "c.kicad_pcb"))        # negotiation: same result, no re-lay
    assert neg["complete"] == 2 and neg["passes"] == 0 and neg["negotiation"]["passes"] >= 1
    assert clean(RD.load(str(tmp_path / "b.kicad_pcb")), {"PCIE_TX_P", "PCIE_TX_N", "MIC_P", "MIC_N"}) >= CLR


def test_skew_limit_per_family_then_kind(tmp_path):
    from fluxplace import constraints as C
    cons = {"pairs": {"ETH_": {"skew_mm": 0.5}}, "si": {"default_skew_mm": 1.0}}
    assert C.skew_limit_source(cons, "ETH_L0_N") == (0.5, "[pairs.ETH_] skew_mm")
    assert C.skew_limit_source(cons, "PCIE_RX_N") == (1.0, "[si] default_skew_mm")
    assert C.skew_limit_source(cons, "MIC_SEC_N")[0] == 10.0 and "low-speed" in C.skew_limit_source(cons, "MIC_SEC_N")[1]
    assert C.skew_limit_mm({"si": {"audio_skew_mm": 4.0}}, "SPK_COMM_N") == 4.0
    assert C.skew_limit_mm({"si": {"skew_mm_by_kind": {"usb2": 2.5}}}, "USB_DM") == 2.5


def test_pads_far_apart_merge_into_a_coupled_run(tmp_path):
    # P and N land on two parts 6 mm apart at both ends: uncoupled stubs to a merge point, coupled in between
    body = "".join(PB.fp(r, x, y, [PB.pad("1", 0, 0, n, 0.6, 0.6)]) for r, x, y, n in
                   [("R1", 20, 20, "D_P"), ("R2", 26, 20, "D_N"), ("R3", 20, 50, "D_P"), ("R4", 26, 50, "D_N")])
    rep, B, _ = lay(tmp_path, body)
    r = rep["pairs"][0]
    assert r["status"] == "complete" and r["skew"] <= r["limit"] and clean(B, {"D_P", "D_N"}) >= CLR
    assert coupled_share(B, "D_P", "D_N", W + GAP) > 0.5


def test_unpaired_pad_is_a_question_not_a_silent_failure(tmp_path):
    body = duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 20, 40, "D_P", "D_N") + PB.fp("R9", 60, 60, [PB.pad("1", 0, 0, "D_P", 0.6, 0.6)])
    rep, B, _ = lay(tmp_path, body)
    r = rep["pairs"][0]
    assert r["unpaired"] == ["R9.1"] and r["status"] != "complete"
    S = PR.pad_questions(rep, str(tmp_path / "o.kicad_pcb"), {})
    assert [q["id"] for q in S.open] == ["pair-pads:D_N"] and "R9.1" in S.open[0]["found"]
    assert {o["choice"] for o in S.open[0]["options"]} == {"single-ended", "netlist"}
    S2 = PR.pad_questions(rep, str(tmp_path / "o.kicad_pcb"), {"answers": {"pair-pads:D_N": "single-ended"}})
    assert not S2.open and rep["pairs"][0]["answer"] == "single-ended"


def test_fanout_alternative_routes_a_blocked_pin_to_a_via_site(tmp_path):
    from fluxplace import fanalt as FA
    # a pin whose straight ways out are walled by foreign pads on three sides: a routed stub reaches a via site
    body = PB.fp("U1", 20, 20, [PB.pad("1", 0, 0, "SIG", 0.3, 0.8)]) + PB.fp("J1", 60, 60, [PB.pad("1", 0, 0, "SIG", 1, 1)]) + \
        PB.fp("W1", 20, 18.9, [PB.pad("1", 0, 0, "X1", 3.0, 0.4)]) + PB.fp("W2", 18.9, 20, [PB.pad("1", 0, 0, "X2", 0.4, 2.6)]) + \
        PB.fp("W3", 21.1, 20, [PB.pad("1", 0, 0, "X3", 0.4, 2.6)])
    b = PB.write(tmp_path, body)
    res = {"pins": [{"ref": "U1", "pin": "1", "net": "SIG", "need": "signal", "ok": False, "blocker": "W1.1"}],
           "params": {"track": 0.15, "clear": 0.15, "via": 0.45, "drill": 0.2, "via_pad_gap": 0.2, "lane": 1.0}}
    st = FA.rescue(b, res, {}, None)
    assert st == {"tried": 1, "via": 1, "share": 0, "pour": 0, "small-via": 0, "left": 0}
    pin = res["pins"][0]
    assert pin["ok"] and pin["kind"] == "alt-via" and "blocker" not in pin
    B = RD.load(b)
    v = [x for x in B["vias"] if x["net"] == "SIG"]
    assert len(v) == 1 and v[0]["locked"] and v[0]["y"] > 20.4 + 0.2            # outside the pad, with the via-to-pad gap, on the open side
    assert G.length([tuple(q) for q in pin["stub"]]) <= 2.5 + 0.4
    assert clean(B, {"SIG"}) >= 0.15 - 1e-6
    off = {"pins": [{"ref": "U1", "pin": "1", "net": "SIG", "ok": False}], "params": res["params"]}
    assert FA.rescue(b, off, {"escape": {"alternatives": False}}, None)["left"] == 1
