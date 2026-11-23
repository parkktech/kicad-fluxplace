"""Shared DRC guard (fluxplace/drcguard.py): multiset-of-keys acceptance, position-free
identity, zone-violation exclusion, and guard_board accepting a count rise with no new key."""
import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import drcguard as DG
from fluxplace import tune as TU

pcbnew = pytest.importorskip("pcbnew")
MM = pcbnew.FromMM


def _v(typ, *descs, x=0.0, y=0.0):
    return {"type": typ, "items": [{"description": d, "pos": {"x": x, "y": y}} for d in descs]}


def test_identity_ignores_position_and_decimal_measurements():
    a = DG.key_of(_v("track_dangling", "Track [N1] on F.Cu, length 10.0000 mm", x=1, y=1))
    b = DG.key_of(_v("track_dangling", "Track [N1] on F.Cu, length 9.7000 mm", x=10.3, y=10))
    assert a == b


def test_identity_keeps_integers_that_name_the_violation():
    assert DG.key_of(_v("clearance", "Pad 1 of U3")) != DG.key_of(_v("clearance", "Pad 2 of U3"))


def test_second_instance_of_an_existing_key_is_new():
    base = Counter({("track_dangling", ("Track [N1]",)): 1})
    cand = Counter({("track_dangling", ("Track [N1]",)): 2})
    assert DG.new_keys(base, cand) == Counter({("track_dangling", ("Track [N1]",)): 1})


def test_removed_keys_and_moved_keys_are_not_new():
    base = Counter({("a", ("x",)): 1, ("b", ("y",)): 1})
    assert not DG.new_keys(base, Counter({("a", ("x",)): 1}))
    assert not DG.new_keys(base, Counter({("a", ("x",)): 1, ("b", ("y",)): 1}))


def test_a_different_violation_is_new_even_when_the_count_is_equal():
    base = Counter({("drc_gap", ("a",)): 1})
    assert DG.new_keys(base, Counter({("drc_width", ("b",)): 1})) == Counter({("drc_width", ("b",)): 1})


def test_zone_violations_are_excluded_from_keys_only():
    d = {"violations": [_v("clearance", "Zone GND [GND]"), _v("clearance", "Pad 1 of U3")],
         "unconnected_items": []}
    keys, counts = DG.keys_from_report(d)
    assert sum(keys.values()) == 1 and counts == (2, 0)


def _board(path):
    b = pcbnew.BOARD()
    b.SetCopperLayerCount(2)
    n = pcbnew.NETINFO_ITEM(b, "A")
    b.Add(n)
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(pcbnew.VECTOR2I(MM(0), MM(0)))
    t.SetEnd(pcbnew.VECTOR2I(MM(40), MM(0)))
    t.SetWidth(MM(0.2))
    t.SetLayer(pcbnew.F_Cu)
    t.SetNet(n)
    b.Add(t)
    b.Save(path)
    return path


def test_guard_board_accepts_a_count_rise_with_no_new_key(tmp_path):
    src = _board(str(tmp_path / "w.kicad_pcb"))
    base = (Counter({("drc_gap", ("a",)): 1}), (1, 0))
    accepted, cur = DG.guard_board(src, src, lambda bd: True, base, "kicad-cli", lambda m: None,
                                   report=lambda p, c: (Counter({("drc_gap", ("a",)): 1}), (3, 0)))
    assert accepted is True and cur[1] == (3, 0)


def test_guard_board_rejects_a_new_key_and_leaves_the_work_file(tmp_path):
    src = _board(str(tmp_path / "w.kicad_pcb"))
    before = open(src, "rb").read()
    base = (Counter({("drc_gap", ("a",)): 1}), (1, 0))
    accepted, _ = DG.guard_board(src, src, lambda bd: True, base, "kicad-cli", lambda m: None,
                                 report=lambda p, c: (Counter({("drc_width", ("b",)): 1}), (1, 0)))
    assert accepted is False and open(src, "rb").read() == before


def test_tune_pairs_guard_uses_the_shared_rule(tmp_path, monkeypatch):
    src = _board(str(tmp_path / "w.kicad_pcb"))
    monkeypatch.setattr(TU, "drc_report", lambda p, kicad_cli="kicad-cli": (Counter({("drc_gap", ("a",)): 1}), (5, 0)))
    ok, _ = TU._guarded(src, src, lambda bd: True, "kicad-cli", (Counter({("drc_gap", ("a",)): 1}), (1, 0)),
                        lambda m: None)
    assert ok is True


# ---- drcguard.accept (PHASE2-guard recipe): one test per rule and mode, the trade case, measure_from_report ----
from fluxplace import audit as AU  # noqa: E402


def _m(opens=0, shorts=0, errors=0, keys=(), by_net=None, gap_ids=None):
    k = Counter(keys)
    m = {"opens": opens, "shorts": shorts, "errors": errors, "keys": k, "err_keys": k,
         "opens_by_net": Counter(by_net or {})}
    if gap_ids is not None:
        m["gap_ids"] = set(gap_ids)
    return m


CLR = ("clearance", ("Pad 1 of U3",))


def test_r0_connectivity_uses_snap_and_first_violation(monkeypatch):
    monkeypatch.setattr(AU, "connectivity_diff", lambda b, a, expect=(): {"ok": False, "violations": ["moved U1"]})
    seen = []
    snap = lambda p: seen.append(p) or {}
    assert DG.accept(_m(), _m(), base_board="a", cand_board="b", snap=snap) == (False, "R0 moved U1")
    assert seen == ["a", "b"]


def test_r0_skipped_without_both_boards():
    def boom(p):
        raise AssertionError("snapshot must not run")
    assert DG.accept(_m(), _m(), "edit", base_board="a", snap=boom) == (True, "")


def test_r0_default_snapshot_is_unchecked(monkeypatch):
    seen = []
    monkeypatch.setattr(AU, "snapshot", lambda p, checks=True, project=None: seen.append(checks) or {})
    monkeypatch.setattr(AU, "connectivity_diff", lambda b, a, expect=(): {"ok": True, "violations": []})
    assert DG.accept(_m(), _m(), "edit", base_board="a", cand_board="b") == (True, "")
    assert seen == [False, False]


def test_r1_shorts_up_in_every_mode():
    for mode in ("repair", "build", "edit"):
        assert DG.accept(_m(shorts=1), _m(shorts=2), mode) == (False, "R1 shorts up")
    assert DG.accept(_m(shorts=2), _m(shorts=1), "edit") == (True, "")


def test_r2_repair_blocks_new_key_unless_kind_matches():
    base = _m(opens=2, keys=[CLR])
    cand = _m(opens=1, keys=[CLR, CLR])          # second instance of the same key is new
    assert DG.accept(base, cand, "repair", kind="short") == (False, "R2 new DRC key clearance")
    assert DG.accept(base, cand, "repair", kind=None) == (False, "R2 new DRC key clearance")
    assert DG.accept(base, cand, "repair", kind="clearance") == (True, "")


def test_r2_not_checked_in_build_or_edit():
    base = _m(opens=2)
    cand = _m(opens=1, keys=[CLR])
    assert DG.accept(base, cand, "build") == (True, "")
    assert DG.accept(base, cand, "edit") == (True, "")


def test_r3_locked_copper():
    assert DG.accept(_m(), _m(), "edit", locked_ok=lambda: False) == (False, "R3 locked copper changed")
    assert DG.accept(_m(), _m(), "edit", locked_ok=lambda: True) == (True, "")


def test_r4_repair_score_must_fall():
    assert DG.accept(_m(opens=2), _m(opens=2), "repair") == (False, "R4 score not lower")
    assert DG.accept(_m(opens=2), _m(opens=1), "repair") == (True, "")
    assert DG.accept(_m(opens=2), _m(opens=1), "repair", score=lambda m: [0]) == (False, "R4 score not lower")


def test_r4_build_opens_must_fall_and_edit_unchecked():
    assert DG.accept(_m(opens=2), _m(opens=2), "build") == (False, "R4 opens not lower")
    assert DG.accept(_m(opens=2), _m(opens=1), "build") == (True, "")
    assert DG.accept(_m(opens=2), _m(opens=3), "edit") == (True, "")


def test_target_gap_must_disappear_when_listed():
    base, cand = _m(opens=3), _m(opens=2, gap_ids={"b"})
    assert DG.accept(base, cand, "repair", target=("b",)) == (False, "target gap still there")
    assert DG.accept(base, cand, "repair", target=("a",)) == (True, "")
    assert DG.accept(base, _m(opens=2), "repair", target=("b",)) == (True, "")   # no gap_ids key: skipped


def test_trade_one_net_opens_two_close_score_falls_accepted():
    base = _m(opens=3, by_net={"A": 1, "B": 1, "C": 1})
    cand = _m(opens=2, by_net={"C": 1, "D": 1})
    assert DG.accept(base, cand, "repair") == (True, "")


def test_measure_from_report_fixture():
    d = {"violations": [
        {"type": "shorting_items", "severity": "error",
         "items": [{"description": "Track [N1] on F.Cu"}, {"description": "Track [N2] on F.Cu"}]},
        {"type": "clearance", "severity": "warning", "items": [{"description": "Pad 1 of U3"}]},
        {"type": "track_dangling", "severity": "error", "items": [{"description": "Zone [GND] on F.Cu"}]},
    ], "unconnected_items": [
        {"description": "x", "items": [{"description": "Pad 1 [N1] of R1 on F.Cu"}]},
        {"description": "x", "items": [{"description": "Pad 2 [N2] of R2 on F.Cu"}]},
    ]}
    v0, v2 = d["violations"][0], d["violations"][2]
    e = DG.measure_from_report(d)
    assert e["opens"] == 2 and e["opens_by_net"] == Counter({"N1": 1, "N2": 1})
    assert e["shorts"] == 1 and e["errors"] == 1      # the zone error is keyed but not counted
    assert e["keys"] is e["err_keys"]
    assert e["keys"] == Counter({DG.key_of(v0): 1, DG.key_of(v2): 1})
    a = DG.measure_from_report(d, severity="all")
    assert a["keys"] == Counter({DG.key_of(v0): 1, DG.key_of(d["violations"][1]): 1})   # warning in, zone violation out
    assert a["errors"] == 2                            # warning counted under "all", zone still out


def test_r5_refuses_trade_that_opens_protected_net():
    base = _m(opens=3, by_net={"A": 1, "B": 1, "C": 1})
    assert DG.accept(base, _m(opens=2, by_net={"C": 1, "P": 1}), "repair", protect=("P",)) == \
        (False, "R5 protected net opened P 0->1")
    assert DG.accept(base, _m(opens=2, by_net={"C": 1, "P": 1}), "build", protect=("P",))[1] == \
        "R5 protected net opened P 0->1"
    assert DG.accept(base, _m(opens=2, by_net={"C": 1, "P": 1}), "edit", protect=("P",))[1] == \
        "R5 protected net opened P 0->1"


def test_r5_accepts_same_trade_on_plain_net():
    base = _m(opens=3, by_net={"A": 1, "B": 1, "C": 1})
    assert DG.accept(base, _m(opens=2, by_net={"C": 1, "D": 1}), "repair", protect=("P",)) == (True, "")


def test_protected_nets_rf_and_pairs():
    cons = {"rf": {"nets": ["RF1"]}, "pairs": {"USB_": {}}}
    assert DG.protected_nets(cons, ["USB_DP", "USB_DN", "GND"]) == {"RF1", "USB_DP", "USB_DN"}
    assert DG.protected_nets({}, ["GND"]) == set()


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        DG.accept(_m(), _m(), "fix")


def test_zone_violation_not_counted_in_errors():
    d = {"violations": [
        {"type": "track_dangling", "severity": "error", "items": [{"description": "Zone [GND] on F.Cu"}]},
        {"type": "clearance", "severity": "error", "items": [{"description": "Pad 1 of U3"}]},
        {"type": "clearance", "severity": "warning", "items": [{"description": "Pad 2 of U3"}]},
    ], "unconnected_items": []}
    assert DG.measure_from_report(d)["errors"] == 1
    assert DG.measure_from_report(d, severity="all")["errors"] == 2


def test_protected_nets_naming_pairs_and_rf_names():
    from fluxplace import drcguard as DG
    got = DG.protected_nets({}, ["USB_DP", "USB_DM", "BT1_RF", "GPIO3"])
    assert {"USB_DP", "USB_DM", "BT1_RF"} <= got and "GPIO3" not in got


def test_keys_per_net_same_open_named_two_ways_is_equal():
    # PHASE3G G2a: KiCad names a different item pair for the same open net from run to run; the key is the net
    def unc(desc_a, desc_b):
        return {"unconnected_items": [{"type": "unconnected_items", "items": [
            {"description": desc_a}, {"description": desc_b}]}], "violations": []}
    a = unc("Track [NET_A] on B.Cu, length 10.0000 mm", "Pad 1 of U1 [NET_A]")
    b = unc("Via [NET_A] on B.Cu", "Pad 2 of J1 [NET_A]")
    assert DG.keys_per_net(a) == DG.keys_per_net(b) == {("Unet", "NET_A"): 1}
    new_open = unc("Track [NET_B] on F.Cu", "Pad 3 of R1 [NET_B]")
    assert set(DG.keys_per_net(new_open)) - set(DG.keys_per_net(a)) == {("Unet", "NET_B")}
    # a zone clearance item is still counted; keep() restricts the violations
    d = {"violations": [{"type": "clearance", "items": [{"description": "Zone [GND] on F.Cu"},
                                                        {"description": "Track [SIG] on F.Cu"}]},
                        {"type": "track_not_centered_on_via", "items": [{"description": "Via [SIG]"}]}],
         "unconnected_items": []}
    assert sum(DG.keys_per_net(d).values()) == 1 + 1
    assert sum(DG.keys_per_net(d, keep=lambda v: v.get("type") != "track_not_centered_on_via").values()) == 1


def test_tune_drc_report_keys_mask_polygon_bridges_by_net(monkeypatch):
    # PHASE3G G2a: one mask polygon named by different item pairs on each run -> keyed (type, net of other item)
    from collections import Counter
    def bridge(other_desc):
        return {"type": "solder_mask_bridge", "items": [
            {"description": "Polygon on F.Mask, length 1.0 mm"}, {"description": other_desc}]}
    run1 = {"violations": [bridge("Pad 1 of U1 [NET_A]"), bridge("Pad 2 of U1 [NET_A]")], "unconnected_items": []}
    run2 = {"violations": [bridge("Pad 7 of R3 [NET_A]"), bridge("Pad 9 of R4 [NET_A]")], "unconnected_items": []}
    for run in (run1, run2):
        monkeypatch.setattr(DG, "drc_json", lambda *a, _r=run, **k: _r)
        keys, counts = TU.drc_report("x.kicad_pcb")
        assert keys == Counter({("solder_mask_bridge", ("NET_A",)): 2}) and counts == (2, 0)


# ---- A1: the one DRC runner
def _fake_cli(calls, report=None):
    import json as _j
    import subprocess as _sp

    def run(cmd, **kw):
        out = cmd[cmd.index("--output") + 1]
        calls.append({"cmd": list(cmd), "dru": open(os.path.join(os.path.dirname(cmd[-1]), "b.kicad_dru")).read()
                      if os.path.isfile(os.path.join(os.path.dirname(cmd[-1]), "b.kicad_dru")) else None,
                      "pro": _j.load(open(os.path.join(os.path.dirname(cmd[-1]), "b.kicad_pro")))
                      if os.path.isfile(os.path.join(os.path.dirname(cmd[-1]), "b.kicad_pro")) else None,
                      "board": cmd[-1]})
        rep = dict(report or {"violations": [], "unconnected_items": []})
        if calls[-1]["dru"] and "fxcanary" in calls[-1]["dru"]:                       # a rule file loaded: the canary fires
            rep["violations"] = list(rep["violations"]) + [{"type": "assertion_failed", "severity": "error", "description": "Assertion failure (rule 'fxcanary')", "items": []}]
        _j.dump(rep, open(out, "w"))
        return _sp.CompletedProcess(cmd, 0, "", "")
    return run


def test_drc_json_always_refills_runs_on_a_copy(tmp_path, monkeypatch):
    b = tmp_path / "x.kicad_pcb"
    b.write_text("(kicad_pcb)")
    calls = []
    monkeypatch.setattr(DG.subprocess, "run", _fake_cli(calls))
    d = DG.drc_json(str(b))
    assert d["violations"] == []
    assert "--refill-zones" in calls[0]["cmd"] and "--severity-all" in calls[0]["cmd"]
    assert calls[0]["board"] != str(b)
    assert not [f for f in os.listdir(tmp_path) if f != "x.kicad_pcb"]


def test_drc_json_rules_text_reaches_cli_and_only_rules_ignores_the_rest(tmp_path, monkeypatch):
    import json as _j
    b = tmp_path / "x.kicad_pcb"
    b.write_text("(kicad_pcb)")
    (tmp_path / "x.kicad_pro").write_text(_j.dumps({"board": {"design_settings": {"rule_severities": {"clearance": "error",
                                                                                                   "skew_out_of_range": "ignore"}}}}))
    (tmp_path / "x.kicad_dru").write_text("(version 1)\n(rule \"old\")")
    calls = []
    monkeypatch.setattr(DG.subprocess, "run", _fake_cli(calls))
    DG.drc_json(str(b), rules="(version 1)\n(rule \"new\")", only_rules=["skew_out_of_range"])
    assert "new" in calls[0]["dru"] and "old" not in calls[0]["dru"]
    rs = calls[0]["pro"]["board"]["design_settings"]["rule_severities"]
    assert rs == {"clearance": "ignore", "skew_out_of_range": "error"}
    DG.drc_json(str(b))
    assert "old" in calls[1]["dru"]
    assert _j.load(open(tmp_path / "x.kicad_pro"))["board"]["design_settings"]["rule_severities"]["clearance"] == "error"


def test_drc_json_two_concurrent_calls_do_not_collide(tmp_path, monkeypatch):
    import threading
    b = tmp_path / "x.kicad_pcb"
    b.write_text("(kicad_pcb)")
    calls, res, errs = [], [], []
    monkeypatch.setattr(DG.subprocess, "run", _fake_cli(calls))

    def go():
        try:
            res.append(DG.drc_json(str(b)))
        except Exception as e:     # noqa
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and len(res) == 2
    assert len({c["board"] for c in calls}) == 2


def test_drc_json_no_report_raises(tmp_path, monkeypatch):
    import subprocess as _sp
    b = tmp_path / "x.kicad_pcb"
    b.write_text("(kicad_pcb)")
    monkeypatch.setattr(DG.subprocess, "run", lambda cmd, **k: _sp.CompletedProcess(cmd, 1, "", "boom"))
    with pytest.raises(RuntimeError):
        DG.drc_json(str(b))


def test_drc_json_retries_once_when_kicad_cli_writes_no_report(tmp_path):
    """critroute lost 10 of 13 tries to `DRC produced no report`: kicad-cli sometimes exits without writing under load."""
    b = tmp_path / "b.kicad_pcb"
    b.write_text("(kicad_pcb)")
    flag = tmp_path / "ran"
    cli = tmp_path / "fake-cli"
    cli.write_text("#!/bin/sh\nout=''\nwhile [ $# -gt 0 ]; do [ \"$1\" = --output ] && out=$2; shift; done\n"
                   f"if [ -f {flag} ]; then echo '{{\"violations\": [], \"unconnected_items\": []}}' > \"$out\"; else touch {flag}; fi\n")
    cli.chmod(0o755)
    d = DG.drc_json(str(b), str(cli), 30)
    assert d["violations"] == [] and flag.exists()
    cli.write_text("#!/bin/sh\nexit 3\n")
    with pytest.raises(RuntimeError, match=r"exited 3 with no report \(2 tries\)"):
        DG.drc_json(str(b), str(cli), 30)
