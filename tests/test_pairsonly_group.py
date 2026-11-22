"""PHASE3I B5b: a pair blocked by one complete (in-tolerance) pair is re-laid as a group: blocker whole, then the blocked
pair; kept only when both end complete, else the board is restored. Synthetic boards, fake lay, DRC injected."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import boardtext as BT
from fluxplace import pairsonly as PO
from fluxplace import routediag as RD
from test_pairsonly import bad_pair, duo, seg, no_drc


def _board(tmp_path):
    body = bad_pair("A", 20) + duo("UD", 60, 20, "D_P", "D_N") + duo("JD", 60, 40, "D_P", "D_N") \
        + seg((60, 20), (60, 40), "D_P") + seg((60.5, 20), (60.5, 40), "D_N")
    return PB.write(tmp_path, body)


def _lay_factory(d_broken=False):
    st = {"moved": False}

    def lay(board, cons, spec, only):
        flat = {x for pr in only for x in (pr if isinstance(pr, tuple) else (pr,))}
        if flat == {"D_P", "D_N"}:
            st["moved"] = True
            pcs = {"D_P": [("seg", "F.Cu", (60.0, 20.0), (60.0, 40.0), 0.15)],
                   "D_N": [("seg", "F.Cu", (60.5, 20.0), (60.5, 40.0), 0.15)]}
            if d_broken:
                pcs.pop("D_N")
            return {"pairs": [{"pair": ["D_N", "D_P"], "status": "laid"}], "laid": pcs, "strip": ["D_P", "D_N"], "kill": [],
                    "keep": [], "B": RD.load(board)}
        if not st["moved"]:
            e = {"pair": ["A_N", "A_P"], "status": "not-laid", "why": "no path", "blockers": ["track D_P F.Cu"],
                 "blocked_by": "no path | track D_P F.Cu"}
            return {"pairs": [e], "laid": {}, "strip": [], "kill": [], "keep": [], "B": RD.load(board)}
        pcs = {"A_P": [("seg", "F.Cu", (20.0, 20.0), (20.0, 40.0), 0.15)], "A_N": [("seg", "F.Cu", (20.5, 20.0), (20.5, 40.0), 0.15)]}
        return {"pairs": [{"pair": ["A_N", "A_P"], "status": "laid"}], "laid": pcs, "strip": ["A_P", "A_N"], "kill": [],
                "keep": [], "B": RD.load(board)}
    return lay


def test_blocked_pair_is_rerouted_as_a_group_with_its_blocker(tmp_path):
    src = _board(tmp_path)
    out = str(tmp_path / "o.kicad_pcb")
    rep = PO.reroute(src, out, ["A_P"], {}, None, drc=no_drc(), lay=_lay_factory(), log=lambda m: None)
    rows = {r["pair"]: r for r in rep["pairs"]}
    assert rows["A_N/A_P"]["status"] == "rerouted" and rows["A_N/A_P"]["grouped_with"] == "D_N/D_P"
    assert rows["D_N/D_P"]["status"] == "rerouted" and rows["D_N/D_P"]["grouped_with"] == "A_N/A_P"
    assert not [f for f in os.listdir(tmp_path) if ".grp" in f]        # group scratch files are removed


def test_group_refused_when_the_blocker_does_not_end_complete(tmp_path):
    src = _board(tmp_path)
    out = str(tmp_path / "o.kicad_pcb")
    rep = PO.reroute(src, out, ["A_P"], {}, None, drc=no_drc(), lay=_lay_factory(d_broken=True), log=lambda m: None)
    a = {r["pair"]: r for r in rep["pairs"]}["A_N/A_P"]
    assert a["status"] == "kept_original" and "group_refused" in a
    assert BT.blocks(open(src).read()) == BT.blocks(open(out).read())   # the board is restored exactly


def test_hard_locked_blocker_is_not_grouped_without_unlock(tmp_path):
    """PHASE3I F7: a blocker whose locked copper is hard (no constraints: fail closed) is not re-laid without unlock."""
    body = bad_pair("A", 20) + duo("UD", 60, 20, "D_P", "D_N") + duo("JD", 60, 40, "D_P", "D_N") \
        + seg((60, 20), (60, 40), "D_P", locked=True) + seg((60.5, 20), (60.5, 40), "D_N", locked=True)
    src = PB.write(tmp_path, body)
    out = str(tmp_path / "o.kicad_pcb")
    rep = PO.reroute(src, out, ["A_P"], {}, None, drc=no_drc(), lay=_lay_factory(), log=lambda m: None)
    a = {r["pair"]: r for r in rep["pairs"]}["A_N/A_P"]
    assert a["status"] == "kept_original" and "blocker hard-locked" in a["group_refused"]
    assert BT.blocks(open(src).read()) == BT.blocks(open(out).read())
