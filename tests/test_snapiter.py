import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from fluxplace import snapiter as SI  # noqa: E402


def test_same_input_and_output_board(tmp_path, monkeypatch):
    import shutil
    src = tmp_path / "b.kicad_pcb"
    src.write_text("board")
    (tmp_path / "b.kicad_pro").write_text("{}")
    monkeypatch.setattr(SI, "_report", lambda _board: {"violations": [], "unconnected_items": []})

    def snap(src, dst, skip):
        shutil.copy(src, dst)
        return 1, 0

    monkeypatch.setattr(SI, "_snap", snap)
    rec = SI.run(str(src), str(src))
    assert rec == {"moved": 1, "excluded": 0, "iters": 1, "centred_left": 0, "refused_geom": 0}
    assert sorted(os.listdir(tmp_path)) == ["b.kicad_pcb", "b.kicad_pro"]            # the trial board is not left behind


def test_zone_fill_flicker_does_not_reject_accepted_snap(tmp_path, monkeypatch):
    import shutil
    src = tmp_path / "b.kicad_pcb"
    src.write_text("board")
    (tmp_path / "b.kicad_pro").write_text("{}")
    reports = iter([
        {"violations": [{"type": "solder_mask_bridge", "items": [{"description": "Zone fill A"}]}], "unconnected_items": []},
        {"violations": [{"type": "solder_mask_bridge", "items": [{"description": "Zone fill B"}]}], "unconnected_items": []},
    ])
    monkeypatch.setattr(SI, "_report", lambda _board: next(reports))

    def snap(src, dst, skip):
        shutil.copy(src, dst)
        return 1, 0

    monkeypatch.setattr(SI, "_snap", snap)
    rec = SI.run(str(src), str(src))
    assert rec["moved"] == 1 and rec["centred_left"] == 0


def test_zone_clearance_increase_is_not_ignored_by_snap_guard(tmp_path, monkeypatch):
    import shutil
    src = tmp_path / "b.kicad_pcb"
    src.write_text("original")
    (tmp_path / "b.kicad_pro").write_text("{}")
    violation = {"type": "clearance", "items": [{"uuid": "track1", "description": "Track [N] on F.Cu"},
                                                {"uuid": "zone1", "description": "Zone [M] on In1.Cu"}]}
    reports = iter([
        {"violations": [violation], "unconnected_items": []},
        {"violations": [violation, violation], "unconnected_items": []},
        {"violations": [violation, violation], "unconnected_items": []},
    ])
    monkeypatch.setattr(SI, "_report", lambda _board: next(reports))
    def snap(src, dst, skip):
        shutil.copy(src, dst)
        return 1, 0
    monkeypatch.setattr(SI, "_snap", snap)
    rec = SI.run(str(src), str(src))
    assert rec["moved"] == 0 and rec["excluded"] == 2
    assert src.read_text() == "original"


def test_open_keys_count_per_net_not_description():
    from fluxplace import snapiter as SI, drcguard as DG
    def rep(desc):
        return {"violations": [], "unconnected_items": [{"type": "unconnected_items", "items": [
            {"description": desc}, {"description": "Track [NETA] on B.Cu, length 0.1500 mm"}]}]}
    base = SI._keys(rep("Pad 3 [NETA] of U1 on F.Cu"))
    moved = SI._keys(rep("Via [NETA] on F.Cu - B.Cu"))          # same open, different items after a snap
    assert not DG.new_keys(base, moved)
    two = rep("Pad 3 [NETA] of U1 on F.Cu"); two["unconnected_items"].append(
        {"type": "unconnected_items", "items": [{"description": "Pad 1 [NETB] of R1 on F.Cu"}, {"description": "Pad 2 [NETB] of R2 on F.Cu"}]})
    assert DG.new_keys(base, SI._keys(two))                      # a new open on another net is still caught


def test_snap_geometry_gap_to_foreign_via_and_track():
    """Pure geometry (no pcbnew): centre line 0.5 -> 10 mm on y = 10, width 0.2."""
    a, b = (5.0, 10.0), (10.0, 10.0)
    assert SI.obstacle_gap(a, b, 0.1, "via", ((10.45, 10.0), 0.3)) == pytest.approx(0.05)
    assert SI.obstacle_gap(a, b, 0.1, "seg", ((10.25, 9.0), (10.25, 11.0), 0.1)) == pytest.approx(0.05)
    assert SI.obstacle_gap(a, b, 0.1, "rect", (10.2, 9.0, 11.0, 11.0)) == pytest.approx(0.1)
    assert SI.obstacle_gap(a, b, 0.1, "rect", (9.0, 9.0, 11.0, 11.0)) < 0       # the segment is inside the pad


def _mini_board(path, tracks, vias, clearance_mm=0.12):
    """Synthetic board (pcbnew, in tmp_path). tracks: (net, layer, (x,y), (x,y), width, locked); vias: (net, (x,y), dia)."""
    import pcbnew
    b = pcbnew.CreateEmptyBoard()
    b.SetCopperLayerCount(2)
    b.GetDesignSettings().m_MinClearance = pcbnew.FromMM(clearance_mm)
    nets = {}
    for name in sorted({t[0] for t in tracks} | {v[0] for v in vias}):
        ni = pcbnew.NETINFO_ITEM(b, name)
        b.Add(ni)
        nets[name] = ni
    P = lambda x, y: pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
    out = []
    for net, layer, a, z, w, locked in tracks:
        t = pcbnew.PCB_TRACK(b)
        t.SetStart(P(*a))
        t.SetEnd(P(*z))
        t.SetWidth(pcbnew.FromMM(w))
        t.SetLayer(pcbnew.F_Cu if layer == "F" else pcbnew.B_Cu)
        t.SetNet(nets[net])
        t.SetLocked(locked)
        b.Add(t)
        out.append(t)
    for net, c, dia, *lk in vias:
        v = pcbnew.PCB_VIA(b)
        v.SetLocked(bool(lk and lk[0]))
        v.SetPosition(P(*c))
        v.SetWidth(pcbnew.F_Cu, pcbnew.FromMM(dia))
        v.SetDrill(pcbnew.FromMM(dia / 2))
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        v.SetNet(nets[net])
        b.Add(v)
    pcbnew.SaveBoard(str(path), b)


def _ends(path):
    import pcbnew
    b = pcbnew.LoadBoard(str(path))
    mm = pcbnew.ToMM
    return [((round(mm(t.GetStart().x), 3), round(mm(t.GetStart().y), 3)),
             (round(mm(t.GetEnd().x), 3), round(mm(t.GetEnd().y), 3)))
            for t in b.GetTracks() if t.Type() == pcbnew.PCB_TRACE_T]


def test_snap_off_centre_end_with_free_space_is_snapped(tmp_path):
    src, dst = tmp_path / "a.kicad_pcb", tmp_path / "b.kicad_pcb"
    _mini_board(src, [("A", "F", (5, 10), (9.9, 10.05), 0.2, False)], [("A", (10, 10), 0.6)])
    moved, refused = SI._snap(str(src), str(dst), set())
    assert (moved, refused) == (1, 0)
    assert _ends(dst) == [((5.0, 10.0), (10.0, 10.0))]


def test_snap_joint_moves_together(tmp_path):
    src, dst = tmp_path / "a.kicad_pcb", tmp_path / "b.kicad_pcb"
    _mini_board(src, [("A", "F", (5, 10), (9.9, 10.05), 0.2, False),
                      ("A", "F", (9.9, 10.05), (9.9, 14), 0.2, False)], [("A", (10, 10), 0.6)])
    moved, refused = SI._snap(str(src), str(dst), set())
    assert (moved, refused) == (2, 0)
    assert sorted(_ends(dst)) == sorted([((5.0, 10.0), (10.0, 10.0)), ((10.0, 10.0), (9.9, 14.0))])


def test_snap_locked_joint_stays(tmp_path):
    src, dst = tmp_path / "a.kicad_pcb", tmp_path / "b.kicad_pcb"
    _mini_board(src, [("A", "F", (5, 10), (9.9, 10.05), 0.2, False),
                      ("A", "F", (9.9, 10.05), (9.9, 14), 0.2, True)], [("A", (10, 10), 0.6)])
    moved, refused = SI._snap(str(src), str(dst), set())
    assert moved == 0
    assert sorted(_ends(dst)) == sorted([((5.0, 10.0), (9.9, 10.05)), ((9.9, 10.05), (9.9, 14.0))])



def test_snap_edit_runs_in_its_own_process(monkeypatch):
    """board.Remove() decays pcbnew's bindings for a process (LoadBoard then returns a raw SwigPyObject): snapiter.run must not do it in
    the caller's process, or the stage after it (close) crashes with `'SwigPyObject' object has no attribute 'GetTracks'`."""
    import fluxplace.freshproc as FP
    called = {}

    def spy(module, func, *a, **k):
        called["fresh"] = (module, func, a)
        return [3, 1]
    monkeypatch.setattr(FP, "in_fresh_process", spy)
    assert SI._snap is SI._SNAP_REAL
    assert SI._snap_fresh("x", "y", {"u2", "u1"}) == (3, 1)
    assert called["fresh"] == ("fluxplace.snapiter", "_snap_json", ("x", "y", ["u1", "u2"], False))


def test_snap_that_collapses_a_track_onto_another_leaves_no_duplicate_copper(tmp_path):
    """A track end 0.1 mm off a via centre is snapped; a second track already ends at the centre, so the moved one becomes an exact copy.
    The moved unlocked copy goes (one stays); boardwrite.verify then reports no DUPLICATE_COPPER.  A duplicate that was already on the
    board (counted in the protected manifest) is not this call's to drop."""
    from fluxplace import boardwrite as BW
    src = tmp_path / "a.kicad_pcb"
    _mini_board(src, [("A", "F", (5, 10), (10.1, 10), 0.2, False), ("A", "F", (5, 10), (10, 10), 0.2, False)], [("A", (10, 10), 0.6)])
    (tmp_path / "a.kicad_pro").write_text("{}")
    dst = tmp_path / "o.kicad_pcb"
    BW.seed(str(src))
    moved, refused = SI._snap_fresh(str(src), str(dst), set())
    assert moved == 1
    v = BW.verify(str(src), str(dst))
    assert v["ok"] and v["duplicates"] == 0, v
    assert _ends(dst) == [((5.0, 10.0), (10.0, 10.0))]
    src2 = tmp_path / "c.kicad_pcb"
    _mini_board(src2, [("A", "F", (5, 10), (10, 10), 0.2, False), ("A", "F", (5, 10), (10, 10), 0.2, False)], [("A", (10, 10), 0.6)])
    (tmp_path / "c.kicad_pro").write_text("{}")
    dst2 = tmp_path / "o2.kicad_pcb"
    SI._snap_fresh(str(src2), str(dst2), set())
    assert len(_ends(dst2)) == 2


def test_locked_stub_end_snaps_only_when_asked_and_stays_locked(tmp_path):
    """D71cz: a locked fan-out stub end off a via centre is snapped by the one-off post-route step (locked_ok), and the item stays locked."""
    import pcbnew
    src = tmp_path / "a.kicad_pcb"
    _mini_board(src, [("A", "F", (5, 10), (10.1, 10.05), 0.2, True)], [("A", (10, 10), 0.6)])
    (tmp_path / "a.kicad_pro").write_text("{}")
    moved, _r = SI._snap_fresh(str(src), str(tmp_path / "o0.kicad_pcb"), set())
    assert moved == 0
    moved, _r = SI._snap_fresh(str(src), str(tmp_path / "o1.kicad_pcb"), set(), True)
    assert moved == 1 and _ends(tmp_path / "o1.kicad_pcb") == [((5.0, 10.0), (10.0, 10.0))]
    t = [x for x in pcbnew.LoadBoard(str(tmp_path / "o1.kicad_pcb")).GetTracks() if x.Type() == pcbnew.PCB_TRACE_T][0]
    assert t.IsLocked()


def test_via_moves_to_the_track_end_when_the_end_cannot_move(tmp_path):
    """The clearance-refused case: both ends of the F track and the B track's end at the old centre sit on one point -> the via moves to it
    and the end at the old centre follows; a via with two different end points is skipped and reported."""
    import pcbnew
    src = tmp_path / "a.kicad_pcb"
    _mini_board(src, [("A", "F", (5, 10), (10.1, 10.05), 0.2, False), ("A", "B", (10, 10), (10, 15), 0.2, False)], [("A", (10, 10), 0.6)])
    moved, skipped, touched = SI._via_to_end(str(src), str(tmp_path / "o.kicad_pcb"), set(), None)
    assert moved == 1 and skipped == []
    b = pcbnew.LoadBoard(str(tmp_path / "o.kicad_pcb"))
    via = [x for x in b.GetTracks() if x.Type() == pcbnew.PCB_VIA_T][0]
    assert (round(pcbnew.ToMM(via.GetPosition().x), 3), round(pcbnew.ToMM(via.GetPosition().y), 3)) == (10.1, 10.05)
    assert ((10.1, 10.05), (10.0, 15.0)) in [((round(pcbnew.ToMM(x.GetStart().x), 3), round(pcbnew.ToMM(x.GetStart().y), 3)),
                                              (round(pcbnew.ToMM(x.GetEnd().x), 3), round(pcbnew.ToMM(x.GetEnd().y), 3)))
                                             for x in b.GetTracks() if x.Type() == pcbnew.PCB_TRACE_T]
    src2 = tmp_path / "c.kicad_pcb"
    _mini_board(src2, [("A", "F", (5, 10), (10.1, 10.05), 0.2, False), ("A", "F", (5, 12), (9.9, 10), 0.2, False)], [("A", (10, 10), 0.6)])
    moved, skipped, _t = SI._via_to_end(str(src2), str(tmp_path / "o2.kicad_pcb"), set(), None)
    assert moved == 0 and skipped and "different end points" in skipped[0][3]


def test_joiner_ties_an_off_centre_end_to_the_exact_via_centre_without_moving_either(tmp_path):
    """D71cz fallback: the via and the segment line stay; a same-net joiner (same layer, width, lock) runs from the end to the via centre."""
    import pcbnew
    src = tmp_path / "a.kicad_pcb"
    _mini_board(src, [("A", "F", (5, 10), (10.1, 10.05), 0.2, True)], [("A", (10, 10), 0.6)])
    assert SI._joiners(str(src), str(tmp_path / "o0.kicad_pcb"), set(), None)[:2] == [0, [["A", 10.0, 10.0, "locked"]]]   # locks hold by default
    assert SI._joiners(str(src), str(tmp_path / "o0.kicad_pcb"), set(), None, True, ["A"])[0] == 0      # a protected net is never lengthened
    added, skipped, touched = SI._joiners(str(src), str(tmp_path / "o.kicad_pcb"), set(), None, True)
    assert len(touched) == 2 and len(set(touched.values())) == 1                                      # the joiner and the segment map to the via
    assert added == 1 and skipped == []
    b = pcbnew.LoadBoard(str(tmp_path / "o.kicad_pcb"))
    r = lambda p: (round(pcbnew.ToMM(p.x), 3), round(pcbnew.ToMM(p.y), 3))
    ends = sorted((r(t.GetStart()), r(t.GetEnd()), t.IsLocked(), round(pcbnew.ToMM(t.GetWidth()), 3)) for t in b.GetTracks() if t.Type() == pcbnew.PCB_TRACE_T)
    assert ends == [((5.0, 10.0), (10.1, 10.05), True, 0.2), ((10.1, 10.05), (10.0, 10.0), True, 0.2)]
    via = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_VIA_T][0]
    assert r(via.GetPosition()) == (10.0, 10.0)


def test_via_move_leaves_locked_and_protected_items_alone(tmp_path):
    """A locked via, a via with a locked end at its centre, and a via of a protected net (pair leg, manifest net) do not move by default."""
    mk = lambda name, vl, tl: _mini_board(tmp_path / name, [("A", "F", (5, 10), (10.1, 10.05), 0.2, False), ("A", "B", (10, 10), (10, 15), 0.2, tl)],
                                          [("A", (10, 10), 0.6) + ((True,) if vl else ())])
    mk("lv.kicad_pcb", True, False)
    got = SI._via_to_end(str(tmp_path / "lv.kicad_pcb"), str(tmp_path / "o.kicad_pcb"), set(), None)
    assert got[0] == 0 and got[1][0][3] == "locked"
    mk("lt.kicad_pcb", False, True)
    src = str(tmp_path / "lt.kicad_pcb")
    got = SI._via_to_end(src, str(tmp_path / "o1.kicad_pcb"), set(), None)
    assert got[0] == 0 and got[1] == [["A", 10.0, 10.0, "locked"]] and got[2] == {}
    moved, skipped, touched = SI._via_to_end(src, str(tmp_path / "o2.kicad_pcb"), set(), None, True)
    assert moved == 1 and len(touched) == 2
    assert SI._via_to_end(src, str(tmp_path / "o3.kicad_pcb"), set(), None, True, ["A"])[0] == 0
    assert SI._via_to_end(src, str(tmp_path / "o4.kicad_pcb"), set(touched), None, True)[0] == 0      # a track the guard named keeps its via


def test_via_run_excludes_the_via_behind_a_new_violation_and_cleans_its_trials(tmp_path, monkeypatch):
    """The guard maps the items of a new violation (a moved end, a fresh joiner) back to their via through `touched`: the next iteration
    skips that via instead of repeating the same batch.  No via named by DRC: nothing runs.  Trial boards are removed."""
    import shutil
    import fluxplace.freshproc as FP
    src = tmp_path / "b.kicad_pcb"
    src.write_text("board")
    (tmp_path / "b.kicad_pro").write_text("{}")
    cen = {"type": SI.CENTRED, "items": [{"description": "Via [A] on F.Cu - B.Cu", "pos": {"x": 1, "y": 2}, "uuid": "v1"}]}
    clr = {"type": "clearance", "severity": "error", "description": "x", "items": [{"description": "Track [A] on F.Cu", "uuid": "j1"},
                                                                                 {"description": "Track [B] on F.Cu", "uuid": "o9"}]}
    reports = iter([{"violations": [cen], "unconnected_items": []}, {"violations": [clr], "unconnected_items": []},
                    {"violations": [cen], "unconnected_items": []}])
    monkeypatch.setattr(SI, "_report", lambda _b: next(reports))
    calls = []

    def fresh(module, func, s, d, skip, only, locked_ok, protect):
        calls.append((func, list(skip), only, locked_ok, protect))
        shutil.copy(s, d)
        return [0 if "v1" in skip else 1, [], {"j1": "v1"}]
    monkeypatch.setattr(FP, "in_fresh_process", fresh)
    rec = SI.joiner_run(str(src), str(tmp_path / "o.kicad_pcb"), log=lambda m: None, protect={"P"})
    assert calls[0] == ("_joiners", [], [["A", 1, 2]], False, ["P"]) and "v1" in calls[1][1]
    assert rec["moved"] == 0 and rec["excluded"] == 3 and rec["centred_left"] == 1
    assert not [f for f in os.listdir(tmp_path) if ".v0." in f or ".v1." in f]
    monkeypatch.setattr(SI, "_report", lambda _b: {"violations": [], "unconnected_items": []})
    assert SI.via_run(str(src), str(tmp_path / "o.kicad_pcb")) == {"moved": 0, "excluded": 0, "skipped": [], "centred_left": 0}
    assert len(calls) == 2


def _pro(d):
    import json
    (d / "a.kicad_pro").write_text(json.dumps({"board": {"design_settings": {"rule_severities": {SI.CENTRED: "error"}}}}))


def _post_stubs(monkeypatch):
    from fluxplace import krtchain as KC, planetie as PT, opengate as OG
    monkeypatch.setattr(PT, "run_guarded", lambda *a, **k: {"placed": 0, "per_net": {}})
    monkeypatch.setattr(OG, "refill", lambda *a, **k: 0)
    return KC


def _centred(path):
    return sum(v["type"] == SI.CENTRED for v in SI._report(str(path))["violations"])


def test_route_stage_post_snaps_an_off_centre_end_on_a_via(tmp_path, monkeypatch):
    """krtchain._post (the route stage's cleanup, before the route gate) moves a KRT-grid end onto the via centre; DRC is clean after."""
    KC = _post_stubs(monkeypatch)
    b = tmp_path / "a.kicad_pcb"
    _mini_board(b, [("A", "F", (5, 10), (10.1, 10.05), 0.2, False)], [("A", (10, 10), 0.6)])
    _pro(tmp_path)
    assert _centred(b) == 1
    rec = KC._post(str(b), str(tmp_path), [], {"track": 0.2, "clearance": 0.127, "via": 0.6, "drill": 0.3})
    assert rec["snap"]["centred_left"] == 0 and _centred(b) == 0
    assert _ends(b) == [((5.0, 10.0), (10.0, 10.0))]


def test_route_stage_post_guard_refuses_a_snap_that_would_short(tmp_path, monkeypatch):
    """A foreign-net track within clearance of the moved end: the end stays off-centre, no new clearance / short error appears."""
    KC = _post_stubs(monkeypatch)
    b = tmp_path / "a.kicad_pcb"
    _mini_board(b, [("A", "F", (14, 10), (10.14, 10), 0.2, False), ("B", "F", (9.65, 8), (9.65, 12), 0.2, False)], [("A", (10, 10), 0.3)])
    _pro(tmp_path)
    before = {v["type"] for v in SI._report(str(b))["violations"]}
    KC._post(str(b), str(tmp_path), [], {"track": 0.2, "clearance": 0.127, "via": 0.6, "drill": 0.3})
    after = {v["type"] for v in SI._report(str(b))["violations"]}
    assert not {"shorting_items", "clearance"} & (after - before)
    assert ((14.0, 10.0), (10.0, 10.0)) not in _ends(b)


def test_route_stage_post_runs_the_via_then_joiner_fallbacks_with_the_protected_nets(tmp_path, monkeypatch):
    KC = _post_stubs(monkeypatch)
    calls = []
    monkeypatch.setattr(SI, "run", lambda *a, **k: {"moved": 1, "centred_left": 5})
    monkeypatch.setattr(SI, "via_run", lambda *a, **k: calls.append(("via", k["protect"])) or {"centred_left": 2})
    monkeypatch.setattr(SI, "joiner_run", lambda *a, **k: calls.append(("joiner", k["protect"])) or {"centred_left": 0})
    rec = KC._post("b.kicad_pcb", str(tmp_path), [], {"track": 0.2, "clearance": 0.127, "via": 0.6, "drill": 0.3}, protect=["P"])
    assert calls == [("via", ["P"]), ("joiner", ["P"])] and rec["snap"]["centred_left"] == 0
    calls.clear()
    monkeypatch.setattr(SI, "run", lambda *a, **k: {"moved": 1, "centred_left": 0})
    KC._post("b.kicad_pcb", str(tmp_path), [], {"track": 0.2, "clearance": 0.127, "via": 0.6, "drill": 0.3})
    assert calls == []
