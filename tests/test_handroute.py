"""Hand-routing toolkit (fluxplace/handroute.py): geometry against hand-computed numbers, lay refusal, shove + re-lay through a gap
that fits two nets in one order only, whole-set commit refusal, locked copper. Boards are built with pcbnew; DRC is real kicad-cli."""
import json
import os
import shutil
import subprocess
import sys

import pytest

pcbnew = pytest.importorskip("pcbnew")
if not shutil.which("kicad-cli"):
    pytest.skip("kicad-cli missing", allow_module_level=True)

from fluxplace import handroute as H          # noqa: E402

PRO = {"meta": {"version": 3}, "board": {"design_settings": {"rules": {"min_clearance": 0.2, "min_track_width": 0.2, "min_via_diameter": 0.5,
                                                                      "min_hole_clearance": 0.2, "min_copper_edge_clearance": 0.3}}},
       "net_settings": {"classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3,
                                     "diff_pair_gap": 0.25, "diff_pair_width": 0.2, "microvia_diameter": 0.3, "microvia_drill": 0.1,
                                     "wire_width": 6, "bus_width": 12, "line_style": 0, "schematic_color": "rgba(0, 0, 0, 0.000)",
                                     "pcb_color": "rgba(0, 0, 0, 0.000)", "priority": 2147483647}],
                        "meta": {"version": 4}, "netclass_assignments": None, "netclass_patterns": []}}


def mm(v):
    return int(round(v * 1e6))


def V(x, y):
    return pcbnew.VECTOR2I(mm(x), mm(y))


class Brd:
    def __init__(self):
        self.b = pcbnew.CreateEmptyBoard()
        self.b.SetCopperLayerCount(2)
        self.nets = {}
        self.n = 0

    def net(self, name):
        if name not in self.nets:
            n = pcbnew.NETINFO_ITEM(self.b, name)
            self.b.Add(n)
            self.nets[name] = n
        return self.nets[name]

    def edge(self, x0, y0, x1, y1):
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        for i in range(4):
            s = pcbnew.PCB_SHAPE(self.b)
            s.SetShape(pcbnew.SHAPE_T_SEGMENT)
            s.SetStart(V(*pts[i]))
            s.SetEnd(V(*pts[(i + 1) % 4]))
            s.SetLayer(pcbnew.Edge_Cuts)
            s.SetWidth(mm(0.1))
            self.b.Add(s)

    def pad(self, ref, net, x, y, w=0.5, h=0.5):
        self.n += 1
        fp = pcbnew.FOOTPRINT(self.b)
        fp.SetReference(ref)
        fp.SetPosition(V(x, y))
        p = pcbnew.PAD(fp)
        p.SetNumber("1")
        p.SetShape(pcbnew.PAD_SHAPE_RECT)
        p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        ls = pcbnew.LSET()
        ls.AddLayer(pcbnew.F_Cu)
        p.SetLayerSet(ls)
        p.SetSize(pcbnew.VECTOR2I(mm(w), mm(h)))
        p.SetPosition(V(x, y))
        p.SetNet(self.net(net))
        fp.Add(p)
        self.b.Add(fp)
        return fp

    def track(self, net, pts, w=0.25, layer=pcbnew.F_Cu, locked=False):
        out = []
        for a, c in zip(pts, pts[1:]):
            t = pcbnew.PCB_TRACK(self.b)
            t.SetStart(V(*a))
            t.SetEnd(V(*c))
            t.SetWidth(mm(w))
            t.SetLayer(layer)
            t.SetNet(self.net(net))
            t.SetLocked(locked)
            self.b.Add(t)
            out.append(t)
        return out

    def save(self, d, name="b"):
        path = os.path.join(str(d), name + ".kicad_pcb")
        pcbnew.SaveBoard(path, self.b)
        json.dump(PRO, open(os.path.join(str(d), name + ".kicad_pro"), "w"))
        return path


def gap_board(d, a_locked=False):
    """Wall of two pads (edges y = -0.65 / +0.65 at x = 10), net A straight through the middle (y = 0), net B open on the lower row."""
    B = Brd()
    B.edge(-2, -8, 22, 8)
    B.pad("W1", "WU", 10, 3.65, 1.0, 6.0)          # lower edge of the upper wall pad: 0.65
    B.pad("W2", "WL", 10, -3.65, 1.0, 6.0)         # upper edge of the lower wall pad: -0.65
    B.pad("A1", "A", 2, 0)
    B.pad("A2", "A", 18, 0)
    B.pad("B1", "B", 4, -3)
    B.pad("B2", "B", 16, -3)
    B.track("A", [(2, 0), (18, 0)], locked=a_locked)
    return B.save(d)


LOWER = "F.Cu: 4,-3 6.7,-0.3 13.3,-0.3 16,-3"
UPPER = "F.Cu: 2,0 2.3,0.3 17.7,0.3 18,0"


def test_edit_guard_scopes_only_foreign_via_centring_flicker():
    from collections import Counter
    foreign = ("track_not_centered_on_via", ("Track [GND_ISO] on F.Cu, length # mm", "Via [GND_ISO] on F.Cu - B.Cu"))
    own = ("track_not_centered_on_via", ("Track [USB_DP_CM5] on F.Cu, length # mm",))
    other = ("clearance", ("Track [GND_ISO] on F.Cu, length # mm",))
    new = Counter({foreign: 1, own: 1, other: 1})
    kept = H._edit_violation_keys(new, "USB_DP_CM5")
    assert foreign not in kept and kept[own] == 1 and kept[other] == 1
    assert H._edit_violation_keys(Counter({foreign: 1}), "GND_ISO") == {foreign: 1}
    assert H._edit_violation_keys(Counter({foreign: 1}), "GND") == {}


def test_net_only_suggest_chooses_closest_distinct_islands():
    import types

    def pad(x):
        return H.G.Ob("pad", "N", ("F.Cu",), ("poly", [(x - 0.2, -0.2), (x + 0.2, -0.2), (x + 0.2, 0.2), (x - 0.2, 0.2)]))

    obs = [pad(0), pad(5), pad(9)]
    islands = [{"items": [obs[0]], "zones": []}, {"items": [obs[1]], "zones": []}, {"items": [obs[2]], "zones": []}]
    model = types.SimpleNamespace(obs=obs, islands=lambda net: (islands, {}))
    ia, ib, pa, pb = H._auto_endpoints(model, "N", islands)
    assert (ia, ib) == (1, 2)
    assert pa[0] < pb[0] and round(pb[0] - pa[0], 1) == 3.6


def test_suggest_requires_both_intended_endpoint_islands():
    pr = {"ends_join": [{"touches": {"island": 1}}, {"touches": {"island": 2}}]}
    assert H._endpoints_join(pr, 1, 2)
    assert not H._endpoints_join(pr, 1, 3)
    assert not H._endpoints_join({"ends_join": [{"touches": {"island": 1}}, {"touches": None}]}, 1, 2)
    assert H._endpoints_join(pr, None, None)


def test_route_constraints_raise_hand_route_via_floors(tmp_path):
    B = Brd()
    B.edge(-2, -8, 22, 8)
    B.pad("A1", "A", 2, 0)
    path = B.save(tmp_path)
    cons = tmp_path / "rules.toml"
    cons.write_text("[route]\nmin_via_mm = 0.53\nmin_drill_mm = 0.31\n")
    M = H._model(path, str(cons))
    assert M.floors["via_dia"] == 0.53 and M.floors["via_drill"] == 0.31
    assert M.net_rule("A")["via_dia"] >= 0.53


def test_apply_reuses_exact_same_net_via(tmp_path):
    B = Brd()
    B.edge(-2, -8, 22, 8)
    B.pad("A1", "A", 2, 0)
    v = pcbnew.PCB_VIA(B.b)
    v.SetPosition(V(5, 5))
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetWidth(mm(0.5))
    v.SetDrill(mm(0.3))
    v.SetNet(B.net("A"))
    B.b.Add(v)
    path = B.save(tmp_path)
    M = H._model(path)
    prims = H.G.parse_path("via 5,5", M.cu)
    added = H._apply_prims(M.b, M, "A", prims, 0.25, (0.5, 0.3))
    assert added[0][0] == "via-existing" and len(list(M.b.GetTracks())) == 1


def test_probe_clearance_numbers(tmp_path):
    p = gap_board(tmp_path)
    # B straight at y = -0.3 next to A at y = 0: edge gap = 0.3 - 0.125 - 0.125 = 0.05 (need 0.2)
    R = H.probe(p, "B", "F.Cu: 6.7,-0.3 13.3,-0.3", width=0.25)
    assert not R["ok"]
    v = [x for x in R["violations"] if x["kind"] == "clearance" and x["with"] == "track A"]
    assert v and abs(v[0]["gap"] - 0.05) < 2e-3 and abs(v[0]["need"] - 0.2) < 1e-6
    # exactly at the rule: y = -0.45 -> gap 0.45 - 0.25 = 0.2 -> OK, margin 0 (nearest item reported)
    R = H.probe(p, "B", "F.Cu: 6.0,-0.45 9.0,-0.45", width=0.25)
    assert R["ok"], R["violations"]
    assert abs(R["items"][0]["nearest"]["gap"] - 0.2) < 2e-3
    # a crossing is flagged as a short
    R = H.probe(p, "B", "F.Cu: 6.0,-1 6.0,1", width=0.25)
    assert any(x["kind"] == "crossing" for x in R["violations"])
    # 45/90 legality and width floor
    R = H.probe(p, "B", "F.Cu: 4,-3 5,-3.7", width=0.25)
    assert any(x["kind"] == "angle_not_45_90" for x in R["violations"])
    assert not H.probe(p, "B", "F.Cu: 4,-3 5,-3", width=0.1)["ok"]


def test_window_free_corridor_and_via_sites(tmp_path):
    p = gap_board(tmp_path)
    W = H.window(p, at=(10, 0), size=(6, 4), net="B", width=0.25, pitch=0.05, free_net="B")
    # the gap is y in (-0.65, 0.65): A occupies it; free corridors on F.Cu exist outside, none threads the wall at x=10
    fc = W["free_corridors"]["F.Cu"]
    assert fc and all(c["max_track_mm"] > 0 for c in fc)
    ids = {t["id"] for t in W["tracks"]}
    assert len(ids) == 1 and W["tracks"][0]["net"] == "A" and W["tracks"][0]["locked"] is False
    assert {p_["ref"] for p_ in W["pads"]} >= {"W1.1", "W2.1"}
    # via sites: legal sites keep via radius 0.3 + 0.2 from the wall (|y| < 0.65 is a gap of 1.3 -> no via fits between pads and A)
    for s in W["via_sites"]["sites"]:
        assert not (abs(s[0] - 10) < 0.5 and abs(s[1]) < 0.65)


def test_open_nets_lists_endpoints(tmp_path):
    p = gap_board(tmp_path)
    R = H.open_nets(p)
    assert R["open"] == 1
    c = R["connections"][0]
    assert c["net"] == "B" and abs(c["straight_mm"] - 12.0) < 0.01 and c["protected"] is False
    assert {c["a"]["pin"], c["b"]["pin"]} == {"B1.1", "B2.1"}
    assert abs(c["rules"]["clearance"] - 0.2) < 1e-6


def test_lay_refused_writes_nothing(tmp_path):
    p = gap_board(tmp_path)
    out = str(tmp_path / "o.kicad_pcb")
    r = H.lay(p, "B", LOWER, out=out, width=0.25)
    assert not r["accepted"] and "probe failed" in r["reason"]
    assert not os.path.exists(out)


def test_shove_relay_order_and_commit(tmp_path):
    p = gap_board(tmp_path)
    work = str(tmp_path / "work.kicad_pcb")
    s = H.session_begin(p, work)
    assert s["unconnected"] == 1
    # B cannot go through while A sits in the middle
    assert not H.lay(work, "B", LOWER, width=0.25)["accepted"]
    ids = [t["id"] for t in H.window(work, at=(10, 0), size=(4, 2))["tracks"]]
    r = H.shove(work, "A", ids=ids, chain=True)
    assert r["accepted"] and len(r["opened"]) == 1 and r["opened"][0]["a"]["pin"] in ("A1.1", "A2.1")
    # commit now would leave A open and B open: refused (unconnected 1 -> 2)
    c = H.session_commit(work, str(tmp_path / "bad.kicad_pcb"))
    assert not c["accepted"] and not os.path.exists(str(tmp_path / "bad.kicad_pcb"))
    # lay B first, then A above it: both fit, in that order
    assert H.lay(work, "B", LOWER, width=0.25)["accepted"]
    assert H.lay(work, "A", UPPER, width=0.25)["accepted"]
    st = H.session_status(work)
    assert st["now"]["unconnected"] == 0 and st["attempts"] >= 4
    # a bad intermediate: undo the A re-lay, commit sees A open -> total 1 (not lower than the start's 1)
    assert H.session_undo(work)["undone"]
    c = H.session_commit(work, str(tmp_path / "bad2.kicad_pcb"))
    assert not c["accepted"] and any("not lower" in x or "lost connectivity" in x for x in c["reasons"])
    assert H.lay(work, "A", UPPER, width=0.25)["accepted"]
    c = H.session_commit(work, str(tmp_path / "good.kicad_pcb"))
    assert c["accepted"], c["reasons"]
    assert c["work"]["unconnected"] == 0 and c["start"]["unconnected"] == 1
    led = open(work + ".session/ledger.txt").read()
    assert "REJECT" in led and "ACCEPT" in led and "commit" in led


def test_locked_copper_never_touched(tmp_path):
    p = gap_board(tmp_path, a_locked=True)
    work = str(tmp_path / "w.kicad_pcb")
    H.session_begin(p, work)
    W = H.window(work, at=(10, 0), size=(4, 2))
    assert W["tracks"][0]["locked"] is True
    r = H.shove(work, "A", ids=[W["tracks"][0]["id"]], chain=True)
    assert not r["accepted"] and "locked" in r["reason"]
    r = H.shove(work, "A", window_box=(8, -1, 4, 2))
    assert not r["accepted"] and "locked" in r["reason"]
    R = H.open_nets(work)
    assert R["connections"][0]["protected"] is False          # the locked net A is closed; B's own copper is not locked
    # the working board is byte-identical in its copper: A's track is still there
    assert len(H.window(work, at=(10, 0), size=(4, 2))["tracks"]) == 1


def test_cli_json_roundtrip(tmp_path):
    p = gap_board(tmp_path)
    cli = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cli.py")
    r = subprocess.run([sys.executable, cli, "probe", p, "--net", "B", "--path", "F.Cu: 6.0,-0.45 9.0,-0.45", "--width", "0.25", "--json"], capture_output=True, text=True)
    d = json.loads(r.stdout[r.stdout.index("{"):])
    assert r.returncode == 0 and d["ok"]
    r = subprocess.run([sys.executable, cli, "probe", p, "--net", "B", "--path", "F.Cu: 6.7,-0.3 13.3,-0.3", "--width", "0.25"], capture_output=True, text=True)
    assert r.returncode == 1 and "VIOLATION" in r.stdout


def test_stitch_site_lands_inside_small_island(monkeypatch):
    """PHASE3H-C: the stitch site search stays inside the split-off L-shaped island and starts at the pad nearest it."""
    def inside_L(x, y):
        return (0 <= x <= 2 and 0 <= y <= 0.5) or (0 <= x <= 0.5 and 0 <= y <= 2)
    sites = H._outline_sites(inside_L, (0, 0, 2, 2), ref=[(1.9, 0.2)], step=0.4, per=12)
    assert sites and all(inside_L(x, y) for x, y in sites)
    assert sites[0] == (1.8, 0.2)
    pair = H._pick_pair([(0.2, 0.2), (0.4, 0.2), (1.8, 0.2)], 0)
    assert pair == [(0.2, 0.2), (1.8, 0.2)]
    assert H._pick_pair([(0.2, 0.2)], 3) == []


def test_lay_stitch_false_is_unchanged_refusal(tmp_path, monkeypatch):
    """PHASE3H-C: stitch=False never reaches the stitch hook; a refused lay stays refused and writes nothing."""
    def boom(*a, **k):
        raise AssertionError("stitch hook must not run")
    monkeypatch.setattr(H, "_stitch_attempts", boom)
    p = gap_board(tmp_path)
    out = str(tmp_path / "o.kicad_pcb")
    r = H.lay(p, "B", LOWER, out=out, width=0.25, stitch=False)
    assert not r["accepted"] and "probe failed" in r["reason"] and "stitched" not in r
    assert not os.path.exists(out)
    # a refusal raised inside mutate never reaches the guard or the hook, with or without a stitch hook
    r2 = H._edit(p, str(tmp_path / "o2.kicad_pcb"), lambda b, M: (_ for _ in ()).throw(ValueError("refused")), "lay", "B", "x",
                 stitch=boom)
    assert not r2["accepted"] and r2["reason"] == "refused"
    assert not os.path.exists(str(tmp_path / "o2.kicad_pcb"))


def test_tmp_beside_is_unique_per_call(tmp_path):
    """Temp boards come from mkstemp beside the board, so repeated runs never share (or unlink) one slot."""
    p = gap_board(tmp_path)
    t1, t2 = H._tmp_beside(p, "hrtry"), H._tmp_beside(p, "hrtry")
    assert t1 != t2 and os.path.dirname(t1) == os.path.dirname(os.path.abspath(p))
    H._drop_tmp(t1)
    H._drop_tmp(t2)
