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
    for net, c, dia in vias:
        v = pcbnew.PCB_VIA(b)
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

