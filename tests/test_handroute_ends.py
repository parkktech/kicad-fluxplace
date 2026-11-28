"""suggest carries its path through the final via and onto the target pad (layer and centre); synthetic 2-layer boards."""
import math
import shutil

import pytest

pcbnew = pytest.importorskip("pcbnew")
if not shutil.which("kicad-cli"):
    pytest.skip("kicad-cli missing", allow_module_level=True)

from fluxplace import handroute as H          # noqa: E402
from test_handroute import Brd, V, mm         # noqa: E402


def _pad(B, ref, net, x, y, w, h, layer):
    fp = B.pad(ref, net, x, y, w, h)
    p = fp.Pads()[0]
    ls = pcbnew.LSET()
    ls.AddLayer(layer)
    p.SetLayerSet(ls)
    return fp


def _end_board(d, ring=False, wall=False, tgt=(18.02, 0.03)):
    B = Brd()
    B.edge(-2, -8, 24, 8)
    _pad(B, "A1", "A", 2, 0, 0.5, 0.5, pcbnew.F_Cu)
    _pad(B, "A2", "A", tgt[0], tgt[1], 1.5, 1.5, pcbnew.B_Cu if ring else pcbnew.F_Cu)
    if ring:    # copper ring on the target layer 0.3 mm outside the pad: the pad is reachable only by a via inside it
        cx, cy = tgt
        for i, (dx, dy, w, h) in enumerate(((0, -1.55, 3.5, 1.0), (0, 1.55, 3.5, 1.0), (-1.55, 0, 1.0, 3.5), (1.55, 0, 1.0, 3.5))):
            _pad(B, f"R{i}", "RING", cx + dx, cy + dy, w, h, pcbnew.B_Cu)
    if wall:
        B.track("X", [(10, -7), (10, 7)], layer=pcbnew.F_Cu)
        B.track("X", [(10, -7), (10, 7)], layer=pcbnew.B_Cu)
    return B.save(d)


def _last(path):
    chunk = path.split(";")[-1]
    lay, pts = chunk.split(":") if ":" in chunk else (None, None)
    x, y = pts.split()[-1].split(",")
    return lay.strip(), float(x), float(y)


def test_suggest_ends_on_target_pad_layer_and_centre(tmp_path):
    p = _end_board(tmp_path, ring=True)
    R = H.suggest(p, "A", src_pt=(2, 0), dst_pt=(18.02, 0.03), pitch=0.05, margin=3.0)
    assert R["found"], R
    assert "via" in R["path"] and R["vias"] == 1
    lay, x, y = _last(R["path"])
    assert lay == "B.Cu" and math.hypot(x - 18.02, y - 0.03) < 1e-3
    assert H.probe(p, "A", R["path"])["ok"]


def test_suggest_extends_grid_end_to_pad_centre(tmp_path):
    p = _end_board(tmp_path)
    R = H.suggest(p, "A", src_pt=(2, 0), dst_pt=(18.02, 0.03), pitch=0.05, margin=3.0)
    assert R["found"], R
    lay, x, y = _last(R["path"])
    assert lay == "F.Cu" and math.hypot(x - 18.02, y - 0.03) < 1e-3
    first = R["path"].split(";")[0].split(":")[1].split()[0].split(",")
    assert math.hypot(float(first[0]) - 2, float(first[1])) < 1e-3


def test_explore_reports_then_found_for_corridor_needing_extension(tmp_path):
    p = _end_board(tmp_path, ring=True, wall=True)
    R = H.suggest(p, "A", src_pt=(2, 0), dst_pt=(18.02, 0.03), pitch=0.05, margin=3.0, explore=True)
    assert not R["found"]
    ok = [e for e in R["explore"] if e["then_found"]]
    assert ok and ok[0]["remove"][0]["net"] == "X" and ok[0]["path"]


def test_explore_reports_and_probes_whole_connected_blocker_run(tmp_path):
    board = Brd()
    board.edge(-2, -8, 24, 8)
    board.pad("A1", "A", 2, 0)
    board.pad("A2", "A", 18, 0)
    board.track("X", [(10, -7), (10, 0), (10, 7)])
    src = board.save(tmp_path)
    r = H.suggest(src, "A", src_pt=(2, 0), dst_pt=(18, 0), layers=["F.Cu"], pitch=0.05, explore=True)
    found = [e for e in r["explore"] if e["then_found"]]
    assert len(found) == 1 and len(found[0]["remove"]) == 2
    expected = {x["uuid"] for x in found[0]["remove"]}
    out = str(tmp_path / "shoved.kicad_pcb")
    shoved = H.shove(src, "X", out=out, ids=[found[0]["remove"][0]["uuid"]], chain=True)
    assert shoved["accepted"], shoved.get("reason")
    assert {x["uuid"] for x in shoved["info"]["removed"]} == expected
    assert H.probe(out, "A", found[0]["path"])["ok"]


def test_probe_clear_dir_and_shift_in_summary(tmp_path):
    from fluxplace import hrcli
    from test_handroute import gap_board
    p = gap_board(tmp_path)
    R = H.probe(p, "B", "F.Cu: 6.7,-0.3 13.3,-0.3", width=0.25)      # A runs at y = 0: gap 0.05 of 0.2 -> 0.15 mm toward -y
    v = [x for x in R["violations"] if x["kind"] == "clearance" and x["with"] == "track A"][0]
    assert v["clear_dir"] == [0.0, -1.0] and v["clear_shift_mm"] == 0.15
    assert hrcli.summary("probe", R).endswith("move 0.15 mm toward -y") or "move 0.15 mm toward -y" in hrcli.summary("probe", R)
    # a diagonal path: shifting along clear_dir by clear_shift_mm cures it
    R = H.probe(p, "B", "F.Cu: 4,-1 6,-3", width=0.25)
    c = [x for x in R["violations"] if x["kind"] == "clearance"]
    for x in c:
        assert abs(math.hypot(*x["clear_dir"]) - 1) < 1e-3 and x["clear_shift_mm"] > 0
        assert (x["clear_shift_mm"] * 100) % 1 < 1e-6 or abs((x["clear_shift_mm"] * 100) % 1 - 1) < 1e-6


def _copy_pair(d):
    import os
    os.makedirs(os.path.join(d, "s"))
    os.makedirs(os.path.join(d, "d"))
    srcs, dsts = [], []
    for name, sub in (("s", srcs), ("d", dsts)):
        B = Brd()
        B.edge(-2, -8, 24, 8)
        _pad(B, "A1", "A", 2, 0, 0.5, 0.5, pcbnew.F_Cu)
        _pad(B, "A2", "A", 18, 0, 0.5, 0.5, pcbnew.F_Cu)
        _pad(B, "Z1", "Z", 2, 4, 0.5, 0.5, pcbnew.F_Cu)
        _pad(B, "Z2", "Z", 18, 4, 0.5, 0.5, pcbnew.F_Cu)
        B.track("Z", [(2, 4), (18, 4)])
        if name == "s":
            B.track("A", [(2, 0), (6, 0)])
            B.track("A", [(6, 0), (14, 0)], layer=pcbnew.B_Cu)
            B.track("A", [(14, 0), (18, 0)])
            for x in (6, 14):
                v = pcbnew.PCB_VIA(B.b)
                v.SetPosition(V(x, 0))
                v.SetWidth(mm(0.6))
                v.SetDrill(mm(0.3))
                v.SetNet(B.net("A"))
                B.b.Add(v)
        else:
            B.track("A", [(2, 0), (18, 0)])
        sub.append(B.save(os.path.join(d, name)))
    return srcs[0], dsts[0]


def test_copy_net_restores_exact_count_by_net_name(tmp_path):
    s, dst = _copy_pair(str(tmp_path))
    out = str(tmp_path / "o.kicad_pcb")
    R = H.copy_net(s, dst, "A", out=out)
    assert R["accepted"], R.get("reason")
    assert R["info"] == {"removed": 1, "copied": 5, "source": s}
    b = pcbnew.LoadBoard(out)
    mine = [t for t in b.GetTracks() if t.GetNetname() == "A"]
    assert len(mine) == 5 and all(t.GetNetCode() == b.FindNet("A").GetNetCode() for t in mine)
    assert sum(1 for t in b.GetTracks() if t.GetNetname() == "Z") == 1


def test_copy_net_refuses_locked_and_writes_nothing(tmp_path):
    import os
    s, dst = _copy_pair(str(tmp_path))
    b = pcbnew.LoadBoard(dst)
    for t in b.GetTracks():
        if t.GetNetname() == "A":
            t.SetLocked(True)
    pcbnew.SaveBoard(dst, b)
    out = str(tmp_path / "o.kicad_pcb")
    R = H.copy_net(s, dst, "A", out=out)
    assert not R["accepted"] and "locked" in R["reason"] and not os.path.exists(out)
    R = H.copy_net(s, dst, "NOPE", out=out)
    assert not R["accepted"] and not os.path.exists(out)


def test_via_min_raises_all_small_vias_bisecting_on_drc(tmp_path):
    from collections import Counter
    from fluxplace import viamin
    B = Brd()
    B.edge(-2, -8, 24, 8)
    for x, d in ((1, 0.4), (5, 0.45), (9, 0.6), (13, 0.3)):
        v = pcbnew.PCB_VIA(B.b)
        v.SetPosition(V(x, 0))
        v.SetWidth(mm(d))
        v.SetDrill(mm(0.2))
        B.b.Add(v)
    src = B.save(tmp_path)
    out = str(tmp_path / "o.kicad_pcb")

    def fake_drc(path, cli):          # the via at x = 5 may not be raised: DRC "sees" a new violation then
        b = pcbnew.LoadBoard(path)
        k = Counter()
        for t in b.GetTracks():
            if t.GetPosition().x == mm(5) and t.GetWidth() >= mm(0.5):
                k[("hole_clearance", ("x",))] += 1
        return k, (sum(k.values()), 0)

    r = viamin.via_min(src, out, report=fake_drc)
    assert (r["floor_mm"], r["vias"], r["below"], r["changed"], r["kept"], r["rejected"]) == (0.5, 4, 3, 2, 1, 1)
    assert r["rejected_at"] == [[5.0, 0.0]]
    w = {round(t.GetPosition().x * 1e-6): round(t.GetWidth() * 1e-6, 3) for t in pcbnew.LoadBoard(out).GetTracks()}
    assert w == {1: 0.5, 5: 0.45, 9: 0.6, 13: 0.5}


def test_via_min_budget_writes_verified_prefix_and_marks_remainder(tmp_path):
    import os
    import time
    from collections import Counter
    from fluxplace import viamin
    B = Brd()
    B.edge(-2, -8, 24, 8)
    for x in (1, 5, 9):
        v = pcbnew.PCB_VIA(B.b)
        v.SetPosition(V(x, 0))
        v.SetWidth(mm(0.4))
        v.SetDrill(mm(0.2))
        B.b.Add(v)
    src = B.save(tmp_path)
    out = str(tmp_path / "partial.kicad_pcb")
    calls = []

    def fake_drc(path, cli):
        calls.append(path)
        time.sleep(0.02)
        raised = [t for t in pcbnew.LoadBoard(path).GetTracks() if t.GetWidth() >= mm(0.5)]
        return Counter({("clearance", ("all-at-once",)): 1}) if len(raised) == 3 else Counter(), (0, 0)

    r = viamin.via_min(src, out, report=fake_drc, budget=0.06)
    assert r["changed"] == 1 and r["rejected"] == 0 and r["not_reached"] == 2
    assert r["budget_exhausted"] and os.path.exists(out)
    assert [t.GetWidth() for t in pcbnew.LoadBoard(out).GetTracks()] == [mm(0.5), mm(0.4), mm(0.4)]
    assert len(calls) == 3


def _stub_board(d):
    """A1 (2, 0) and A2 (18, 0) on F.Cu, a foreign F.Cu wall at x = 10; A2 carries fan-out stubs with joints every 0.15 mm."""
    B = Brd()
    B.edge(-2, -8, 24, 8)
    _pad(B, "A1", "A", 2, 0, 0.5, 0.5, pcbnew.F_Cu)
    _pad(B, "A2", "A", 18, 0, 0.5, 0.5, pcbnew.F_Cu)
    B.track("X", [(10, -7), (10, 7)], layer=pcbnew.F_Cu)
    B.track("A", [(18, 0), (17.4, 0), (17.25, 0), (17.1, 0), (16.7, 0.04), (16.2, 0.04)], locked=True)
    return B.save(d)


ENDS = [(18, 0), (17.4, 0), (17.25, 0), (17.1, 0), (16.7, 0.04), (16.2, 0.04)]


def test_no_via_where_an_existing_own_net_track_would_end_inside_it(tmp_path):
    """W16 BT4_RF: the path's last via sat on the fan-out stubs of the target island; three of their ends lay inside the via off its
    centre. The probe said OK, KiCad said track_not_centered_on_via, and the lay guard refused all four candidates."""
    p = _stub_board(tmp_path)
    M = H._model(p, None)
    bad = H.G.parse_path("F.Cu: 2,0 3,0; via 3,0; B.Cu: 3,0 17.2,0; via 17.2,0", M.cu)          # via 0.05 / 0.1 mm off two stub ends
    pr = H.probe_prims(M, "A", bad)
    assert any(v["kind"] == "track_not_centered_on_via" and v.get("with") == "existing track end" for v in pr["violations"])
    R = H.suggest(p, "A", src_pt=(2, 0), dst_pt=(18, 0), pitch=0.05, margin=3.0, M=M)
    assert R["found"], R.get("reason")
    vias = [q[1] for q in H.G.parse_path(R["path"], M.cu) if q[0] == "via"]
    assert vias                                                                                    # the wall forces B.Cu
    for v in vias:
        assert not [e for e in ENDS if 0.0005 < math.hypot(e[0] - v[0], e[1] - v[1]) < R["via"][0] / 2], (v, R["path"])
    out = str(tmp_path / "o.kicad_pcb")
    res = H.lay(p, "A", R["path"], out=out)
    assert res["accepted"], res.get("reason")
