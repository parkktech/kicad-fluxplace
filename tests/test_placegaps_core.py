"""PLACEGAPS phase 0: gap shape, detectors (every kind from data the stages already have), the accept rule, the close loop
(fake evaluator, fake rungs), memo, report files, detect-only (byte-equal board), never raises. No pcbnew, no router."""
import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import placegaps as PG  # noqa: E402


def part(x, y, side="F.Cu", court=None):
    return {"pos": (x, y), "angle": 0.0, "side": side, "court": court or (x - 1, y - 1, x + 1, y + 1), "pads": []}


def fake_B():
    return {"parts": {"U1": part(10, 10), "C1": part(30, 10), "C2": part(12, 14), "R9": part(50, 50, "B.Cu")}, "bbox": (0, 0, 100, 100), "pads": []}


def ctx(**kw):
    c = PG.Ctx(stage="placetour", board="/nonexistent.kicad_pcb", cons={}, spec={}, ds_dir=None, blame=None, parts_gate=None)
    c.update(kw)
    return c


def test_every_kind_has_a_detector_and_only_ghost_open_and_row_rail_have_rungs_so_far():
    assert set(PG.ORDER) == set(PG.DETECT) == set(PG.DISPATCH)
    assert {k: v for k, v in PG.DISPATCH.items() if v} == {"ghost-open": ["unblock", "seal", "endpoint"], "row-rail": ["pull-load", "pour-need"],
                                                          "row-cin-side": ["template-turn", "unit-turn", "clear-side", "unit-shift", "cin-re-deal"],
                                                          "row-template": ["template-turn", "clear-side"], "row-pin-bypass": ["re-deal"],
                                                          "row-away": ["feasible", "push-out", "re-seat"], "row-flight": ["push-out"]}
    assert set(PG.RUNGS) == {"unblock", "seal", "endpoint", "pull-load", "pour-need", "template-turn", "unit-turn", "clear-side", "re-deal", "cin-re-deal",
                             "unit-shift", "feasible", "push-out", "re-seat"}
    assert len(set(PG.ORDER)) == len(PG.ORDER)
    assert PG.ORDER.index("ghost-open") < PG.ORDER.index("row-cin-side") < PG.ORDER.index("support-far") < PG.ORDER.index("part-stock")


def test_gap_shape_and_class_order():
    g = PG.gap("ghost-open", "N1", "plain", 2, refs=("U1", "R1", "U1"), nets=("N1",), at=(1.234, 5.678), detail="x")
    assert g["id"] == "ghost-open:N1" and g["refs"] == ["R1", "U1"] and g["at"] == [1.23, 5.68] and g["row"] is None
    h = PG.gap("ghost-open", "P/N", "hs-pair", 1)
    assert [x["id"] for x in PG._sort([g, h])] == ["ghost-open:P/N", "ghost-open:N1"]       # hs pair before plain whatever the mag


def test_ghost_detector_pair_is_one_gap_and_classes(monkeypatch):
    monkeypatch.setattr(PG, "_net_class", lambda c: lambda n: {"USB_DP": ("hs-pair", "USB_DM"), "USB_DM": ("hs-pair", "USB_DP"),
                                                               "RFX": ("rf", None)}.get(n, ("plain", None)))
    bl = {"open": [{"net": "USB_DP", "refs": ["J1", "U1"]}, {"net": "USB_DM", "refs": ["J1"]}, {"net": "RFX", "refs": ["C1"]},
                   {"net": "S1", "refs": []}],
          "costly": [{"net": "S1", "vias": 3, "detour": 2.5, "refs": ["R1"]}],
          "hot": [{"cells": 4, "occ": 0.7, "refs": ["U1"], "nets": ["S1"], "bbox": [0, 0, 4, 2], "vias": 1}], "turned": []}
    r = PG.detect(ctx(blame=bl), ["ghost-open", "ghost-costly", "ghost-hot"])
    assert r["counts"] == {"ghost-open": 3, "ghost-costly": 1, "ghost-hot": 1}
    assert r["classes"]["ghost-open"] == {"hs-pair": 1, "rf": 1, "plain": 1}
    pair = next(g for g in r["gaps"] if g["class"] == "hs-pair")
    assert pair["nets"] == ["USB_DM", "USB_DP"] and pair["id"] == "ghost-open:USB_DM/USB_DP"
    assert r["gaps"][0]["class"] == "hs-pair"
    assert PG.score_terms(r["gaps"])["hs_open"] == 1 and PG.score_terms(r["gaps"])["rf_open"] == 1


def test_not_measured_is_none_not_zero():
    r = PG.detect(ctx(), ["ghost-open", "part-stock"])                  # no blame, no parts gate
    assert r["counts"] == {"ghost-open": None, "part-stock": None}
    assert "-" in PG.table(r) and PG.stage_line(r) == "gaps 0"


def test_detector_failure_is_an_error_not_a_zero():
    c = ctx(rows=[{"hard": True, "verdict": "fail", "kind": "x"}])        # row without name: the detector raises
    r = PG.detect(c, ["row-away", "ghost-open"])
    assert r["counts"]["row-away"] is None and "row-away" in r["errors"]
    assert "ERR" in PG.table(r)


@pytest.mark.parametrize("row,kind", [
    ({"kind": "support", "name": "U1 template cin_side", "metric": "template"}, "row-cin-side"),
    ({"kind": "support", "name": "U1 template sw", "metric": "template"}, "row-template"),
    ({"kind": "support", "name": "U1 bare", "metric": "pin-bypass"}, "row-pin-bypass"),
    ({"kind": "separation", "name": "a-b", "metric": "distance"}, "row-away"),
    ({"kind": "keepout-flight", "name": "k", "metric": "crossing"}, "row-flight"),
    ({"kind": "rail", "name": "r", "metric": "reach"}, "row-rail"),
    ({"kind": "pair", "name": "p", "metric": "skew"}, "row-other")])
def test_row_kind_mapping(row, kind):
    assert PG.row_kind(row) == kind


def test_intent_row_mapping():
    assert PG.row_kind({"kind": "edge"}, True) == "row-edge"
    assert PG.row_kind({"kind": "away"}, True) == "row-away" and PG.row_kind({"kind": "region"}, True) == "row-away"


def test_rows_detector_hard_fail_only_and_support_rows_fold_into_support_far():
    rows = [{"kind": "separation", "name": "ant-metal", "metric": "distance", "hard": True, "verdict": "fail", "value": 8.6, "max": 10.0, "refs": ["A1"]},
            {"kind": "separation", "name": "soft", "metric": "distance", "hard": False, "verdict": "fail", "value": 1, "max": 10.0},
            {"kind": "separation", "name": "ok", "metric": "distance", "hard": True, "verdict": "ok", "value": 11, "max": 10.0},
            {"kind": "support", "name": "C1 -> U1.1", "metric": "distance", "hard": True, "verdict": "fail", "value": 20, "max": 4.0, "refs": ["C1"]},
            {"kind": "support", "name": "C7 -> U1.2", "metric": "distance", "hard": True, "verdict": "fail", "value": 5, "max": 4.0, "refs": ["C7"]}]
    far = [{"ref": "C1", "ic": "U1", "pad": "1", "dist": 20.0, "limit": 4.0, "kind": "decap"},
           {"ref": "C2", "ic": "U1", "pad": "2", "dist": 9.0, "limit": 4.0, "kind": "other"}]
    B = fake_B()
    c = ctx(rows=rows, _far=far, B=B, _intent=[{"kind": "away", "name": "g", "hard": True, "met": False, "value": 1, "limit": 3, "refs": ["U1"], "detail": ""},
                                              {"kind": "support", "name": "agg", "hard": True, "met": False, "value": 2, "limit": 0, "refs": [], "detail": ""}])
    r = PG.detect(c, ["row-away", "row-other", "support-far"])
    assert r["counts"] == {"row-away": 2, "row-other": 1, "support-far": 2}      # C7 has no far entry: kept as a row, not lost
    sf = {g["refs"][0] if g["refs"][0] != "U1" else g["refs"][1]: g for g in r["gaps"] if g["kind"] == "support-far"}
    assert sf["C1"]["row"] == "support:C1 -> U1.1" and sf["C2"]["row"] is None and sf["C1"]["class"] == "decap"
    assert PG.score_terms(r["gaps"])["hard_rows"] == 4 and PG.score_terms(r["gaps"])["decap_far"] == 1


def test_support_starved_by_free_area(monkeypatch):
    B = fake_B()
    far = [{"ref": "C1", "ic": "U1", "pad": "1", "dist": 20.0, "limit": 4.0, "kind": "decap"}]
    r = PG.detect(ctx(B=B, _far=far), ["support-starved"])
    assert r["counts"]["support-starved"] == 0                           # open board: ring area is plenty
    B["parts"]["U1"]["court"] = (9, 9, 11, 11)
    for i in range(40):                                                # wall the ring in with big courtyards
        B["parts"][f"X{i}"] = part(10, 10, court=(5, 5, 15, 15))
    r = PG.detect(ctx(B=B, _far=far), ["support-starved"])
    assert r["counts"]["support-starved"] == 1 and r["gaps"][0]["refs"][0] == "C1"


def test_side_illegal_height_and_closed_area(monkeypatch):
    from fluxplace import intent as IT, legalise as LGS
    monkeypatch.setattr(IT, "height", lambda B, cons, ref: {"R9": 3.0}.get(ref))
    monkeypatch.setattr(LGS, "keepout_rects", lambda B, cons: [((45, 45, 55, 55), {"B"}, [], None, "closed")])
    r = PG.detect(ctx(B=fake_B(), cons={"placement": {"back_max_height_mm": 2.0}}), ["side-illegal"])
    assert r["counts"]["side-illegal"] == 1 and "over back_max_height_mm" in r["gaps"][0]["detail"] and "closed" in r["gaps"][0]["detail"]


def test_parts_detector_from_gate():
    g = {"parts": {"U1": [{"need": "datasheet", "code": "PART_DATASHEET_MISSING", "detail": "no pdf"}],
                   "U2": [{"need": "stock", "code": "PART_NO_STOCK", "detail": "0 at qty"}, {"need": "datasheet", "code": "X", "detail": ""}],
                   "U3": [{"need": "stock", "code": "STOCK_UNCHECKED", "detail": "offline"}, {"need": "model", "code": "M", "detail": ""}]}}
    r = PG.detect(ctx(parts_gate=g), ["part-datasheet", "part-stock"])
    assert r["counts"] == {"part-datasheet": 2, "part-stock": 1}


def st(**kw):
    return dict({"illegal": 0}, **kw)


def side(gaps, **kw):
    return {"gaps": gaps, "state": st(**kw), "refusal": None}


def test_accept_rules():
    far = lambda n, m=1.0: [PG.gap("support-far", f"C{i}", "decap", m, refs=[f"C{i}"]) for i in range(n)]
    hs = PG.gap("ghost-open", "P/N", "hs-pair", 1)
    base = side(far(3) + [hs])
    assert PG._accept(base, side(far(2) + [hs]), "support-far:C0")[0] is True                                  # lower score
    ok, check, why = PG._accept(base, side(far(0)), None)                                                      # hs open gone, fine
    assert ok
    ok, check, why = PG._accept(side(far(3)), side(far(0) + [hs]))                                             # supports fixed, hs open created
    assert (ok, check) == (False, "judge:hs-pair open")
    ok, check, why = PG._accept(base, dict(side(far(0)), state=st(illegal=2)))
    assert (ok, check) == (False, "legal:new")
    ok, check, _ = PG._accept(base, {"gaps": [], "state": st(), "refusal": "legal:overlap C1 vs C2"})
    assert (ok, check) == (False, "legal:overlap")
    ok, check, _ = PG._accept(base, {"gaps": [], "state": st(), "refusal": "drc:clearance C1"})
    assert (ok, check) == (False, "drc:clearance")
    row = PG.gap("row-away", "a", "plain", 0.2, row="separation:a")
    ok, check, _ = PG._accept(side(far(1)), side(far(0) + [row]))
    assert (ok, check) == (False, "row:separation:a")                                                          # a new hard row: refused with its name
    assert PG._accept(base, base)[:2] == (False, "score:no-gain")                # no gain


def test_accept_target_progress_on_equal_score():
    g1, g2 = PG.gap("support-far", "C1", "decap", 5.0), PG.gap("support-far", "C1", "decap", 5.0)
    g2["mag"] = 2.0
    base, cand = side([g1]), side([g2])
    assert PG.score(base["gaps"]) > PG.score(cand["gaps"])                                                     # far_mm is a score term: lower anyway
    cand2 = side([dict(g2, mag=2.0)], open=1)
    assert PG._accept(base, side([g1]), "support-far:C1")[0] is False
    assert PG._accept(side([g1], open=1), cand2, "support-far:C1")[0] is True


def test_hs_open_outranks_any_number_of_plain_opens():
    hs = [PG.gap("ghost-open", "P/N", "hs-pair", 1)]
    plain = [PG.gap("ghost-open", f"S{i}", "plain", 1) for i in range(20)]
    assert PG.score(hs) > PG.score(plain)                                                                       # one hs open is worse than 20 plain


class FakeEv(PG.Evaluator):
    """State = set of far support refs; a candidate moves one ref home (removes it) unless it is listed as `bad`."""
    def __init__(self, far, bad=(), illegal=()):
        self.far, self.bad, self.illegal, self.committed, self.B = set(far), set(bad), set(illegal), [], fake_B()

    def detect(self, kinds):
        return [PG.gap("support-far", r, "decap", 5.0, refs=[r]) for r in sorted(self.far)]

    def state(self):
        return {"illegal": 0}

    def board(self):
        return self.B

    def pose_key(self, g):
        return (g["id"], tuple(sorted(self.far)))

    def trial(self, cand):
        ref = next(iter(cand["moves"]))
        if ref in self.illegal:
            return {"gaps": self.detect(None), "state": {"illegal": 0}, "refusal": f"legal:overlap {ref} vs X1"}
        left = self.far - ({ref} if ref not in self.bad else set())
        return {"gaps": [PG.gap("support-far", r, "decap", 5.0, refs=[r]) for r in sorted(left)], "state": {"illegal": 0}, "refusal": None, "left": left}

    def commit(self, tr):
        self.far = set(tr["left"])
        self.committed.append(sorted(self.far))


def rung_home(g, c):
    ref = g["refs"][0]
    return [{"id": f"home-{ref}", "rung": "home", "moves": {ref: (0, 0, 0, "F")}, "claims": [], "notes": ""}]


def test_close_repairs_refuses_and_remembers(monkeypatch):
    monkeypatch.setitem(PG.DISPATCH, "support-far", ["home"])
    monkeypatch.setitem(PG.RUNGS, "home", rung_home)
    ev = FakeEv({"C1", "C2", "C3"}, bad={"C2"}, illegal={"C3"})
    out = PG.close({"cons": {"flow": {"no_gain_rounds": 2}}}, ["support-far"], ev)
    assert [a["gap"] for a in out["accepted"]] == ["support-far:C1"]
    checks = {(r["gap"], r["check"]) for r in out["refused"]}
    assert ("support-far:C3", "legal:overlap") in checks and ("support-far:C2", "score:no-gain") in checks
    assert out["memo_skips"] >= 1 and len([r for r in out["refused"] if r["gap"] == "support-far:C2"]) == 1   # the second round does not retry the same poses
    assert len(out["gaps_in"]) == 3 and len(out["gaps_out"]) == 2 and out["score_out"] < out["score_in"]
    assert out["stop"].startswith(("no gain", "nothing new"))


def test_close_is_deterministic(monkeypatch):
    monkeypatch.setitem(PG.DISPATCH, "support-far", ["home"])
    monkeypatch.setitem(PG.RUNGS, "home", rung_home)
    runs = []
    for _ in range(2):
        ev = FakeEv({"C1", "C2", "C3", "C4"}, bad={"C3"})
        o = PG.close({"cons": {}}, ["support-far"], ev)
        runs.append((o["accepted"], o["refused"], o["stop"], ev.committed))
    assert runs[0] == runs[1]


def test_close_without_rungs_changes_nothing():
    ev = FakeEv({"C1"})
    out = PG.close({"cons": {}}, ["support-far"], ev)
    assert out["stop"] == "no rung for the gaps in scope" and out["accepted"] == [] and ev.committed == []
    assert PG.close({"cons": {}}, ["support-far"], FakeEv(set()))["stop"] == "no gap in scope"


def test_close_time_stop_is_named(monkeypatch):
    monkeypatch.setitem(PG.DISPATCH, "support-far", ["home"])
    monkeypatch.setitem(PG.RUNGS, "home", rung_home)
    t = iter(range(0, 10 ** 6, 1000))
    out = PG.close({"cons": {"flow": {"budget_min": 1}}}, ["support-far"], FakeEv({"C1", "C2"}), clock=lambda: next(t))
    assert "time stop" in out["stop"]


def test_pose_hash_follows_the_poses():
    B = fake_B()
    g = PG.gap("support-far", "C1", "decap", 1, refs=["C1", "U1"])
    h0 = PG.pose_hash(B, g)
    assert PG.pose_hash(B, g) == h0
    B["parts"]["C2"]["pos"] = (12.5, 14)                                   # a neighbour inside the window moved
    assert PG.pose_hash(B, g) != h0
    B2 = fake_B()
    B2["parts"]["R9"]["pos"] = (51, 50)                                    # a far part: not in the window
    assert PG.pose_hash(B2, g) == h0
    assert PG.seed(g, 3) == PG.seed(g, 3) and PG.seed(g, 3) != PG.seed(PG.gap("support-far", "C2", "decap", 1), 3)


def test_detect_stage_is_detect_only_and_writes_reports(tmp_path, monkeypatch):
    board = tmp_path / "b.kicad_pcb"
    board.write_bytes(b"(kicad_pcb)\n")
    h = hashlib.sha256(board.read_bytes()).hexdigest()
    bl = {"open": [{"net": "S1", "refs": ["U1"]}], "costly": [], "hot": [], "turned": []}
    monkeypatch.setattr(PG, "_net_class", lambda c: lambda n: ("plain", None))
    lines = []
    r = PG.detect_stage("placetour", str(board), {}, {}, str(tmp_path / "out"), blame=bl, printer=lines.append,
                        kinds=["ghost-open", "ghost-costly", "ghost-hot"])
    assert r["counts"]["ghost-open"] == 1 and r["line"] == "gaps 1 in 1 kinds: ghost-open 1"
    assert hashlib.sha256(board.read_bytes()).hexdigest() == h and sorted(os.listdir(tmp_path)) == ["b.kicad_pcb", "out"]
    assert sorted(os.listdir(tmp_path / "out")) == ["ledger-rows.md", "placegaps.json", "placegaps.md"]
    d = json.loads((tmp_path / "out" / "placegaps.json").read_text())
    assert d["counts"]["ghost-open"] == 1 and d["gaps"][0]["id"] == "ghost-open:S1"
    assert "ghost-open" in lines[0] and "part-stock" in lines[0]                       # the table lists every kind


def test_detect_stage_never_raises(tmp_path):
    r = PG.detect_stage("floorplan", "/no/such/board.kicad_pcb", None, None, str(tmp_path), printer=None)
    assert r["report"] is not None or r["errors"]                                      # unreadable board: errors, not an exception
    r = PG.detect_stage("floorplan", "/no/such/board.kicad_pcb", {}, {}, "/proc/forbidden/x", printer=lambda *_: 1 / 0)
    assert "line" in r


def test_merge_keeps_measured_counts():
    a = {"gaps": [PG.gap("part-stock", "U1")], "counts": {"part-stock": 1, "ghost-open": None}, "classes": {"part-stock": {"plain": 1}}, "errors": {}}
    b = {"gaps": [PG.gap("ghost-open", "S1")], "counts": {"part-stock": None, "ghost-open": 1}, "classes": {"ghost-open": {"plain": 1}}, "errors": {}}
    m = PG.merge(a, b)
    assert m["counts"] == {"part-stock": 1, "ghost-open": 1} and len(m["gaps"]) == 2


def test_stage_call_sites_exist():
    root = os.path.join(os.path.dirname(__file__), "..", "fluxplace")
    for mod, stage in (("ready.py", "ready"), ("floorplan2.py", "floorplan"), ("placetour.py", "placetour")):
        assert f'detect_stage("{stage}"' in open(os.path.join(root, mod)).read()


def test_stock_unchecked_is_not_a_zero():
    g = {"parts": {"U1": [{"need": "datasheet", "code": "D", "detail": ""}, {"need": "stock", "code": "STOCK_UNCHECKED", "detail": ""}]},
         "unchecked": ["distributor data and stock NOT CHECKED (offline / no credentials)"]}
    r = PG.detect(ctx(parts_gate=g), ["part-datasheet", "part-stock"])
    assert r["counts"] == {"part-datasheet": 1, "part-stock": None} and "part-stock" in r["errors"]


def test_close_soft_rows_stop_after_soft_budget(monkeypatch):
    class SoftEv(FakeEv):
        def detect(self, kinds):
            return [dict(PG.gap("support-far", r, "decap", 5.0, refs=[r]), soft=(r == "C2")) for r in sorted(self.far)]
    monkeypatch.setitem(PG.DISPATCH, "support-far", ["home"])
    monkeypatch.setitem(PG.RUNGS, "home", rung_home)
    ticks = {"t": 0.0}

    def clock():
        ticks["t"] += 100.0
        return ticks["t"]
    ev = SoftEv({"C1", "C2", "C3"}, bad={"C1", "C2", "C3"})
    out = PG.close({"cons": {"placegaps": {"soft_budget_min": 5}}}, ["support-far"], ev, clock=clock)
    assert out.get("soft_skips", 0) >= 1 and all(r["gap"] != "support-far:C2" for r in out["refused"][-1:]) or out.get("soft_skips")
    ev = SoftEv({"C2"}, bad={"C2"})
    out = PG.close({"cons": {"placegaps": {"soft_budget_min": 1000}}}, ["support-far"], ev, clock=clock)
    assert not out.get("soft_skips") and any(r["gap"] == "support-far:C2" for r in out["refused"])       # inside the budget it is worked
