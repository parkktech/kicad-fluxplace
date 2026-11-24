"""Trace finishing stage, T1a: frame, rule table, corner measures, score, events (TRACE-FINISH-SPEC, RECIPE-TRACE-FINISH)."""
import hashlib, json, math, os, sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pcbgen as G
from fluxplace import routediag, events, tracefinish as TF, tfmeasure as TM

RULES = os.path.join(os.path.dirname(TF.__file__), "data", "trace_finish_rules.json")
HARD = [f"H{i}" for i in range(1, 11)]
PRACTICE = ("C1 C2 C3 C4 C5 C6 C7 C8 C9 C11 C12 C13 C14 C15 C16 C17 C18 C19 C20 "
            "P1 P2 P3 P4 P5 P6 P7 P8 P9 P10 P12 P13 P14 P15 "
            "M1 M2 M4 M9 M11 "
            "S1 S2 S3 S4 S5 S6 S8 S9 S10 S11 S12 S13").split()


def seg(a, b, mid=None, net="N", layer="F.Cu", w=0.2, locked=False):
    return {"net": net, "a": a, "b": b, "w": w, "mid": mid, "layer": layer, "locked": locked}


def model(segs):
    return {"segs": segs, "pads": [], "parts": {}, "zones": [], "vias": [], "copper": ["F.Cu", "B.Cu"], "edge": None}


def wedge(interior_deg, net="N", layer="F.Cu", L=2.0):
    """Two straight legs of length L meeting at (0,0) with the given interior angle."""
    t = math.radians(interior_deg)
    return [seg((L, 0.0), (0.0, 0.0), net=net, layer=layer), seg((0.0, 0.0), (L * math.cos(t), L * math.sin(t)), net=net, layer=layer)]


def rot(segs, k, mirror=False):
    def p(xy):
        x, y = (-xy[0], xy[1]) if mirror else xy
        for _ in range(k):
            x, y = -y, x
        return (x, y)
    out = []
    for s in segs:
        d = dict(s, a=p(s["a"]), b=p(s["b"]))
        if s["mid"] is not None:
            d["mid"] = p(s["mid"])
        out.append(d)
    return out


# ---- 1. rule table -----------------------------------------------------------------------------------------------

def test_rule_table_is_10_hard_50_practice_with_sources():
    rows = json.load(open(RULES))["rules"]
    ids = [r["id"] for r in rows]
    assert len(ids) == 60 and len(set(ids)) == 60
    assert sorted(r["id"] for r in rows if r["tag"] == "HARD") == sorted(HARD)
    assert sorted(r["id"] for r in rows if r["tag"] == "PRACTICE") == sorted(PRACTICE)
    assert len(PRACTICE) == 50 and not {"C10", "P11", "S7", "M3", "M5", "M6", "M7", "M8", "M10"} & set(ids)
    assert all(r.get("src") for r in rows)
    assert {r["conf"] for r in rows} <= {"H", "M", "L", "J"}
    assert all(r["weight"] == {"H": 3, "M": 2, "L": 1, "J": 1}[r["conf"]] for r in rows)


# ---- 2. classes on two fixture boards (4-layer and 6-layer, different net names) ---------------------------------

FOUR = dict(rf={"nets": ["RF_GNSS"]}, pairs={"USB2_": {"impedance_diff": 90, "skew_mm": 0.15, "skew_max_mm": 1.27},
                                   "ETH_": {"impedance_diff": 100, "skew_mm": 0.15, "skew_max_mm": 0.5}})
SIX = dict(rf={"nets": ["ANT_LTE"]}, pairs={"PCIE_": {"impedance_diff": 90, "skew_mm": 0.1, "skew_max_mm": 0.127},
                                            "USB3_": {"impedance_diff": 90, "skew_mm": 0.1, "skew_max_mm": 0.127}})


def _two_nets(tmp_path, nets):
    tmp_path.mkdir(parents=True, exist_ok=True)
    # two parts per pair of nets, so every net has >= 2 pads (netclass classifies nets with >= 2 pads)
    body = "".join(G.two(f"{k}{i}", 10 + 5 * i, 30 + 10 * k_i, a, b) for i, (a, b) in enumerate(nets) for k, k_i in (("R", 0), ("Q", 1)))
    return routediag.load(G.board(tmp_path, body))


def test_classes_on_two_fixture_boards(tmp_path):
    B4 = _two_nets(tmp_path / "a", [("RF_GNSS", "GND"), ("USB2_DP", "USB2_DN"), ("ETH_TXP", "ETH_TXN"), ("VBAT_5V", "GND"), ("AUD_IN", "GND")])
    got4 = TM.classes(B4, FOUR, None)
    assert got4["RF_GNSS"] == "RF" and got4["USB2_DP"] == "HSS" and got4["ETH_TXP"] == "HSS"
    assert got4["AUD_IN"] == "ANA" and got4["GND"] == "GEN"
    B6 = _two_nets(tmp_path / "b", [("ANT_LTE", "GND"), ("PCIE_TX0P", "PCIE_TX0N"), ("USB3_RX_P", "USB3_RX_N"), ("VBAT", "GND")])
    got6 = TM.classes(B6, SIX, None)
    assert got6["ANT_LTE"] == "RF" and got6["PCIE_TX0P"] == "HSF" and got6["USB3_RX_P"] == "HSF"


# ---- 3. corners, junctions, arcs, rotation and mirror ------------------------------------------------------------

@pytest.mark.parametrize("interior,kind", [(30, "acute"), (60, "acute"), (89, "acute"), (90.4, "right"), (135, "45")])
def test_wedge_kinds(interior, kind):
    cs = TM.corners(model(wedge(interior)))
    assert len(cs) == 1 and cs[0]["kind"] == kind


def test_arc_to_tangent_joint_is_straight_not_corner():
    # straight leg into a 90 degree arc, tangent at the joint: no corner counted as a bend
    R = 2.0
    cx, cy = 10.0, R
    end = (cx + R * math.sin(math.pi / 2), cy - R * math.cos(math.pi / 2))
    mid = (cx + R * math.sin(math.pi / 4), cy - R * math.cos(math.pi / 4))
    a = seg((0.0, 0.0), (10.0, 0.0))
    b = seg((10.0, 0.0), end, mid=mid)
    cs = TM.corners(model([a, b]))
    assert len(cs) == 1 and cs[0]["kind"] == "straight"


def test_t_is_junction_not_corner():
    segs = [seg((0, 0), (5, 0)), seg((5, 0), (10, 0)), seg((5, 0), (5, 5))]
    assert TM.corners(model(segs)) == []
    js = TM.junctions(model(segs))
    assert len(js) == 1 and js[0]["n"] == 3


@pytest.mark.parametrize("k,mirror", [(0, False), (1, False), (2, False), (3, False), (0, True), (1, True)])
def test_corner_counts_invariant_under_rotation_and_mirror(k, mirror):
    segs = wedge(30) + wedge(89, layer="B.Cu") + wedge(90.4, net="M")
    base = sorted(c["kind"] for c in TM.corners(model(segs)))
    moved = sorted(c["kind"] for c in TM.corners(model(rot(segs, k, mirror))))
    assert base == moved == ["acute", "acute", "right"]


# ---- 4. one fixture per rule measure -----------------------------------------------------------------------------

def _states(sites):
    return sorted(s["state"] for s in sites)


def test_c1_acute_row_not_ok():
    assert _states(TM.c1_sites(model(wedge(30)))) == ["open"]


def test_c2_right_row_not_ok_and_45_ok():
    right = wedge(90.4)
    ok45 = wedge(135)
    assert _states(TM.c2_sites(model(right))) == ["open"]
    assert _states(TM.c2_sites(model(ok45))) == []


def test_c4_short_segment_between_bends():
    # two 45-degree bends with a 0.2 mm middle run (w = 0.2, needs >= 0.4 mm)
    # 45 bend at (5,0), run of 0.1414 mm, 45 bend at (5.1,0.1); w = 0.2 so the run needs >= 0.4 mm
    segs = [seg((0, 0), (5, 0)), seg((5, 0), (5.1, 0.1)), seg((5.1, 0.1), (9, 0.1))]
    assert _states(TM.c4_sites(model(segs))) == ["open"]


def test_c13_odd_angle_counted_not_failed():
    sites = TM.c13_sites(model(wedge(180 - 30)))            # deflection 30 deg
    assert _states(sites) == ["already"]


def test_c14_branch_under_90_open():
    segs = [seg((0, 0), (5, 0)), seg((5, 0), (10, 0)), seg((5, 0), (5 + 3 * math.cos(math.radians(30)), 3 * math.sin(math.radians(30))))]
    assert _states(TM.c14_sites(model(segs))) == ["open"]


def test_c5_bend_is_arc_or_open():
    arc_bend = wedge(90, net="R")
    arc_bend[1] = seg((0, 0), (2, 2), net="R", mid=(0.6, 1.4))
    assert "already" in _states(TM.c5_sites(model(arc_bend), {"R": "RF"}))
    assert _states(TM.c5_sites(model(wedge(90, net="R")), {"R": "RF"})) == ["open"]


def test_c5_only_rf_hsf_hss_bends():
    assert _states(TM.c5_sites(model(wedge(135, net="G")), {"G": "GEN"})) == []
    assert _states(TM.c5_sites(model(wedge(135, net="G")), {"G": "ANA"})) == []
    assert _states(TM.c5_sites(model(wedge(90, net="H")), {"H": "HSS"})) == ["open"]


def test_c15_polyline_arc_open_native_arc_already():
    pts = [(10 + 2 * math.cos(math.radians(a)), 10 + 2 * math.sin(math.radians(a))) for a in range(0, 50, 5)]
    poly = [seg(pts[i], pts[i + 1], w=0.2) for i in range(len(pts) - 1)]   # 5 deg steps: chord 0.17 mm < 0.2
    assert _states(TM.c15_sites(model(poly))) == ["open"]
    native = [seg((0, 0), (2, 2), mid=(0.6, 1.4))]
    assert _states(TM.c15_sites(model(native))) == ["already"]


def test_m1_acid_trap_open():
    segs = wedge(30)
    segs = [dict(s, mid=None) for s in segs]
    assert _states(TM.m1_sites(model(segs), 60.0)) == ["open"]


def test_m9_zero_length_and_duplicate_open():
    segs = [seg((0, 0), (0, 0)), seg((1, 0), (2, 0)), seg((2, 0), (1, 0))]
    assert _states(TM.m9_sites(model(segs))).count("open") == 2


# ---- 5. records and score: hand table, flagged rise ---------------------------------------------------------------

def test_score_hand_table_and_flag():
    # rule x class: already, applied, not_possible counts -> (sum, eligible)
    table = {("C1", "GEN"): ["already", "already", "not_possible"], ("C1", "RF"): ["already"],
             ("C2", "GEN"): ["applied", "applied", "already", "already"]}
    sc = {k: TM.score_of(v) for k, v in table.items()}
    assert round(sc[("C1", "GEN")][0], 3) == round(2 / 3, 3) and sc[("C1", "GEN")][1] == 3
    assert sc[("C1", "RF")] == (1.0, 1)
    assert sc[("C2", "GEN")] == (1.0, 4)
    assert TM.score_flags({"eligible": 10, "score": 0.5}, {"eligible": 7, "score": 0.7}) == ["RISE_ON_FALLING_ELIGIBLE"]
    assert TM.score_flags({"eligible": 10, "score": 0.5}, {"eligible": 10, "score": 0.7}) == []


# ---- 6. F0 record (DRC faked, one clearance violation: base, verdict not FAIL) ---------------------------------

class _Ctx:
    """Minimal stand-in for pipeline.Ctx: cons, spec, dir(n, name) -> <work>/<n>-<name>/."""
    def __init__(self, work, cons):
        self.work, self.cons, self.cons_path, self.spec, self.spec_path = str(work), cons, None, None, None

    def dir(self, n, name):
        d = os.path.join(self.work, f"{n}-{name}")
        os.makedirs(d, exist_ok=True)
        return d


def _enabled_ctx(tmp_path, board, cons):
    return _Ctx(tmp_path / "w", cons)


def test_f0_clearance_violation_is_base_not_fail(tmp_path, monkeypatch):
    board = G.board(tmp_path, G.two("R1", 10, 10, "A", "B"))
    fake = {"violations": [{"type": "clearance", "description": "Clearance violation", "items": [{"description": "x"}]}],
            "unconnected_items": []}
    monkeypatch.setattr(TF, "_drc", lambda path, **k: fake)
    c = _enabled_ctx(tmp_path, board, {"finish": {"enabled": True}})
    res = TF.stage(c, 7, board)
    assert res["verdict"] != "fail" and res["verdict"] != "FAIL"
    fj = json.load(open(os.path.join(c.dir(7, "trace-finish"), "finish.json")))
    assert any(r["id"] == "H1" and r["base"] >= 1 for r in fj["legality"])


# ---- 7. stage: skipped and enabled byte identity, events --------------------------------------------------------

def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def test_skipped_when_finish_absent_and_nothing_written(tmp_path, monkeypatch):
    board = G.board(tmp_path, G.two("R1", 10, 10, "A", "B"))
    c = _enabled_ctx(tmp_path, board, {})
    res = TF.stage(c, 7, board)
    assert res["verdict"] == "pass" and res["line"] == "trace-finish: skipped ([finish] enabled is not set)"
    assert res["board"] == board
    assert not os.path.exists(os.path.join(c.dir(7, "trace-finish"), "finish.json"))


def test_enabled_output_is_byte_copy_and_events_in_order(tmp_path, monkeypatch):
    board = G.board(tmp_path, G.two("R1", 10, 10, "A", "B"))
    monkeypatch.setattr(TF, "_drc", lambda path, **k: {"violations": [], "unconnected_items": []})
    stream = tmp_path / "events.jsonl"
    monkeypatch.setenv(events.ENV_FILE, str(stream))
    c = _enabled_ctx(tmp_path, board, {"finish": {"enabled": True}})
    res = TF.stage(c, 7, board)
    assert _sha(res["board"]) == _sha(board)
    lines = [json.loads(l) for l in open(stream)]
    kinds = [l["kind"] for l in lines]
    assert "progress" in kinds and "criteria" in kinds and "artifact" in kinds
    crit = [l for l in lines if l["kind"] == "criteria"]
    for ph in ("F0", "F8"):                      # HARD rows precede score rows in each report (T1b: score rows exist at F0 now)
        hard_first = [bool(l.get("hard")) for l in crit if l.get("phase") == ph]
        assert hard_first and hard_first == sorted(hard_first, reverse=True)
        assert {l["id"] for l in crit if l.get("phase") == ph and l.get("hard")} == set(HARD)
    assert kinds.index("progress") < next(i for i, l in enumerate(lines) if l["kind"] == "criteria")


@pytest.mark.parametrize("k,mirror", [(0, False), (1, False), (2, False), (3, False), (0, True), (1, True)])
def test_rule_measure_counts_invariant_under_rotation_and_mirror(k, mirror):
    # one combined fixture: wedges, a 0.1 mm run between two bends, a 3-leg junction, zero-length and duplicate segments
    segs = wedge(30) + wedge(90.4, net="M") + [seg((0, 0), (0, 0)), seg((20, 0), (21, 0)), seg((21, 0), (20, 0))]
    segs += [seg((5, 0), (5.1, 0.1)), seg((5, 0), (5, 3)), seg((5.1, 0.1), (9, 0.1))]
    ref, moved = model(segs), model(rot(segs, k, mirror))
    fns = {"c1": lambda B: TM.c1_sites(B), "c2": lambda B: TM.c2_sites(B), "c4": lambda B: TM.c4_sites(B),
           "c13": lambda B: TM.c13_sites(B), "c14": lambda B: TM.c14_sites(B), "m1": lambda B: TM.m1_sites(B, 60.0),
           "m9": lambda B: TM.m9_sites(B)}
    for name, f in fns.items():
        a = sorted(s["state"] for s in f(ref))
        b = sorted(s["state"] for s in f(moved))
        assert a == b, name


def test_progress_heartbeat_during_long_drc(tmp_path, monkeypatch):
    import time as _t
    stream = tmp_path / "events.jsonl"
    monkeypatch.setenv(events.ENV_FILE, str(stream))
    monkeypatch.setattr(TF, "HEARTBEAT_S", 0.05)
    def slow_drc(path, **k):
        _t.sleep(0.4)                                  # stands in for the 22 s W15 DRC
        return {"violations": [], "unconnected_items": []}
    monkeypatch.setattr(TF, "_drc", slow_drc)
    TF._drc_base(str(tmp_path / "b.kicad_pcb"), "kicad-cli")
    ts = [json.loads(l)["ts"] for l in open(stream) if json.loads(l)["kind"] == "progress"]
    assert len(ts) >= 4
    assert max(b - a for a, b in zip(ts, ts[1:])) < 0.3


# ---- stage: every rule has a measure, HARD rows H3 / H6-H9 present ---------------------------------------------------------------

def test_stage_f0_measures_every_rule_and_hard_row(tmp_path, monkeypatch):
    board = G.board(tmp_path, G.two("R1", 10, 10, "A", "B"))
    monkeypatch.setattr(TF, "_drc", lambda path, **k: {"violations": [], "unconnected_items": []})
    c = _Ctx(tmp_path / "w", {"finish": {"enabled": True}})
    res = TF.stage(c, 7, board)
    fj = json.load(open(os.path.join(c.dir(7, "trace-finish"), "finish.json")))
    assert fj["not_measured_rules"] == []
    ids = {r["id"] for r in fj["score"]}
    rules = json.load(open(os.path.join(os.path.dirname(TF.__file__), "data", "trace_finish_rules.json")))["rules"]
    assert {r["id"] for r in rules if r["tag"] == "PRACTICE"} <= ids
    hard = {h["id"]: h for h in fj["legality"]}
    assert [i for i, h in hard.items() if "not_measured" in h] == ["H6"] and "gap 16" in hard["H6"]["not_measured"]
    assert all(hard[i]["base"] is not None for i in ("H3", "H7", "H8", "H9"))
    assert fj["facts"]["vias"] == 0 and res["verdict"] == "pass"


def test_class_scores_weighted_by_rule_weight_and_zero_limit_ignored():
    rules = [{"id": "C1", "weight": 3}, {"id": "C2", "weight": 1}, {"id": "P1", "weight": 2}]
    rows = [{"id": "C1", "cls": "RF", "value": 0.5, "limit": 2}, {"id": "C1", "cls": "GEN", "value": 1.0, "limit": 4},
            {"id": "C2", "cls": "RF", "value": 0.25, "limit": 4}, {"id": "C2", "cls": "GEN", "value": 0.0, "limit": 1},
            {"id": "P1", "cls": "RF", "value": 1.0, "limit": 3}]
    cs = TM.class_scores(rows, rules)
    assert round(cs["RF"], 3) == 0.625 and round(cs["GEN"], 3) == 0.75 and round(cs["board"], 3) == 0.675
    with_zero = rows + [{"id": "P1", "cls": "GEN", "value": 0.333, "limit": 0}]
    assert TM.class_scores(with_zero, rules) == cs


def test_stage_writes_class_score_and_board_line(tmp_path, monkeypatch):
    board = G.board(tmp_path, G.two("R1", 10, 10, "A", "B"))
    monkeypatch.setattr(TF, "_drc", lambda path, **k: {"violations": [], "unconnected_items": []})
    c = _Ctx(tmp_path / "w", {"finish": {"enabled": True}})
    res = TF.stage(c, 8, board)
    fj = json.load(open(os.path.join(c.dir(8, "trace-finish"), "finish.json")))
    assert set(fj["class_score"]) == {"F0", "F8"} and "board" in fj["class_score"]["F0"]
    assert res["line"].endswith(f"score board {fj['class_score']['F0']['board']} -> {fj['class_score']['F8']['board']}")


# ---- M5: tests that bite (each kills one survivor) ------------------------------------------------------------------

def _rec(**k):
    hard = [{"id": "H1", "base": 2}, {"id": "H2", "base": {"GEN": 0.2}}, {"id": "H3", "base": 1}, {"id": "H4", "base": 0},
            {"id": "H5", "base": 0}, {"id": "H6", "base": 0}, {"id": "H7", "base": 0}, {"id": "H8", "base": 0},
            {"id": "H9", "base": 0}, {"id": "H10", "base": {"opens": 5, "drc_keys": 2}}]
    rec = {"hard": hard, "drc": {"clearance": 2}}
    rec.update(k)
    return rec


def _bump(rec, hid, fn):
    out = json.loads(json.dumps(rec))
    for h in out["hard"]:
        if h["id"] == hid:
            h["base"] = fn(h["base"])
    return out


@pytest.mark.parametrize("hid", ["H1", "H3", "H4", "H5", "H7", "H8", "H9"])
def test_worse_plus_one_on_each_count_row(hid):
    assert TF._worse(_rec(), _bump(_rec(), hid, lambda b: b + 1)) == [hid]


def test_worse_opens_plus_one_is_h10_and_equal_is_empty():
    assert TF._worse(_rec(), _bump(_rec(), "H10", lambda b: {"opens": 6, "drc_keys": 2})) == ["H10"]
    assert TF._worse(_rec(), _rec()) == []


def test_worse_class_min_width_h2_h6_and_drc_key_h10():
    assert TF._worse(_rec(), _bump(_rec(), "H2", lambda b: {"GEN": 0.19})) == ["H2"]
    assert TF._worse(_rec(), _bump(_rec(), "H6", lambda b: 1)) == ["H6"]
    more = _rec(drc={"clearance": 2, "hole_to_hole": 1})
    assert TF._worse(_rec(), more) == ["H10"]


def test_stage_second_f0_worse_on_h1_fails(tmp_path, monkeypatch):
    board = G.board(tmp_path, G.two("R1", 10, 10, "A", "B"))
    monkeypatch.setattr(TF, "_drc", lambda path, **k: {"violations": [], "unconnected_items": []})
    real, calls = TF.f0, []

    def fake(*a, **k):
        r = real(*a, **k)
        calls.append(1)
        if len(calls) == 2:
            for h in r["hard"]:
                if h["id"] == "H1":
                    h["base"] += 1
        return r
    monkeypatch.setattr(TF, "f0", fake)
    c = _Ctx(tmp_path / "w", {"finish": {"enabled": True}})
    res = TF.stage(c, 9, board)
    fj = json.load(open(os.path.join(c.dir(9, "trace-finish"), "finish.json")))
    assert res["verdict"] == "fail" and "ILLEGAL: H1" in res["line"] and fj["worse_hard"] == ["H1"]


def test_drc_fake_report_counts_h1_h4_h5(tmp_path, monkeypatch):
    board = G.board(tmp_path, G.two("R1", 10, 10, "A", "B"))
    vio = [{"type": "clearance", "items": []}, {"type": "clearance", "items": []}, {"type": "hole_to_hole", "items": []}] + \
          [{"type": "copper_edge_clearance", "items": []} for _ in range(3)]
    monkeypatch.setattr(TF, "_drc", lambda path, **k: {"violations": vio, "unconnected_items": []})
    c = _Ctx(tmp_path / "w", {"finish": {"enabled": True}})
    TF.stage(c, 10, board)
    fj = json.load(open(os.path.join(c.dir(10, "trace-finish"), "finish.json")))
    h = {r["id"]: r["base"] for r in fj["legality"]}
    assert (h["H1"], h["H4"], h["H5"]) == (2, 1, 3)


def test_h2_narrow_track_not_ok(tmp_path, monkeypatch):
    seg = '(segment (start 20 20) (end 30 20) (width 0.09) (layer "F.Cu") (net "A"))\n'
    board = G.board(tmp_path, G.two("R1", 10, 10, "A", "B") + seg)
    monkeypatch.setattr(TF, "_drc", lambda path, **k: {"violations": [], "unconnected_items": []})
    c = _Ctx(tmp_path / "w", {"finish": {"enabled": True}})
    TF.stage(c, 11, board)
    fj = json.load(open(os.path.join(c.dir(11, "trace-finish"), "finish.json")))
    assert next(r for r in fj["legality"] if r["id"] == "H2")["ok"] is False




def test_c14_branch_75_open_90_already():
    def segs(deg):
        t = math.radians(deg)
        return [seg((0, 0), (5, 0)), seg((5, 0), (10, 0)), seg((5, 0), (5 + 3 * math.cos(t), 3 * math.sin(t)))]
    assert _states(TM.c14_sites(model(segs(75)))) == ["open"]
    assert _states(TM.c14_sites(model(segs(90)))) == ["already"]


def test_stream_f8_labels_pct_hard_ok_and_sites_total(tmp_path, monkeypatch):
    stream = tmp_path / "events.jsonl"
    monkeypatch.setenv(events.ENV_FILE, str(stream))
    board = G.board(tmp_path, G.two("R1", 10, 10, "A", "B"))
    monkeypatch.setattr(TF, "_drc", lambda path, **k: {"violations": [], "unconnected_items": []})
    c = _Ctx(tmp_path / "w", {"finish": {"enabled": True}})
    TF.stage(c, 12, board)
    evs = [json.loads(l) for l in open(stream)]
    prog = [e for e in evs if e["kind"] == "progress"]
    assert any(str(e.get("phase", "")).startswith("F8 measure") for e in prog)
    pcts = [e["pct"] for e in prog if "pct" in e and str(e.get("phase", "")).startswith("F8")]
    assert pcts and pcts == sorted(pcts) and 0 <= pcts[0] and pcts[-1] <= 100
    crit = [e for e in evs if e["kind"] == "criteria" and e.get("phase") == "F8" and e.get("hard")]
    assert crit and all("value" in e and "text" in e for e in crit)
    fj = json.load(open(os.path.join(c.dir(12, "trace-finish"), "finish.json")))
    assert all(e["ok"] == (e["id"] not in fj["worse_hard"]) for e in crit)
    assert set(fj["sites_total"]) == set(fj["sites"])


def test_beating_heartbeat_over_a_long_seed(tmp_path, monkeypatch):
    import time as _t
    stream = tmp_path / "events.jsonl"
    monkeypatch.setenv(events.ENV_FILE, str(stream))
    monkeypatch.setattr(TF, "HEARTBEAT_S", 0.05)
    TF._beating("manifest seed", lambda: _t.sleep(0.6), stage="pipeline")   # stands in for the 12 s seed
    ts = [json.loads(l)["ts"] for l in open(stream) if json.loads(l)["kind"] == "progress"]
    assert len(ts) >= 4 and max(b - a for a, b in zip(ts, ts[1:])) < 0.3
