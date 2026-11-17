"""pairs --only: re-route named pairs whole on a routed board; keep the original unless the new pair is connected, matched,
DRC-clean and opens nothing; refuse locked copper; --allow-shove; budget stop. Synthetic text boards, DRC injected."""
import os
import signal
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import planboards as PB
from fluxplace import boardtext as BT
from fluxplace import pairsonly as PO
from fluxplace import routediag as RD
from fluxplace import tune as TU

CAGE = (PB.keepout(17, 16.5, 23.5, 17) + PB.keepout(17, 23, 23.5, 23.5) + PB.keepout(17, 16.5, 17.5, 23.5) + PB.keepout(23, 16.5, 23.5, 23.5))


def duo(ref, x, y, p, n, pitch=0.5):
    return PB.fp(ref, x, y, [PB.pad("1", 0, 0, p, 0.3, 0.8), PB.pad("2", pitch, 0, n, 0.3, 0.8)])


def seg(a, b, net, layer="F.Cu", w=0.15, locked=False):
    lk = "(locked yes) " if locked else ""
    return f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) {lk}(layer "{layer}") (net "{net}") (uuid "u{abs(hash((a, b, net, layer))) % 10**9}"))\n'


def via(x, y, net):
    return f'(via (at {x} {y}) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net "{net}") (uuid "v{abs(hash((x, y, net))) % 10**9}"))\n'


def bad_pair(k, x, x2=None, locked=False):
    """pair k_P/k_N from U(x,20) to J(x2,40): P straight on F.Cu, N down through a via pair on B.Cu (layers and vias differ)."""
    x2 = x if x2 is None else x2
    P, N = f"{k}_P", f"{k}_N"
    body = duo(f"U{k}", x, 20, P, N) + duo(f"J{k}", x2, 40, P, N)
    body += seg((x, 20), (x2, 40), P, locked=locked)
    body += seg((x + 0.5, 20), (x + 0.5, 22), N) + via(x + 0.5, 22, N) + seg((x + 0.5, 22), (x2 + 0.5, 38), N, "B.Cu") \
        + via(x2 + 0.5, 38, N) + seg((x2 + 0.5, 38), (x2 + 0.5, 40), N)
    return body


def no_drc(counter=None):
    def f(path):
        if counter is not None:
            counter.append(path)
        return Counter(), (0, 0)
    return f


def left(tmp_path):
    return sorted(p for p in os.listdir(tmp_path) if ".work" in p or ".lck" in p or p.startswith("~"))


def test_resolve_names_prefix_and_unmatched():
    pairs = [("USB_DP", "USB_DM"), ("CSI_P", "CSI_N")]
    assert PO.resolve(["USB_DP/USB_DM"], pairs) == ([pairs[0]], [])
    assert PO.resolve(["CSI_N", "USB"], pairs) == ([pairs[1], pairs[0]], [])
    assert PO.resolve(["ZZ", "CSI*"], pairs) == ([pairs[1]], ["ZZ"])


def test_reroute_lays_one_coupled_pair_and_leaves_the_other_alone(tmp_path):
    src = PB.write(tmp_path, bad_pair("A", 20) + bad_pair("B", 50))
    out = str(tmp_path / "o.kicad_pcb")
    runs = []
    rep = PO.reroute(src, out, ["A_P/A_N"], {}, None, drc=no_drc(runs), log=lambda m: None)
    r = rep["pairs"][0]
    assert r["status"] == "rerouted", r
    assert r["before"]["audit"] == "mismatch" and r["after"]["audit"] == "complete"
    assert r["after"]["vias"] == [0, 0] and r["after"]["skew"] <= 0.3
    B = RD.load(out)
    assert not [v for v in B["vias"] if v["net"] == "A_N"]
    assert all(s["locked"] for s in B["segs"] if s["net"] in ("A_P", "A_N"))        # laid by the pair stage machinery: locked
    # pair B untouched, item for item
    b_in = {k: v for k, v in BT.blocks(open(src).read()).items() if '"B_' in v}
    b_out = {k: v for k, v in BT.blocks(open(out).read()).items() if '"B_' in v}
    assert b_in == b_out and len(b_in) == 6
    assert len(runs) == 2 and left(tmp_path) == []                                  # baseline + the candidate


def test_blocked_pair_restores_original_exactly(tmp_path):
    src = PB.write(tmp_path, duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 60, 40, "D_P", "D_N") + CAGE
                   + seg((20, 20), (60, 40), "D_P") + seg((20.5, 20), (20.5, 22), "D_N") + via(20.5, 22, "D_N")
                   + seg((20.5, 22), (60.5, 38), "D_N", "B.Cu") + via(60.5, 38, "D_N") + seg((60.5, 38), (60.5, 40), "D_N"))
    out = str(tmp_path / "o.kicad_pcb")
    rep = PO.reroute(src, out, ["D_P"], {}, None, drc=no_drc(), log=lambda m: None)
    r = rep["pairs"][0]
    assert r["status"] == "kept_original" and r["reason"].startswith("blocked corridor") and r["blockers"]
    assert BT.blocks(open(out).read()) == BT.blocks(open(src).read())              # item count and geometry
    assert left(tmp_path) == []


def test_locked_leg_is_refused_and_named(tmp_path):
    src = PB.write(tmp_path, bad_pair("A", 20, locked=True))
    rep = PO.reroute(src, str(tmp_path / "o.kicad_pcb"), ["A_P"], {}, None, drc=no_drc(), log=lambda m: None)
    r = rep["pairs"][0]
    assert r["status"] == "refused_locked" and "segment A_P F.Cu" in r["reason"] and r["locked"]
    assert BT.blocks(open(src).read()) == BT.blocks(open(str(tmp_path / "o.kicad_pcb")).read())


def test_new_drc_key_or_opened_net_rejects_the_candidate(tmp_path):
    src = PB.write(tmp_path, bad_pair("A", 20))
    n = []
    rep = PO.reroute(src, str(tmp_path / "o.kicad_pcb"), ["A_P"], {}, None,
                     drc=lambda p: (Counter() if not n.append(p) and len(n) == 1 else Counter({("clearance", ("x",)): 1}), (0, 0)),
                     log=lambda m: None)
    r = rep["pairs"][0]
    assert r["status"] == "kept_original" and r["reason"].startswith("new DRC violation")
    assert BT.blocks(open(src).read()) == BT.blocks(open(str(tmp_path / "o.kicad_pcb")).read())


def test_already_complete_pair_is_not_touched(tmp_path):
    body = duo("U1", 20, 20, "D_P", "D_N") + duo("J1", 20, 40, "D_P", "D_N") + seg((20, 20), (20, 40), "D_P") + seg((20.5, 20), (20.5, 40), "D_N")
    src = PB.write(tmp_path, body)
    rep = PO.reroute(src, str(tmp_path / "o.kicad_pcb"), ["D_P"], {}, None, drc=no_drc(), log=lambda m: None)
    assert rep["pairs"][0]["status"] == "already_ok"


def _fake_lay_factory(B_after_strip, blocked_first):
    """lay(): blocked (naming S0) on the first call, then a coupled straight pair, both as pairs.plan returns them."""
    calls = []

    def lay(board, cons, spec, only):
        calls.append(board)
        B = RD.load(board)
        if blocked_first and len(calls) == 1:
            e = {"pair": ["A_N", "A_P"], "status": "not-laid", "why": "no path", "blockers": ["track S0 F.Cu", "pad U9.1 (GND)"],
                 "blocked_by": "no path | track S0 F.Cu"}
            return {"pairs": [e], "laid": {}, "strip": [], "kill": [], "keep": [], "B": B}
        pcs = {"A_P": [("seg", "F.Cu", (20.0, 20.0), (20.0, 40.0), 0.15)], "A_N": [("seg", "F.Cu", (20.5, 20.0), (20.5, 40.0), 0.15)]}
        return {"pairs": [{"pair": ["A_N", "A_P"], "status": "laid"}], "laid": pcs, "strip": ["A_P", "A_N"], "kill": [], "keep": [], "B": B}
    lay.calls = calls
    return lay


def _shove_board(tmp_path):
    s0 = PB.fp("R1", 80, 70, [PB.pad("1", 0, 0, "S0"), PB.pad("2", 5, 0, "S0")]) + seg((80, 70), (85, 70), "S0")
    return PB.write(tmp_path, bad_pair("A", 20) + s0)


def test_blocked_names_neighbours_without_shove(tmp_path):
    src = _shove_board(tmp_path)
    rep = PO.reroute(src, str(tmp_path / "o.kicad_pcb"), ["A_P"], {}, None, drc=no_drc(), lay=_fake_lay_factory(None, True), log=lambda m: None)
    r = rep["pairs"][0]
    assert r["status"] == "kept_original" and r["shove_candidates"] == ["S0"] and "S0" in " ".join(r["blockers"])
    assert BT.blocks(open(src).read()) == BT.blocks(open(str(tmp_path / "o.kicad_pcb")).read())


def test_allow_shove_removes_the_named_neighbour_and_reports_the_open_net(tmp_path):
    src = _shove_board(tmp_path)
    out = str(tmp_path / "o.kicad_pcb")
    rep = PO.reroute(src, out, ["A_P"], {}, None, drc=no_drc(), lay=_fake_lay_factory(None, True), allow_shove=True, log=lambda m: None)
    r = rep["pairs"][0]
    assert r["status"] == "rerouted" and r["shoved"] == ["S0"] and r["opened"] == ["S0"]
    assert not [s for s in RD.load(out)["segs"] if s["net"] == "S0"]
    assert "OPENED S0" in PO.format_text(rep)


def test_shove_never_takes_pair_rf_or_power_nets():
    cls = {"A_P": {"class": "pair"}, "B_P": {"class": "pair"}, "ANT_RF": {"class": "rf"}, "VCC": {"class": "rail"}, "S1": {"class": "signal"}}
    shov, refused = PO.shove_nets(["track B_P F.Cu", "track ANT_RF F.Cu", "via VCC", "track S1 F.Cu", "track A_P F.Cu", "pad U1.1 (S1)"], ("A_P", "A_N"), cls, set())
    assert shov == ["S1"] and {n for n, _ in refused} == {"B_P", "ANT_RF", "VCC"}


def test_budget_stop_keeps_the_pairs_done_and_cleans_up(tmp_path):
    src = PB.write(tmp_path, bad_pair("A", 20) + bad_pair("B", 50))
    out = str(tmp_path / "o.kicad_pcb")
    t = [0.0]
    n = []

    def drc(p):
        n.append(p)
        if len(n) == 2:
            t[0] = 1e6                                          # the budget is gone once pair A is accepted
        return Counter(), (0, 0)
    rep = PO.reroute(src, out, ["A_P", "B_P"], {}, None, drc=drc, budget=TU.Budget(100, 0, clock=lambda: t[0]), log=lambda m: None)
    st = {r["pair"]: r["status"] for r in rep["pairs"]}
    assert rep["stopped"] == "budget" and st == {"A_N/A_P": "rerouted", "B_N/B_P": "not_reached"}
    assert not [v for v in RD.load(out)["vias"] if v["net"] == "A_N"] and [v for v in RD.load(out)["vias"] if v["net"] == "B_N"]
    assert left(tmp_path) == []


def test_sigterm_during_drc_writes_out_and_restores_handlers(tmp_path):
    src = PB.write(tmp_path, bad_pair("A", 20))
    out = str(tmp_path / "o.kicad_pcb")
    old = signal.getsignal(signal.SIGTERM)
    n = []

    def drc(p):
        n.append(p)
        if len(n) == 2:
            os.kill(os.getpid(), signal.SIGTERM)
        return Counter(), (0, 0)
    rep = PO.reroute(src, out, ["A_P"], {}, None, drc=drc, budget=TU.Budget(None, 0), log=lambda m: None)
    assert rep["stopped"] == "signal" and rep["pairs"][0]["status"] == "not_reached"
    assert BT.blocks(open(out).read()) == BT.blocks(open(src).read())          # the candidate was never accepted
    assert left(tmp_path) == [] and signal.getsignal(signal.SIGTERM) is old


@pytest.mark.skipif(not __import__("shutil").which("kicad-cli"), reason="kicad-cli not installed")
def test_real_drc_guard_path(tmp_path):
    src = PB.write(tmp_path, bad_pair("A", 20))
    rep = PO.reroute(src, str(tmp_path / "o.kicad_pcb"), ["A_P"], {}, None, log=lambda m: None)
    assert rep["pairs"][0]["status"] in ("rerouted", "kept_original") and left(tmp_path) == []
    if rep["pairs"][0]["status"] == "kept_original":
        assert rep["pairs"][0]["reason"].startswith("new DRC violation")        # synthetic board: the guard may say so; it never crashes
