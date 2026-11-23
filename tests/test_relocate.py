"""relocate rung: stub walk, free-spot search, locked/protected parts, a rejected attempt leaves the board unchanged. Synthetic text boards."""
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import pcbgen as PG
from fluxplace import relocate as RL, routediag as RD, boardwrite as BW

COURT = (-1.0, -0.6, 1.0, 0.6)


def seg(a, b, net, w=0.2, layer="F.Cu", locked=False):
    lk = " (locked yes)" if locked else ""
    return f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) (layer "{layer}") (net "{net}"){lk})\n'


def cap(ref, x, y, n1="VDD", n2="GND", **kw):
    return PG.two(ref, x, y, n1, n2, court=COURT, **kw)


def build(tmp_path, body, name="b.kicad_pcb"):
    return PG.board(tmp_path, body, name)


def ic_board(tmp_path, extra="", cap_kw=None, cap_at=(60, 40)):
    """U1 (pin 1 VDD) at (20, 40); C1 far away on VDD/GND, joined to U1 by a long track."""
    body = PG.ic("U1", 20, 40, ["VDD", "GND", "A", "B"], court=(-3, -2, 3, 2))
    body += cap("C1", cap_at[0], cap_at[1], **(cap_kw or {}))
    body += seg((cap_at[0] - 0.5, cap_at[1]), (22.0, 39.5), "VDD")
    return build(tmp_path, body + extra)


def sha(p):
    return hashlib.sha1(open(p, "rb").read()).hexdigest()


def test_stub_walk_stops_at_a_junction(tmp_path):
    body = cap("C1", 10, 10, "N", "G") + PG.two("R1", 30, 10, "N", "X")
    body += seg((9.5, 10), (15, 10), "N") + seg((15, 10), (20, 10), "N")          # C1 pad -> junction at (20, 10)
    body += seg((20, 10), (29.5, 10), "N") + seg((20, 10), (20, 20), "N")         # junction: three items meet
    p = build(tmp_path, body)
    B = RD.load(p)
    st = RL.stub_items(B, "C1")
    assert st["segs"] == {0, 1} and not st["vias"] and st["locked_mm"] == 0


def test_stub_walk_takes_the_whole_wire_to_another_part_and_keeps_locked_copper(tmp_path):
    body = cap("C1", 10, 10, "N", "G") + PG.two("R1", 30, 10, "N", "X")
    body += seg((9.5, 10), (20, 10), "N") + seg((20, 10), (29.5, 10), "N")
    p = build(tmp_path, body)
    assert RL.stub_items(RD.load(p), "C1")["segs"] == {0, 1}
    body = cap("C1", 10, 10, "N", "G") + PG.two("R1", 30, 10, "N", "X")
    body += seg((9.5, 10), (10.5, 10), "N", locked=True) + seg((10.5, 10), (29.5, 10), "N")
    p = build(tmp_path, body, "c.kicad_pcb")
    st = RL.stub_items(RD.load(p), "C1")
    assert st["segs"] == set() and st["locked_mm"] == 1.0


def test_spot_is_legal_and_close_to_the_target(tmp_path):
    p = ic_board(tmp_path)
    B = RD.load(p)
    tgt = next(q for q in B["pads"] if q["ref"] == "U1" and q["num"] == "1")
    s = RL.find_spot(B, "C1", tgt)
    assert s and s["dist"] <= RL.RELOC_MAX_MM
    (pads, court) = RL.G.moved(B["parts"]["C1"], s["x"], s["y"], s["angle"])
    u = B["parts"]["U1"]["court"]
    assert not RL.G.overlap(court, u, RL.RELOC_COURT_GAP)


def test_spot_avoids_courtyards_and_foreign_copper(tmp_path):
    # fill the ring next to U1 pin 1 with courtyards of other parts and a foreign track: the spot must still be legal
    blockers = "".join(cap(f"C{i}", 20 + dx, 40 + dy, "X", "Y") for i, (dx, dy) in enumerate(
        [(-5.0, -2.5), (-5.0, 0.0), (-5.0, 2.5), (-2.5, -3.5), (0.0, -3.5), (2.5, -3.5)], start=10))
    p = ic_board(tmp_path, extra=blockers + seg((10, 44.2), (30, 44.2), "FOREIGN"))
    B = RD.load(p)
    tgt = next(q for q in B["pads"] if q["ref"] == "U1" and q["num"] == "1")
    s = RL.find_spot(B, "C1", tgt)
    assert s
    pads, court = RL.G.moved(B["parts"]["C1"], s["x"], s["y"], s["angle"])
    for r, q in B["parts"].items():
        if r != "C1":
            assert not RL.G.overlap(court, q["court"], RL.RELOC_COURT_GAP), r
    for q in pads:                                   # clear of the foreign track (y 44.2, 0.2 wide) by more than the clearance
        assert abs(q["y"] - 44.2) - q["ey"] - 0.1 >= B["clearance"] or abs(q["x"] - 20) > 20


def test_no_spot_when_nothing_fits(tmp_path):
    big = "".join(cap(f"K{i}", 20 + dx, 40 + dy, "X", "Y") for i, (dx, dy) in enumerate(
        [(x * 2.3, y * 1.4) for x in range(-5, 6) for y in range(-8, 9)]))
    p = ic_board(tmp_path, extra=big)
    B = RD.load(p)
    tgt = next(q for q in B["pads"] if q["ref"] == "U1" and q["num"] == "1")
    assert RL.find_spot(B, "C1", tgt, max_mm=3.0) is None


def gap(nets):
    return {"id": "g1", "kind": "open-free", "class": "plain", "nets": nets, "at": None, "layer": None, "evidence": ""}


def test_locked_protected_and_big_parts_are_never_suggested(tmp_path):
    p = ic_board(tmp_path)
    B = RD.load(p)
    assert [m["part"] for m in RL.suggest(B, [gap(["VDD"])])] == ["C1"]
    assert RL.suggest(B, [gap(["VDD"])], protect={"VDD"}) == []                       # protected net
    assert RL.suggest(B, [gap(["VDD"])], manifest={"nets": {"VDD": {"why": "pair a/b"}}}) == []
    assert RL.suggest(B, [gap(["VDD"])], manifest={"nets": {"VDD": {"why": "locked"}}})   # locked entries only guard locked items
    p2 = ic_board(tmp_path, cap_kw={"locked": True})
    assert RL.suggest(RD.load(p2), [gap(["VDD"])]) == []                               # locked part
    assert RL.suggest(B, [dict(gap(["VDD"]), kind="short")]) == []


def run(p, tmp_path, route, count, budget=5000):
    out = str(tmp_path / "out.kicad_pcb")
    res = RL.relocate(p, out, [gap(["VDD"])], {"cons": {}, "protect": set(), "budget_s": budget}, route=route, open_count=count)
    return res, out


def test_a_rejected_attempt_leaves_the_board_unchanged(tmp_path):
    p = ic_board(tmp_path)
    before = sha(p)
    res, out = run(p, tmp_path, lambda b, o, nets, c: None, lambda b: {"VDD": 1})        # nothing routed: opens stay 1
    assert res["moved"] == 0 and not os.path.exists(out) and sha(p) == before
    assert res["log"][0]["result"] == "opens not lower"
    assert not [f for f in os.listdir(tmp_path) if ".rl" in f]


def test_a_kept_attempt_moves_the_part_and_removes_its_stub(tmp_path):
    p = ic_board(tmp_path)
    before = sha(p)

    def route(b, o, nets, c):                        # a stand-in router: the board stays as moved, the stub is gone
        open(o, "w").write(open(b).read())

    count = lambda b: {"VDD": 0} if os.path.basename(b).find(".rl") >= 0 else {"VDD": 1}      # noqa: E731
    res, out = run(p, tmp_path, route, count)
    assert res["moved"] == 1 and sha(p) == before and os.path.exists(out)
    B0, B1 = RD.load(p), RD.load(out)
    d0 = ((B0["parts"]["C1"]["pos"][0] - 20) ** 2 + (B0["parts"]["C1"]["pos"][1] - 40) ** 2) ** .5
    d1 = ((B1["parts"]["C1"]["pos"][0] - 20) ** 2 + (B1["parts"]["C1"]["pos"][1] - 40) ** 2) ** .5
    assert d0 > 30 and d1 < 10 and not B1["segs"]
    assert res["log"][0]["result"] == "kept" and res["log"][0]["from"] == [60.0, 40.0]


def test_a_net_that_gets_worse_rejects_the_move(tmp_path):
    p = ic_board(tmp_path)
    count = lambda b: {"VDD": 1} if ".rl" not in b else {"VDD": 0, "OTHER": 2}                 # noqa: E731
    res, out = run(p, tmp_path, lambda b, o, nets, c: None, count)
    assert res["moved"] == 0 and not os.path.exists(out) and res["log"][0]["result"].startswith("worse")


def test_rung_is_in_the_open_free_ladder_after_handrepair():
    from fluxplace import gaps as GP
    r = GP.DISPATCH["open-free"]
    assert r.index("handrepair") < r.index("relocate") < r.index("ai") and "relocate" in GP.RUNGS


def test_a_spent_budget_tries_nothing_and_changes_nothing(tmp_path):
    p = ic_board(tmp_path)
    before = sha(p)
    res, out = run(p, tmp_path, lambda b, o, nets, c: 1 / 0, lambda b: {"VDD": 1}, budget=60)
    assert res["moved"] == 0 and res["tried"] == 0 and not os.path.exists(out) and sha(p) == before
    assert res["log"][0]["result"] == "budget"


# ---- review (w16reloc): guards that cannot be read, owned parts, 1-pad parts, the yield gate

def test_a_one_pad_part_and_a_part_with_unreadable_guards_are_never_suggested(tmp_path, monkeypatch):
    p = ic_board(tmp_path)
    B = RD.load(p)
    one = build(tmp_path, PG.ic("U1", 20, 40, ["VDD", "GND", "A", "B"], court=(-3, -2, 3, 2))
                + PG.fp("X1", 70, 40, [PG.pad("1", 0, 0, "VDD")], court=COURT), "one.kicad_pcb")
    B1 = RD.load(one)
    assert [m["part"] for m in RD.diagnose(B1, ["VDD"])["summary"]["moves"]] == ["X1"]      # route-diagnose names it
    assert RL.suggest(B1, [gap(["VDD"])]) == []                                             # one pad: it stays
    monkeypatch.setattr(RL, "RELOC_PAD_MIN", 1)
    assert [m["part"] for m in RL.suggest(B1, [gap(["VDD"])])] == ["X1"]
    monkeypatch.undo()
    from fluxplace import preroute as PR
    monkeypatch.setattr(PR, "fixed_parts", lambda B, cons: 1 / 0)     # the fixed set cannot be read: nothing may move
    assert RL.suggest(B, [gap(["VDD"])]) == []


def test_an_owned_support_part_only_moves_toward_its_owner(tmp_path, monkeypatch):
    p = ic_board(tmp_path)
    B = RD.load(p)
    monkeypatch.setattr(RD, "_owners", lambda B: {"C1": {"ic": "U9", "pad": "1", "net": "X", "how": "net"}})
    assert RL.suggest(B, [gap(["VDD"])]) == []                        # the suggestion goes to U1, the owner is U9
    monkeypatch.setattr(RD, "_owners", lambda B: {"C1": {"ic": "U1", "pad": "1", "net": "VDD", "how": "net"}})
    assert [m["part"] for m in RL.suggest(B, [gap(["VDD"])])] == ["C1"]


def _run_w(p, tmp_path, route, count):
    out = str(tmp_path / "out.kicad_pcb")
    return RL.relocate(p, out, [gap(["VDD"])], {"cons": {}, "protect": set(), "budget_s": 5000, "work": str(tmp_path)}, route=route, open_count=count)


def test_a_dropped_move_is_not_routed_again_in_the_same_work_dir(tmp_path):
    p = ic_board(tmp_path)
    calls = []
    route = lambda b, o, nets, c: calls.append(nets)                  # noqa: E731
    res = _run_w(p, tmp_path, route, lambda b: {"VDD": 1})
    assert res["log"][0]["result"] == "opens not lower" and len(calls) == 1
    res = _run_w(p, tmp_path, route, lambda b: {"VDD": 1})
    assert res["status"] == "no relocatable part" and len(calls) == 1


def test_the_rung_stops_after_misses_in_a_row_and_a_kept_move_resets_the_count(tmp_path):
    import json
    p = ic_board(tmp_path)
    st = os.path.join(str(tmp_path), RL.RELOC_STATE)
    json.dump({"misses": RL.RELOC_MISS_MAX, "tried": {}}, open(st, "w"))
    res = _run_w(p, tmp_path, lambda b, o, nets, c: 1 / 0, lambda b: {"VDD": 1})
    assert res["status"] == "no yield here" and res["tried"] == 0
    json.dump({"misses": RL.RELOC_MISS_MAX - 1, "tried": {}}, open(st, "w"))
    count = lambda b: {"VDD": 0} if ".rl" in os.path.basename(b) else {"VDD": 1}      # noqa: E731
    keep = lambda b, o, nets, c: open(o, "w").write(open(b).read())                   # noqa: E731
    assert _run_w(p, tmp_path, keep, count)["moved"] == 1 and json.load(open(st))["misses"] == 0
    # close did not adopt it (the part is where it was): one more try, then the move is spent
    assert _run_w(p, tmp_path, keep, count)["moved"] == 1
    assert _run_w(p, tmp_path, keep, count)["status"] == "no relocatable part"


def test_the_router_rung_runs_before_relocate():
    from fluxplace import gaps as GP
    r = GP.DISPATCH["open-free"]
    assert r.index("krt") < r.index("relocate") and GP.LEVEL["relocate"] == "L1"
