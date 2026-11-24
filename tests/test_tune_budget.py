"""tune budget, stall and clean exit: the best accepted board is written, temp and lock files are removed, and the
unfixable pairs (legs on different layer sequences / via counts) are named instead of tuned."""
import os
import signal
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from fluxplace import tune as TU
from test_lengthmatch import build_board, NAMES, _stackup, LM

pcbnew = pytest.importorskip("pcbnew")


def _fake_drc(monkeypatch, on_call=None, new_key_after=None):
    calls = []

    def fake(path, kicad_cli="kicad-cli", timeout=600):
        calls.append(path)
        if on_call:
            on_call(len(calls))
        keys = Counter({("v", (str(len(calls)),)): 1}) if new_key_after and len(calls) > new_key_after else Counter()
        return keys, (0, 0)
    monkeypatch.setattr(TU, "drc_report", fake)
    monkeypatch.setattr(TU, "classify_pairs", lambda b, p: {})
    return calls


def _leftovers(tmp_path):
    return sorted(p for p in os.listdir(tmp_path) if "work" in p or ".try" in p or p.endswith(".lck") or ".drc" in p)


def _ntracks(path):
    return len(list(pcbnew.LoadBoard(path).GetTracks()))


def _clock():
    t = [0.0]
    return t, (lambda: t[0])


def test_budget_stop_writes_best_accepted_board_and_cleans_up(tmp_path, monkeypatch):
    src = build_board(str(tmp_path / "in.kicad_pcb"))
    t, clock = _clock()
    _fake_drc(monkeypatch, on_call=lambda n: t.__setitem__(0, 1000.0) if n == 2 else None)   # budget gone after 1st accepted bump
    out, res = str(tmp_path / "out.kicad_pcb"), {}
    # two pairs: one accepted bump lands a pair inside its limit (PHASE3G G2b), so the budget ends on the second pair
    rep = TU.tune_pairs(src, out, {"DATA_1": "DATA_0", "DATA_3": "DATA_2"}, 0.2, budget=TU.Budget(100, clock=clock),
                        result=res, log=lambda m: None)
    assert res["stopped"] == "budget" and res["pairs"][0]["status"] == "ok"
    assert res["pairs"][1]["status"] == "over" and res["pairs"][1]["reason"] == "budget"
    assert _ntracks(out) > _ntracks(src)                       # the accepted step is in the output
    assert _leftovers(tmp_path) == []
    assert rep[0][0] == "DATA_0"


def test_sigterm_during_drc_leaves_a_usable_out_and_no_temp_files(tmp_path, monkeypatch):
    src = build_board(str(tmp_path / "in.kicad_pcb"))
    old = signal.getsignal(signal.SIGTERM)
    _fake_drc(monkeypatch, on_call=lambda n: os.kill(os.getpid(), signal.SIGTERM) if n == 3 else None)
    out, res = str(tmp_path / "out.kicad_pcb"), {}
    TU.tune_pairs(src, out, {"DATA_1": "DATA_0", "DATA_3": "DATA_2"}, 0.2, budget=TU.Budget(None), result=res,
                  log=lambda m: None)
    assert res["stopped"] == "signal" and os.path.exists(out)
    assert _ntracks(out) > _ntracks(src)                       # the first pair's accepted bump before the signal
    assert _leftovers(tmp_path) == [] and signal.getsignal(signal.SIGTERM) is old


def test_nothing_accepted_copies_the_input(tmp_path, monkeypatch):
    src = build_board(str(tmp_path / "in.kicad_pcb"))
    t, clock = _clock()
    _fake_drc(monkeypatch, on_call=lambda n: t.__setitem__(0, 1e6))     # baseline DRC eats the budget
    out = str(tmp_path / "out.kicad_pcb")
    res = {}
    TU.tune_pairs(src, out, {"DATA_1": "DATA_0"}, 0.2, budget=TU.Budget(10, clock=clock), result=res, log=lambda m: None)
    assert res["stopped"] == "budget" and _ntracks(out) == _ntracks(src) and _leftovers(tmp_path) == []
    assert res["accepted"] == 0 and open(out, "rb").read() == open(src, "rb").read()


def test_stall_stops_a_pair_after_n_rejected_tries(tmp_path, monkeypatch):
    src = build_board(str(tmp_path / "in.kicad_pcb"))
    calls = _fake_drc(monkeypatch, new_key_after=1)                     # every try adds a new DRC key: rejected
    out, res = str(tmp_path / "out.kicad_pcb"), {}
    TU.tune_pairs(src, out, {"DATA_1": "DATA_0"}, 0.2, stall=3, result=res, log=lambda m: None)
    assert len(calls) == 1 + 3                                          # baseline + 3 tries
    assert res["stopped"] == "stall" and res["pairs"][0]["reason"] == "stall"
    assert _ntracks(out) == _ntracks(src) and _leftovers(tmp_path) == []


def test_needs_reroute_is_reported_not_tuned(tmp_path, monkeypatch):
    src = build_board(str(tmp_path / "in.kicad_pcb"))
    calls = _fake_drc(monkeypatch)
    monkeypatch.setattr(TU, "classify_pairs", lambda b, p: {"DATA_0": "layers F.Cu vs F.Cu>B.Cu; vias 0 vs 1"})
    out, res = str(tmp_path / "out.kicad_pcb"), {}
    TU.tune_pairs(src, out, {"DATA_1": "DATA_0"}, 0.2, result=res, log=lambda m: None)
    assert res["pairs"][0]["status"] == "needs_reroute" and "vias 0 vs 1" in res["pairs"][0]["reason"]
    assert res["needs_reroute"] == 1 and len(calls) == 1                # only the baseline DRC ran


def test_classify_pairs_layer_and_via_mismatch_and_open_leg(monkeypatch):
    from fluxplace import pairaudit as PA, routediag as RD
    prof = {"A_P": {"path": True, "layers": ["F.Cu"], "vias": 0, "length": 9},
            "A_N": {"path": True, "layers": ["F.Cu", "In1.Cu", "F.Cu"], "vias": 2, "length": 9},
            "B_P": {"path": True, "layers": ["F.Cu"], "vias": 0, "length": 9},
            "B_N": {"path": True, "layers": ["F.Cu"], "vias": 0, "length": 9},
            "C_P": {"path": True, "layers": ["F.Cu"], "vias": 0, "length": 9},
            "C_N": {"path": False, "layers": [], "vias": 0, "length": 0}}
    monkeypatch.setattr(RD, "load", lambda p: {})
    monkeypatch.setattr(PA, "profile", lambda B, n: prof[n])
    out = TU.classify_pairs("x", {"A_N": "A_P", "B_N": "B_P", "C_N": "C_P"})
    assert set(out) == {"A_P", "C_P"} and "vias 0 vs 2" in out["A_P"] and "leg open" in out["C_P"]


def test_budget_default_scales_with_pairs():
    assert TU.default_budget(10) == 10 * TU.PER_PAIR_S and TU.default_budget(0) == TU.PER_PAIR_S


def test_tune_match_budget_stop_reports_not_reached(tmp_path, monkeypatch):
    src = build_board(str(tmp_path / "in.kicad_pcb"))
    t, clock = _clock()
    _fake_drc(monkeypatch, on_call=lambda n: t.__setitem__(0, 1e6) if n == 2 else None)
    g = LM.groups({"match": {"DATA": {"nets": ["DATA_\\d+"], "tolerance_mm": 0.2, "amplitude_mm": 0.6}}})[0]
    lens = lambda b, gg: LM.lengths_from_board(b, LM.members(gg, NAMES), _stackup(), [], False)
    tgt = max(v["mm"] for v in lens(pcbnew.LoadBoard(src), g).values())
    out, res = str(tmp_path / "out.kicad_pcb"), {}
    rep = TU.tune_match(src, out, [g], lens, {"DATA": (tgt, 0.2)}, budget=TU.Budget(50, clock=clock), result=res, log=lambda m: None)
    assert res["stopped"] == "budget" and any(r["status"] == "not_reached" for r in rep)
    assert len(rep) == len(NAMES) and _leftovers(tmp_path) == []


def test_cli_skips_zone_refill_when_no_track_was_accepted(monkeypatch):
    import cli
    from fluxplace import opengate as OG
    calls = []
    monkeypatch.setattr(OG, "refill", lambda path: calls.append(path))
    cli._tune_refill("unchanged", {"accepted": 0, "stopped": None})
    assert calls == []
    cli._tune_refill("changed", {"accepted": 1, "stopped": None})
    assert calls == ["changed"]


def test_lock_tuned_locks_only_the_tuned_pair_copper(tmp_path):
    """PHASE3E 8: the one lock helper both `tune` and the krt tune step use."""
    from fluxplace import tune as TU2
    p = tmp_path / "b.kicad_pcb"
    seg = '(segment\n\t\t(start 0 0)\n\t\t(end 1 0)\n\t\t(width 0.2)\n\t\t(layer "F.Cu")\n\t\t(net "%s")\n\t)\n'
    p.write_text("(kicad_pcb\n\t" + seg % "P" + "\t" + seg % "N" + "\t" + seg % "X" + ")\n")
    assert TU2.lock_tuned(str(p), {"P": "N"}) == 2
    from fluxplace import boardtext as BT
    txt = p.read_text()
    assert {BT.net_name(b) for _i, _j, _k, b in BT.scan(txt) if BT.is_locked(b)} == {"P", "N"}


# ------------------------------------------------ PHASE3G G2b: away-from-partner bumps, entry jog, lock flags
def _pcb_track(b, ni, x0, y0, x1, y1, w=0.15, locked=False):
    import pcbnew
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x0), pcbnew.FromMM(y0)))
    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
    t.SetLayer(pcbnew.F_Cu)
    t.SetWidth(pcbnew.FromMM(w))
    t.SetNet(ni)
    t.SetLocked(locked)
    b.Add(t)
    return t


def _pair_board(tmp_path, p_locked=False):
    """Two parallel F.Cu legs: P at y=10, its partner N at y=10.3 (0.15 mm apart edge to edge), x 10..40."""
    import pcbnew
    b = pcbnew.BOARD()
    nets = {}
    for n in ("P", "N", "X"):
        nets[n] = pcbnew.NETINFO_ITEM(b, n)
        b.Add(nets[n])
    _pcb_track(b, nets["P"], 10, 10, 40, 10, locked=p_locked)
    _pcb_track(b, nets["N"], 10, 10.3, 40, 10.3)
    return b, nets


def _add_pad(b, net, x, y):
    import pcbnew
    fp = pcbnew.FOOTPRINT(b)
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    pad = pcbnew.PAD(fp)
    pad.SetShape(pcbnew.PAD_SHAPE_RECT)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetLayerSet(pcbnew.LSET.FrontMask())
    pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.6), pcbnew.FromMM(0.6)))
    pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    pad.SetNet(net)
    fp.Add(pad)
    b.Add(fp)


def _track_ys(b, net):
    return [(t.GetStart().y / 1e6, t.GetEnd().y / 1e6) for t in b.GetTracks() if t.GetNetname() == net]


def test_meander_to_adds_exact_need_and_bumps_away_from_partner(tmp_path):
    from fluxplace import tune as TU
    b, _ = _pair_board(tmp_path)
    before = sum(t.GetLength() for t in b.GetTracks() if t.GetNetname() == "P") / 1e6
    added = TU.meander_to(b, "P", 1.0, amp_mm=0.6, period_mm=0.8, candidate=0, clearance_mm=0.1, away_from="N")
    assert added == pytest.approx(1.0, abs=0.01)
    after = sum(t.GetLength() for t in b.GetTracks() if t.GetNetname() == "P") / 1e6
    assert after - before == pytest.approx(1.0, abs=0.01)
    # every new point lies on the side of the original leg away from the partner (partner at +y)
    ys = [y for pair in _track_ys(b, "P") for y in pair]
    assert max(ys) <= 10.0 + 1e-6 and min(ys) < 9.9


def test_meander_to_refuses_locked_leg_unless_allowed_and_keeps_lock(tmp_path):
    from fluxplace import tune as TU
    b, _ = _pair_board(tmp_path, p_locked=True)
    assert TU.meander_to(b, "P", 1.0, clearance_mm=0.1, away_from="N") == 0.0      # default: locked copper is not edited
    added = TU.meander_to(b, "P", 1.0, clearance_mm=0.1, away_from="N", locked_ok=True)
    assert added == pytest.approx(1.0, abs=0.01)
    p_tracks = [t for t in b.GetTracks() if t.GetNetname() == "P"]
    assert len(p_tracks) > 1 and all(t.IsLocked() for t in p_tracks)              # replacement keeps the lock flag
    n_tracks = [t for t in b.GetTracks() if t.GetNetname() == "N"]
    assert len(n_tracks) == 1 and not n_tracks[0].IsLocked()                       # the partner is untouched


def test_entry_jog_adds_exact_need_at_a_pad_end(tmp_path):
    import pcbnew
    from fluxplace import tune as TU
    b, nets = _pair_board(tmp_path)
    _add_pad(b, nets["P"], 40, 10)
    added = TU.entry_jog(b, "P", 0.2, away_from="N", clearance_mm=0.1)
    assert added == pytest.approx(0.2, abs=0.01)
    ys = [y for pair in _track_ys(b, "P") for y in pair]
    assert min(ys) < 9.95 and max(ys) <= 10.0 + 1e-6                               # the bend is away from N


def test_entry_jog_refused_when_foreign_track_is_in_the_way(tmp_path):
    from fluxplace import tune as TU
    b, nets = _pair_board(tmp_path)
    # X runs through the 45-degree bend zone (offset ~0.24 mm on the side away from N)
    _pcb_track(b, nets["X"], 25, 9.76, 36, 9.76)
    _add_pad(b, nets["P"], 40, 10)
    n0 = len([t for t in b.GetTracks() if t.GetNetname() == "P"])
    assert TU.entry_jog(b, "P", 0.2, away_from="N", clearance_mm=0.1) == 0.0      # refused by the foreign track
    assert len([t for t in b.GetTracks() if t.GetNetname() == "P"]) == n0


def test_bump_polyline_fixed_side_puts_every_bump_on_one_side():
    from fluxplace import tune as TU
    pts = TU._bump_polyline(30.0, 3, 0.5, 0.8, side=-1)
    assert min(p[1] for p in pts) == pytest.approx(-0.5) and max(p[1] for p in pts) == pytest.approx(0.0)


# ------------------------------------------- PHASE3G G1b c: tune grades the pad-to-pad path (pairaudit), not raw copper
def _fcu_pad(b, net, x, y, sx=0.6, sy=0.6):
    import pcbnew
    fp = pcbnew.FOOTPRINT(b)
    fp.SetReference(f"U{len(b.GetFootprints())}")
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    pad = pcbnew.PAD(fp)
    pad.SetNumber("1")
    pad.SetShape(pcbnew.PAD_SHAPE_RECT)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetLayerSet(pcbnew.LSET.AllCuMask())
    pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(sx), pcbnew.FromMM(sy)))
    pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    pad.SetNet(net)
    fp.Add(pad)
    b.Add(fp)


def _path_board(tmp_path, name="pb.kicad_pcb"):
    """P: straight 20 mm leg at y=10. N: 20 mm leg at y=20 that runs 1 mm INTO its far pad (3 mm long, centre x=30):
    the track's end is inside the pad, so raw copper is 21 mm but the pad-to-pad path is 20 mm.
    S (y=30): a 20 mm leg with a 25 mm same-net stub branching at x=20 (the stub is off the pad-to-pad path)."""
    import pcbnew
    b = pcbnew.BOARD()
    nets = {n: pcbnew.NETINFO_ITEM(b, n) for n in ("P", "N", "S")}
    for n in nets.values():
        b.Add(n)
    _fcu_pad(b, nets["P"], 10, 10); _fcu_pad(b, nets["P"], 30, 10)
    _pcb_track(b, nets["P"], 10, 10, 30, 10)
    _fcu_pad(b, nets["N"], 10, 20); _fcu_pad(b, nets["N"], 30, 20, sx=3.0)
    _pcb_track(b, nets["N"], 10, 20, 30, 20)
    _pcb_track(b, nets["N"], 30, 20, 31, 20)
    _fcu_pad(b, nets["S"], 10, 30); _fcu_pad(b, nets["S"], 30, 30)
    _pcb_track(b, nets["S"], 10, 30, 20, 30)
    _pcb_track(b, nets["S"], 20, 30, 30, 30)
    _pcb_track(b, nets["S"], 20, 30, 20, 55)
    path = str(tmp_path / name)
    b.Save(path)
    return path


def test_path_len_counts_pad_to_pad_path_not_copper_inside_a_pad(tmp_path):
    from fluxplace import tune as TU
    import pcbnew
    src = _path_board(tmp_path)
    assert TU.path_len(src, "P") == pytest.approx(20.0, abs=0.01)
    assert TU.path_len(src, "N") == pytest.approx(TU.path_len(src, "P"), abs=0.01)   # equal on the audit's number
    raw_n = sum(t.GetLength() for t in pcbnew.LoadBoard(src).GetTracks() if t.GetNetname() == "N") / 1e6
    assert raw_n == pytest.approx(21.0, abs=0.01)                                       # the raw sum is not the path


def test_tune_pairs_leaves_pair_alone_when_path_lengths_agree(tmp_path, monkeypatch):
    from fluxplace import tune as TU
    src = _path_board(tmp_path)
    _fake_drc(monkeypatch)
    out, res = str(tmp_path / "out.kicad_pcb"), {}
    TU.tune_pairs(src, out, {"N": "P"}, 0.1, result=res, log=lambda m: None)
    row = res["pairs"][0]
    assert row["status"] == "ok" and row["skew"] == pytest.approx(0.0, abs=0.1)     # KiCad's number (in-pad copper counts)
    # KiCad is the length authority: the 1 mm branch the pad-to-pad walk ignores is copper KiCad counts, so the pair is tuned inside the limit
    assert _ntracks(out) >= _ntracks(src) and abs(row["skew"]) <= 0.1


def test_path_segs_excludes_same_net_stub_and_tune_ignores_it(tmp_path, monkeypatch):
    from fluxplace import tune as TU
    import pcbnew
    src = _path_board(tmp_path)
    keys = TU.path_segs(src, "S")
    stub = TU.seg_key((20, 30), (20, 55), "F.Cu")
    assert keys is not None and stub not in keys and len(keys) == 2                    # the two leg pieces only
    assert TU.path_len(src, "S") == pytest.approx(20.0, abs=0.01)                      # stub not counted on the path
    # meander_to with the path restriction bumps a leg piece, never the 25 mm stub (it is the longest straight)
    b = pcbnew.LoadBoard(src)
    before_stub = [t for t in b.GetTracks() if t.GetNetname() == "S" and t.GetLength() / 1e6 > 24.0]
    added = TU.meander_to(b, "S", 1.0, amp_mm=0.6, period_mm=0.8, candidate=0, clearance_mm=0.1, on_path=keys)
    assert added == pytest.approx(1.0, abs=0.02)
    assert [t for t in b.GetTracks() if t.GetNetname() == "S" and t.GetLength() / 1e6 > 24.0] == before_stub


# ---- A5: tune exits 0 for open / skipped pairs, non-zero only when a pair it worked on got worse
def test_tune_finish_open_and_over_but_not_worse_pairs_exit_zero(tmp_path):
    import cli
    from types import SimpleNamespace
    res = {"stopped": None, "pairs": [
        {"pair": "MIC_P/MIC_N", "status": "needs_reroute", "reason": "leg open", "skew": None, "before": None},
        {"pair": "U_P/U_N", "status": "unmeasured", "reason": "kicad-cli", "skew": None, "before": None},
        {"pair": "E_P/E_N", "status": "over", "skew": 0.6, "before": 0.9}]}
    assert cli.tune_worse(res) == []
    a = SimpleNamespace(report=str(tmp_path / "r.json"))
    assert cli._tune_finish(a, str(tmp_path / "o.kicad_pcb"), res, cli.tune_worse(res), "pair") == 0


def test_tune_finish_nonzero_when_a_tuned_pair_got_worse(tmp_path):
    import cli
    from types import SimpleNamespace
    res = {"stopped": None, "pairs": [{"pair": "E_P/E_N", "status": "over", "skew": 1.2, "before": 0.9}]}
    assert len(cli.tune_worse(res)) == 1
    assert cli._tune_finish(SimpleNamespace(report=str(tmp_path / "r.json")), str(tmp_path / "o"), res, cli.tune_worse(res), "pair") == 1
