import os, subprocess, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import preroute as PR, routediag as RD, placechecks as PCK, clusters as CL, routable as RB, boardwrite as BW
import pcbgen as g

ROOT = os.path.join(os.path.dirname(__file__), "..")


def run(path, cons=None, spec=None):
    return PR.run(RD.load(path), cons, spec)


def seg(a, b, net, w=0.15):
    return f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) (layer "F.Cu") (net "{net}"))\n'


# (a) one objective ------------------------------------------------------------------
def test_score_is_one_scalar_and_ranks_boards(tmp_path):
    far = g.ic("U1", 20, 20, ["N1", "a", "b", "c", "d"], pitch=1.27) + g.two("C1", 60, 20, "N1", "GND")
    near = g.ic("U1", 20, 20, ["N1", "a", "b", "c", "d"], pitch=1.27) + g.two("C1", 15.5, 20, "GND", "N1")
    sf = run(g.board(tmp_path, far, name="f.kicad_pcb"))[1]["score"]
    sn = run(g.board(tmp_path, near, name="n.kicad_pcb"))[1]["score"]
    assert sn < sf
    f, card = run(g.board(tmp_path, far, name="f2.kicad_pcb"))
    assert PR.score(f, card) == card["score"]


def test_placer_never_returns_worse_and_keeps_best(tmp_path):
    p = g.board(tmp_path, g.ic("U1", 20, 20, ["N1", "a", "b", "c", "d"], pitch=1.27)
                + g.two("C1", 25, 20, "GND", "N1"))             # already ideal
    B = RD.load(p)
    s0 = PR.run(B)[1]["score"]
    pl = RB.Placer(B, None, None, lambda m: None)
    mv = pl.run()
    assert PR.run(B)[1]["score"] <= s0
    B2 = RD.load(p)
    pl2 = RB.Placer(B2, {"placement": {"time_limit_s": 0}}, None, lambda m: None)
    pl2.run()
    assert pl2.best is not None and PR.run(B2)[1]["score"] <= s0


# (b) hard vs soft ---------------------------------------------------------------------
def test_hard_classes_cost_more_than_soft(tmp_path):
    p = g.board(tmp_path, g.two("R1", 10, 10, "RF_A", "x") + g.two("R2", 90, 10, "RF_A", "y")
                + g.two("R3", 10, 40, "SIG", "x2") + g.two("R4", 90, 40, "SIG", "y2"))
    f, card = run(p)
    rf = [x for x in f if x["code"] == "NET_OVER_BUDGET" and x["cls"] == "rf"][0]
    sg = [x for x in f if x["code"] == "NET_OVER_BUDGET" and x["cls"] == "signal"][0]
    assert PR.HARD and "rf" in PR.HARD and "signal" not in PR.HARD
    assert PR.W_HARD * (rf["value"] - rf["limit"]) > PR.W_SOFT * (sg["value"] - sg["limit"])


# (c) side policy / height region ------------------------------------------------------
def test_height_region_and_side_policy(tmp_path):
    b = g.two("C1", 20, 20, "A", "B", side="B", court=(-1, -.5, 1, .5)) + g.two("C2", 40, 20, "C", "D", side="B")
    b += g.two("R1", 60, 20, "E", "F")
    p = g.board(tmp_path, b)
    cons = {"placement": {"height_region": [{"name": "under M.2", "rect": [10, 10, 30, 30], "side": "B",
                                             "max_height_mm": 0.6}],
                          "heights": {"C1": 1.0}, "side": {"default": "F"}}}
    f, _ = run(p, cons)
    hr = [x for x in f if x["code"] == "HEIGHT_REGION"]
    assert len(hr) == 1 and hr[0]["level"] == "FAIL" and hr[0]["refs"] == ["C1"]
    cons["placement"]["heights"] = {}
    f, _ = run(p, cons)
    assert [x["level"] for x in f if x["code"] == "HEIGHT_REGION"] == ["WARN"]      # unknown height flagged
    assert any(x["code"] == "SIDE_POLICY" for x in f)


# (d) block cohesion -------------------------------------------------------------------
def test_block_cohesion_term(tmp_path):
    spread = g.two("C401", 10, 10, "A", "B") + g.two("C402", 90, 70, "C", "D")
    tight = g.two("C401", 10, 10, "A", "B") + g.two("C402", 14, 10, "C", "D")
    spec = {"blocks": {"veh": ["C401", "C402"]}}
    a = run(g.board(tmp_path, spread, name="s.kicad_pcb"), None, spec)[1]
    b = run(g.board(tmp_path, tight, name="t.kicad_pcb"), None, spec)[1]
    assert a["block_mm"] > b["block_mm"] and a["score"] > b["score"]


# (e) regulator template ---------------------------------------------------------------
def test_regulator_priority_order(tmp_path):
    ic = g.ic("U1", 30, 30, ["VIN", "SW", "BOOT", "FB", "GND", "VCC"], pitch=1.27)
    b = (ic + g.two("L1", 60, 30, "SW", "VOUT") + g.two("C2", 62, 40, "BOOT", "SW")
         + g.two("C3", 64, 50, "VIN", "GND") + g.two("R1", 66, 60, "FB", "VOUT"))
    spec = {"components": [{"ref": "U1", "pinmap": {"VIN": "1", "SW": "2", "BOOT": "3", "FB": "4", "GND": "5", "VCC": "6"}}]}
    B = RD.load(g.board(tmp_path, b))
    cl, _ = CL.build(B, None, spec)
    c = cl[0]
    assert c["regulator"]
    ranks = {s["ref"]: s["rank"] for s in c["supports"]}
    assert ranks["L1"] == 0 and ranks["C3"] == 1 and ranks["C2"] == 2 and ranks["R1"] == 3
    assert [s["ref"] for s in c["supports"]][:3] == ["L1", "C3", "C2"]
    assert "regulator template" in CL.format_text(cl)


# (f) anchor spacing + halo ------------------------------------------------------------
def test_anchor_spacing_not_a_movable_failure(tmp_path):
    b = g.two("J1", 5, 10, "P_P", "Q") + g.two("J2", 95, 10, "P_P", "R")
    p = g.board(tmp_path, b)
    f, _ = run(p, {"placement": {"fixed": ["J1", "J2"]}})
    assert [x["code"] for x in f if x["code"] in ("NET_OVER_BUDGET", "ANCHOR_SPACING")] == ["ANCHOR_SPACING"]
    a = [x for x in f if x["code"] == "ANCHOR_SPACING"][0]
    assert a["level"] == "WARN" and "J1" in a["msg"] and "J2" in a["msg"] and "minimum achievable" in a["msg"]


def test_support_distance_measured_from_halo_edge(tmp_path):
    ic = g.ic("U1", 20, 20, list("ABCDEFGHIJ"), pitch=0.4)
    b = ic + g.two("C1", 21.2, 20, "A", "GND")      # a hair over the 3 mm bypass limit... measured from the pin
    B = RD.load(g.board(tmp_path, ic + g.two("C1", 14.0, 17.9, "GND", "A")))
    cl, _ = CL.build(B)
    s = cl[0]["supports"][0]
    pin = next(p for p in B["parts"]["U1"]["pads"] if p["num"] == s["pad"])
    cap = next(p for p in B["parts"]["C1"]["pads"] if p["net"] == "A")
    raw = ((pin["x"] - cap["x"]) ** 2 + (pin["y"] - cap["y"]) ** 2) ** .5
    assert abs(s["dist"] - (raw - CL.HALO_MM)) < 1e-6


# (g) mating direction -----------------------------------------------------------------
def conn(ref, x, y, angle=0.0, name="X"):
    # pads biased to +y (the pad row) with the courtyard reaching 4 mm further in -y: opening "-y"
    return g.fp(ref, x, y, [g.pad("1", -2, 1, "N%s1" % ref), g.pad("2", 2, 1, "N%s2" % ref)], angle=angle, name=name,
                court=(-4, -5, 4, 2))


def test_connector_blocked_and_wrong_way(tmp_path):
    # J1 at the top edge, opening -y (outward, toward y=0); an SMA 8 mm out would be in the plug box? it is
    # off-board there; put a part INSIDE the plug box: the box is outward, so it is empty -> ok
    ok = g.board(tmp_path, conn("J1", 50, 6) + g.two("R1", 20, 40, "A", "B"), name="ok.kicad_pcb")
    assert not [x for x in run(ok)[0] if x["code"] == "CONNECTOR_BLOCKED"]
    # opening +y at the top edge faces inward: cannot be plugged in
    wrong = g.board(tmp_path, conn("J1", 50, 3, angle=180.0) + g.two("R1", 20, 40, "A", "B"), name="w.kicad_pcb")
    assert any(x["code"] == "CONNECTOR_BLOCKED" and "nearest board edge" in x["msg"]
               for x in run(wrong, {"placement": {"connectors": {"J1": {}}}})[0])
    # declared opening, plug box hits a neighbour 6 mm outside the edge of the courtyard
    cons = {"placement": {"connectors": {"J1": {"opening": "-y"}}, "plug_mm": 15}}
    blk = g.board(tmp_path, conn("J1", 50, 6) + g.two("R9", 50, 0.5, "A", "B", court=(-1, -.5, 1, .5))
                  , name="b.kicad_pcb", outline=(0, -5, 100, 80))
    f = run(blk, cons)[0]
    hit = [x for x in f if x["code"] == "CONNECTOR_BLOCKED"]
    assert hit and hit[0]["level"] == "FAIL" and "R9" in hit[0]["msg"]


def _cb(fs):
    return [x for x in fs if x["code"] == "CONNECTOR_BLOCKED"]


def test_connector_opening_inference(tmp_path):
    wrong = lambda **k: conn("J1", 50, 3, angle=180.0, **k) + g.two("R1", 20, 40, "A", "B")
    # vertical name: mates along z, no in-plane keep-out, no finding
    v = g.board(tmp_path, wrong(name="Molex_CLIK-Mate_1x14_P1.25mm_Vertical"), name="v.kicad_pcb")
    assert not _cb(run(v)[0])
    # horizontal at the edge facing inward: inferred WARN
    h = g.board(tmp_path, wrong(name="USB_C_Receptacle_TopMnt_Horizontal"), name="h.kicad_pcb")
    f = _cb(run(h, {"placement": {"connectors": {"J1": {}}}})[0])
    assert f and f[0]["level"] == "WARN" and "inferred" in f[0]["msg"]
    # unnamed, strong asymmetry but mid-board: not inferred
    m = g.board(tmp_path, conn("J1", 50, 40, angle=180.0), name="m.kicad_pcb")
    assert not _cb(run(m)[0])
    # declared z beats a Horizontal name; declared direction stays FAIL
    z = {"placement": {"connectors": {"J1": {"opening": "z"}}}}
    assert not _cb(run(h, z)[0])
    d = {"placement": {"connectors": {"J1": {"opening": "-y"}}}}
    assert _cb(run(h, d)[0])[0]["level"] == "FAIL"


# (h) stale copper ---------------------------------------------------------------------
def test_stale_copper_detected_and_stripped(tmp_path):
    b = g.two("R1", 10, 10, "A", "B") + g.two("R2", 30, 10, "A", "C")
    good = seg((10.5 - 1, 10), (29.5, 10), "A")                  # from R1.1 (9.5,10) .. R2.1 (29.5,10)
    p_ok = g.board(tmp_path, b + good, name="ok.kicad_pcb")
    assert not [x for x in run(p_ok)[0] if x["code"] == "STALE_COPPER"]
    # R2 moved away: the track now ends in the air
    moved = g.two("R1", 10, 10, "A", "B") + g.two("R2", 60, 40, "A", "C") + good
    p = g.board(tmp_path, moved, name="st.kicad_pcb")
    B = RD.load(p)
    s, v = PCK.stale_items(B)
    assert s == [0] and v == []
    assert [x for x in run(p)[0] if x["code"] == "STALE_COPPER"]
    out = tmp_path / "clean.kicad_pcb"
    r = subprocess.run([sys.executable, os.path.join(ROOT, "cli.py"), "route-ready", p, "--strip-stale",
                        "--out", str(out)], capture_output=True, text=True)
    assert "removed 1 stale" in r.stdout
    assert "(segment" not in out.read_text() and not [x for x in run(str(out))[0] if x["code"] == "STALE_COPPER"]


def test_stale_fixpoint_removes_chains(tmp_path):
    b = g.two("R1", 10, 10, "A", "B") + g.two("R2", 60, 40, "A", "C")
    chain = seg((9.5, 10), (20, 10), "A") + seg((20, 10), (30, 10), "A")   # second hangs off the first
    B = RD.load(g.board(tmp_path, b + chain))
    s, _ = PCK.stale_items(B)
    assert s == [0, 1] or s == [1, 0] or sorted(s) == [0, 1]
