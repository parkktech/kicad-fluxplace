"""PHASE3C: failed pairs regrouped per connector group (A groups, B regroup), the krt-whole `pairgroups` step (C.1),
route nets without failed pair legs (C.2), the gaps `pairgroup` rung (C.3). Synthetic boards; the router is never run."""
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import test_krtchain as TK  # noqa: E402  (reuses the krt-whole test context)
from fluxplace import constraints as C, gaps as GP, krtchain as K, pairsonly as PO, routediag as RD, netclass as NC  # noqa: E402
from fluxplace import pairaudit as PA, pairfb as FB  # noqa: E402


def pads(ref, pairs):
    """pads of one footprint: pairs = [(p_net, n_net), ...] (one P and one N pad each)."""
    out = []
    for p, n in pairs:
        out += [{"ref": ref, "net": p, "x": 0, "y": 0}, {"ref": ref, "net": n, "x": 0.5, "y": 0}]
    return out


def test_groups_connector_chip_and_lone_pair():
    B = {"pads": pads("X", [("A_P", "A_N"), ("B_P", "B_N")]) + pads("Y", [("C_P", "C_N")])
         + pads("Z", [("D_P", "D_N"), ("D_P", "D_N")])}
    B["pads"] += [{"ref": "Z", "net": "D_P", "x": 1, "y": 0}, {"ref": "Z", "net": "D_N", "x": 1, "y": 1}]
    g = PO.groups(B, {}, [("A_P", "A_N"), ("B_P", "B_N"), ("C_P", "C_N"), ("D_P", "D_N")])
    assert g == {"X": [("A_P", "A_N"), ("B_P", "B_N")], "C_P/C_N": [("C_P", "C_N")], "Z": [("D_P", "D_N")]}


def test_groups_tie_prefers_ref_with_more_pairs_then_name():
    # pair P1 has 2 pads on U1 and 2 on U2; U2 also holds pair P2 -> U2 has more pairs overall
    B = {"pads": pads("U1", [("P1_P", "P1_N")]) + pads("U2", [("P1_P", "P1_N")]) + pads("U2", [("P2_P", "P2_N")])}
    g = PO.groups(B, {}, [("P1_P", "P1_N"), ("P2_P", "P2_N")])
    assert "U2" in g and ("P1_P", "P1_N") in g["U2"]


def _fake_board(monkeypatch, tmp_path, done):
    src = tmp_path / "in.kicad_pcb"
    src.write_text("board")
    B = {"pads": pads("X", [("A_P", "A_N"), ("B_P", "B_N")]) + pads("Y", [("C_P", "C_N")])}
    monkeypatch.setattr(RD, "load", lambda p: B)
    monkeypatch.setattr(NC, "classify", lambda *a, **k: {})
    monkeypatch.setattr(PO, "_complete", lambda board, members, cons: len([m for m in members if m in done]))
    return str(src)


def test_regroup_keeps_better_and_reverts_worse(tmp_path, monkeypatch):
    done = set()
    src = _fake_board(monkeypatch, tmp_path, done)
    calls = []

    def fake_reroute(board, out, names, cons=None, spec=None, **kw):
        calls.append(list(names))
        rows = []
        for nm in names:
            ok = nm.startswith(("A_", "B_"))              # the X group lays; the Y lone pair does not
            if ok:
                done.add(tuple(nm.split("/")))
            rows.append({"pair": nm, "status": "rerouted" if ok else "kept_original", "reason": ""})
        shutil.copy(board, out)
        return {"pairs": rows}

    monkeypatch.setattr(PO, "reroute", fake_reroute)
    out = str(tmp_path / "out.kicad_pcb")
    r = PO.regroup(src, out, {}, None, [("A_P", "A_N"), ("B_P", "B_N"), ("C_P", "C_N")], log=lambda m: None)
    assert r["groups"]["X"]["laid"] == ["A_P/A_N", "B_P/B_N"] and r["groups"]["X"]["failed"] == []
    assert r["groups"]["C_P/C_N"]["failed"] == ["C_P/C_N"] and r["groups"]["C_P/C_N"]["gap_retry"] is False
    assert r["failed"] == [("C_P", "C_N")] and os.path.exists(out)
    assert calls[0] == ["A_P/A_N", "B_P/B_N"]                     # the bigger group first


def test_regroup_gap_retry_once_with_fab_min_clearance(tmp_path, monkeypatch):
    done = set()
    src = _fake_board(monkeypatch, tmp_path, done)
    monkeypatch.setattr(RD, "load", lambda p: {"pads": pads("X", [("USB_P", "USB_N")])})
    seen = []

    def fake_reroute(board, out, names, cons=None, spec=None, *a, **kw):
        gap = (cons or {}).get("pairs", {}).get("USB_", {}).get("gap_mm")
        seen.append((gap, list(names)))
        ok = gap is not None
        if ok:
            done.update(tuple(nm.split("/")) for nm in names)
        shutil.copy(board, out)
        return {"pairs": [{"pair": nm, "status": "rerouted" if ok else "kept_original", "reason": ""} for nm in names]}

    monkeypatch.setattr(PO, "reroute", fake_reroute)
    cons = {"pairs": {"USB_": {}}}
    r = PO.regroup(src, str(tmp_path / "o.kicad_pcb"), cons, None, [("USB_P", "USB_N")], log=lambda m: None)
    assert len(seen) == 2 and seen[0][0] is None and seen[1][0] == C.fab_profile(cons)["min_clearance_mm"]
    assert r["groups"]["USB_P/USB_N"]["gap_retry"] is True and r["groups"]["USB_P/USB_N"]["laid"] == ["USB_P/USB_N"]
    assert "gap_mm" not in cons["pairs"]["USB_"]                 # the caller's constraints are not changed


def test_family_gap_already_set_is_not_retried(tmp_path, monkeypatch):
    done = set()
    src = _fake_board(monkeypatch, tmp_path, done)
    monkeypatch.setattr(RD, "load", lambda p: {"pads": pads("X", [("USB_P", "USB_N")])})
    n = []

    def fake_reroute(b, o, names, *a, **kw):
        n.append(1)
        shutil.copy(b, o)
        return {"pairs": [{"pair": x, "status": "kept_original", "reason": ""} for x in names]}

    monkeypatch.setattr(PO, "reroute", fake_reroute)
    r = PO.regroup(src, str(tmp_path / "o.kicad_pcb"), {"pairs": {"USB_": {"gap_mm": 0.14}}}, None, [("USB_P", "USB_N")],
                   log=lambda m: None)
    assert len(n) == 1 and r["groups"]["USB_P/USB_N"]["gap_retry"] is False


def test_pairgroups_step_skips_without_incomplete_pair(tmp_path, monkeypatch):
    src = tmp_path / "in.kicad_pcb"
    src.write_text("board")
    monkeypatch.setattr(RD, "load", lambda p: {"pads": []})
    monkeypatch.setattr(NC, "classify", lambda *a, **k: {})
    monkeypatch.setattr(PA, "pair_list", lambda cls: [])
    c = type("C", (), {"cons": {}, "spec": None})()
    dst = str(tmp_path / "2-pairgroups.kicad_pcb")
    failed = set()
    assert K._run_pairgroups(c, str(tmp_path), str(src), dst, failed, 60.0) == 0
    assert open(dst).read() == "board" and failed == set()


def test_pairgroups_step_collects_failed_legs_and_survives_errors(tmp_path, monkeypatch):
    src = tmp_path / "in.kicad_pcb"
    src.write_text("board")
    monkeypatch.setattr(RD, "load", lambda p: {"pads": []})
    monkeypatch.setattr(NC, "classify", lambda *a, **k: {})
    monkeypatch.setattr(PA, "pair_list", lambda cls: [("A_P", "A_N")])
    monkeypatch.setattr(PA, "audit", lambda B, pairs, cons, opn: {("A_P", "A_N"): {"status": "open", "why": ""}})
    monkeypatch.setattr(K.RC, "text_open", lambda B: {})
    monkeypatch.setattr(PO, "regroup", lambda b, o, cons, spec, pairs, log, budget_s: (shutil.copy(b, o),
                        {"failed": [("A_P", "A_N")], "groups": {}})[1])
    c = type("C", (), {"cons": {}, "spec": None})()
    failed = set()
    assert K._run_pairgroups(c, str(tmp_path), str(src), str(tmp_path / "p.kicad_pcb"), failed, 60.0) == 0
    assert failed == {"A_P", "A_N"}
    monkeypatch.setattr(RD, "load", lambda p: 1 / 0)                 # any error: the board is copied, rc 0
    assert K._run_pairgroups(c, str(tmp_path), str(src), str(tmp_path / "q.kicad_pcb"), set(), 60.0) == 0
    assert open(tmp_path / "q.kicad_pcb").read() == "board"


def test_route_nets_exclude_failed_pair_legs(tmp_path, monkeypatch):
    c, calls, runner = TK._ctx(tmp_path, monkeypatch, [0, 0, 0])
    monkeypatch.setattr(K, "_run_pairgroups", lambda c_, d, s, o, failed, b, **kw: (failed.add("USB_DP"), shutil.copy(s, o), 0)[2])
    r = K.run(c, 5, str(tmp_path / "in.kicad_pcb"), runner)
    assert [s["step"] for s in r["steps"]] == ["plane-GND", "pairgroups", "route"]     # gated USB legs: no KRT diff (F1)
    route = calls[-1]
    nets = route[route.index("--nets") + 1:route.index("--layers")] if "--layers" in route else []
    assert "USB_DP" not in nets and "USB_DM" in nets and "X" in nets


def test_gaps_pairgroup_rung_and_dispatch(tmp_path, monkeypatch):
    assert GP.DISPATCH["pair-open"] == ["pairgroup", "ai"] and GP.DISPATCH["pair-asym"] == ["pairgroup", "ai"]   # PHASE3G G1
    assert GP.DISPATCH["pair-skew"] == ["pairgroup", "tune", "ai"] and GP.LEVEL["pairgroup"] == "L1"
    monkeypatch.setattr(RD, "load", lambda p: {})
    monkeypatch.setattr(NC, "classify", lambda *a, **k: {})
    monkeypatch.setattr(PA, "pair_list", lambda cls: [("A_P", "A_N"), ("Q_P", "Q_N")])
    gaps = [{"kind": "pair-open", "nets": ["A_N"]}]
    ctx = {"cons": {}, "log": lambda m: None, "budget_s": 60.0}
    base = tmp_path / "in.kicad_pcb"
    base.write_text("board")
    out = str(tmp_path / "close.kicad_pcb")
    monkeypatch.setattr(PO, "regroup", lambda b, o, cons, spec, pairs, log, budget_s: (
        shutil.copy(b, o), {"groups": {"X": {"laid": []}}, "failed": [pairs[0]]})[1])
    assert GP._r_pairgroup(str(base), out, gaps, ctx) == {"status": "nothing laid", "pairs": 1}
    assert not os.path.exists(out)                                  # no candidate board
    seen = []
    monkeypatch.setattr(PO, "regroup", lambda b, o, cons, spec, pairs, log, budget_s: (seen.append(pairs), shutil.copy(b, o),
                        {"groups": {"X": {"laid": ["A_P/A_N"]}}, "failed": []})[2])
    assert GP._r_pairgroup(str(base), out, gaps, ctx)["status"] == "ran" and seen == [[("A_P", "A_N")]]
    assert GP._r_pairgroup(str(base), out, [{"kind": "open-gap", "nets": ["A_N"]}], ctx) == {"status": "no pair"}
