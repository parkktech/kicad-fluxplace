import json, os, subprocess, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import preroute as PR, routediag as RD, netclass as NC, review as R
import pcbgen as g

ROOT = os.path.join(os.path.dirname(__file__), "..")
HERE = os.path.dirname(os.path.abspath(__file__))


def run(path, cons=None, spec=None, **kw):
    return PR.run(RD.load(path), cons, spec, **kw)


def codes(f):
    return [x["code"] for x in f]


def test_net_over_budget_and_waiver(tmp_path):
    b = g.ic("U1", 10, 10, ["N1", "a", "b", "c", "d"]) + g.ic("U2", 80, 10, ["N1", "e", "f", "g", "h"])
    p = g.board(tmp_path, b)
    f, card = run(p)
    assert "NET_OVER_BUDGET" in codes(f) and card["fail"] >= 1
    msg = [x for x in f if x["code"] == "NET_OVER_BUDGET"][0]
    assert msg["value"] > msg["limit"] == 30.0
    f2, card2 = run(p, {"review": {"waive": ["NET_OVER_BUDGET:^N1 "]}})
    assert "NET_OVER_BUDGET" not in codes(f2)
    f3, _ = run(p, {"placement": {"budget": {"signal": 100}}})
    assert "NET_OVER_BUDGET" not in codes(f3)


def test_support_part_far_and_clean_board(tmp_path):
    b = g.ic("U1", 20, 20, ["N1", "a", "b", "c", "d"]) + g.two("C1", 40, 20, "N1", "GND")
    f, _ = run(g.board(tmp_path, b))
    assert "SUPPORT_PART_FAR" in codes(f)
    ok = g.ic("U1", 20, 20, ["N1", "a", "b", "c", "d"], pitch=1.27) + g.two("C1", 15.5, 20, "GND", "N1")
    f2, card = run(g.board(tmp_path, ok, name="ok.kicad_pcb"))
    assert card["fail"] == 0, [x["msg"] for x in f2]


def test_keepout_trapped_names_the_part_not_the_zone_owner(tmp_path):
    zone = ('(zone (net 0) (net_name "") (layers "F.Cu") (name "ANT1_CLEARANCE") '
            '(keepout (tracks not_allowed) (vias allowed) (pads allowed)) '
            '(polygon (pts (xy 10 10) (xy 30 10) (xy 30 30) (xy 10 30))))\n')
    b = zone + g.two("L1", 20, 20, "A", "B") + g.two("ANT1", 15, 15, "B", "C") + g.two("R9", 50, 50, "X", "Y")
    f, _ = run(g.board(tmp_path, b))
    kt = [x for x in f if x["code"] == "KEEPOUT_TRAPPED"]
    assert [x["refs"] for x in kt] == [["L1"]]


def _owned_zone(layer):
    # zone owned by a moved + rotated footprint; KiCad stores its polygon in ABSOLUTE board coordinates
    z = (f'(zone (net 0) (net_name "") (layers "{layer}") (name "") '
         '(keepout (tracks not_allowed) (vias not_allowed) (pads allowed)) '
         '(polygon (pts (xy 10 10) (xy 14 10) (xy 14 14) (xy 10 14))))')
    return (f'(footprint "H" (layer "F.Cu") (at 50 20 90) (property "Reference" "J9") {z})\n')


def test_footprint_owned_zone_is_absolute_and_layer_checked(tmp_path):
    kt = lambda b, n: [x["refs"] for x in run(g.board(tmp_path, b, name=n))[0] if x["code"] == "KEEPOUT_TRAPPED"]
    # F.Cu keep-out: a cap at its absolute position is trapped; one where a double transform would put it is not
    assert kt(_owned_zone("F.Cu") + g.two("C5", 12, 12, "A", "B"), "a.kicad_pcb") == [["C5"]]
    assert kt(_owned_zone("F.Cu") + g.two("C6", 38, 32, "A", "B"), "b.kicad_pcb") == []
    # a B.Cu keep-out does not trap an F.Cu pad
    assert kt(_owned_zone("B.Cu") + g.two("C5", 12, 12, "A", "B"), "c.kicad_pcb") == []


def test_pin_escape_blocked_by_neighbour_courtyard_and_copper(tmp_path):
    # fine-pitch IC, left pad row; a part 0.1 mm off the pad row blocks the lane
    ic = g.ic("U1", 20, 20, ["A", "B", "C", "D", "E", "F"], pitch=0.4)
    near = g.two("R1", 17.2, 19.2, "X", "Y", court=(-1.0, -0.5, 1.0, 0.5))
    f, _ = run(g.board(tmp_path, ic + near))
    assert "PIN_ESCAPE_BLOCKED" in codes(f)
    far = g.two("R1", 12, 12, "X", "Y", court=(-1.0, -0.5, 1.0, 0.5))
    f2, _ = run(g.board(tmp_path, ic + far, name="c.kicad_pcb"))
    assert "PIN_ESCAPE_BLOCKED" not in codes(f2)
    seg = "(segment (start 17.5 18.5) (end 17.5 22) (width 0.15) (layer \"F.Cu\") (net \"ZZ\"))\n"
    f3, _ = run(g.board(tmp_path, ic + far + seg, name="d.kicad_pcb"))
    assert "PIN_ESCAPE_BLOCKED" in codes(f3)


def test_ratsnest_crossings_and_congestion(tmp_path):
    b = (g.two("R1", 10, 10, "A", "A2") + g.two("R2", 50, 50, "A", "A3")
         + g.two("R3", 50, 10, "B", "B2") + g.two("R4", 10, 50, "B", "B3"))
    p = g.board(tmp_path, b)
    B = RD.load(p)
    cls = NC.classify(B)
    # A joins (9.5,10)-(49.5,50) and B joins (49.5,10)-(9.5,50): they cross once
    assert PR.ratsnest_crossings(PR.routed_edges(B, cls)) == 1
    f, card = run(p)
    assert card["crossings"] == 1 and "RATSNEST_CROSSINGS" in codes(f)
    p2 = g.board(tmp_path, g.two("Ra", 10, 10, "A", "Q") + g.two("Rb", 50, 10, "A", "W")
                 + g.two("Rc", 10, 50, "B", "E") + g.two("Rd", 50, 50, "B", "T"), name="x.kicad_pcb")
    assert run(p2)[1]["crossings"] == 0
    # congestion: 20 parallel nets squeezed through a 2 mm strip on 2 layers
    body = ""
    for i in range(30):
        body += g.two(f"R{i}a", 5, 10 + 0.1 * i, f"S{i}", f"p{i}") + g.two(f"R{i}b", 95, 10 + 0.1 * i, f"S{i}", f"q{i}")
    f3, card3 = run(g.board(tmp_path, body, name="y.kicad_pcb"), {"preroute": {"signal_layers": 2}})
    assert card3["congestion"]["overflow"] > 0 and "CONGESTION" in codes(f3)


def test_review_carries_preroute_codes_and_waives(tmp_path):
    b = g.ic("U1", 20, 20, ["N1", "a", "b", "c", "d"]) + g.two("C1", 40, 20, "N1", "GND")
    p = g.board(tmp_path, b)
    facts = {"board_path": p, "parts": {}, "net_pads": {}}
    f = R.check_preroute(facts, None, {})
    assert any(x["code"] == "SUPPORT_PART_FAR" and x["level"] == "FAIL" for x in f)
    assert R._waived(f[0], ["%s:." % f[0]["code"]])
    assert R.check_preroute(facts, None, {"preroute": {"enabled": False}}) == []


def test_cli_route_ready_exit_codes(tmp_path):
    bad = g.board(tmp_path, g.ic("U1", 20, 20, ["N1", "a", "b", "c", "d"]) + g.two("C1", 40, 20, "N1", "GND"))
    cli = os.path.join(ROOT, "cli.py")
    r = subprocess.run([sys.executable, cli, "route-ready", bad, "--json", str(tmp_path / "o.json")],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "NOT READY" in r.stdout
    assert json.load(open(tmp_path / "o.json"))["scorecard"]["fail"] >= 1
    r = subprocess.run([sys.executable, cli, "route-ready", bad, "--waive", "SUPPORT_PART_FAR:.",
                        "--waive", "NET_OVER_BUDGET:."], capture_output=True, text=True)
    assert r.returncode == 0 and "RESULT: ready" in r.stdout


def test_route_refuses_unless_forced(tmp_path):
    bad = g.board(tmp_path, g.ic("U1", 20, 20, ["N1", "a", "b", "c", "d"]) + g.two("C1", 40, 20, "N1", "GND"))
    cli = os.path.join(ROOT, "cli.py")
    r = subprocess.run([sys.executable, cli, "route", "--board", bad], capture_output=True, text=True)
    assert r.returncode != 0 and "ABORT before route: route-ready failed" in (r.stderr + r.stdout)
    assert "NOT READY" in r.stdout          # the scorecard is printed before refusing
    r = subprocess.run([sys.executable, cli, "route", "--board", bad, "--force"], capture_output=True, text=True)
    assert "continuing to route (--force)" in r.stdout


def test_signal_layers_exclude_inner_planes_not_outer_pours(tmp_path):
    z = lambda net, lay: (f'(zone (net 1) (net_name "{net}") (layers "{lay}") '
                          '(polygon (pts (xy 0 0) (xy 100 0) (xy 100 80) (xy 0 80))))\n')
    body = z("GND", "F.Cu") + z("GND", "In1.Cu") + g.two("R1", 10, 10, "A", "B")
    B = RD.load(g.board(tmp_path, body))          # layers: F.Cu In1.Cu B.Cu
    sig, plane = PR.layer_split(B)
    assert sig == ["F.Cu", "B.Cu"] and plane == ["In1.Cu"]
    assert PR.layer_split(B, {"preroute": {"signal_layers": ["F.Cu", "In1.Cu"]}})[0] == ["F.Cu", "In1.Cu"]


def test_rail_cap_near_any_pin_or_bulk_is_not_far(tmp_path):
    from fluxplace import clusters as CL
    def val(ref, x, y, v, a, b):
        return g.fp(ref, x, y, [g.pad("1", -0.5, 0, a), g.pad("2", 0.5, 0, b)]).replace(
            f'(property "Reference" "{ref}")', f'(property "Reference" "{ref}") (property "Value" "{v}")')
    b = g.ic("U501", 20, 20, ["VDD", "a", "b", "c", "d"]) + g.ic("U502", 80, 20, ["VDD", "e", "f", "g", "h"])
    b += val("C509", 16, 20, "100nF", "VDD", "GND")     # block 5 owner U501... but sits next to nothing else
    b += val("C510", 76, 20, "100nF", "VDD", "GND")     # right beside U502 pin: serves U502 although numbered 5xx
    b += val("C511", 50, 50, "47uF", "VDD", "GND")      # bulk: not per-pin bypass
    cl, _ = CL.build(RD.load(g.board(tmp_path, b)))
    far = {s["ref"] for _, s in CL.far_parts(cl)}
    assert "C510" not in far and "C511" not in far
