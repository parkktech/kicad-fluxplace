"""W16 route-3 pairs-stage fixes (docs/PCIE-DIAG-W16.md): tier-ordered emission, tier-aware corridor reservations, report == board,
audit reconciliation tolerance, USB-C double pads."""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planboards as PB
from fluxplace import netlen, pairaudit as PA, pairfb as FB, pairneg, pairs as PR, routediag as RD

needs_cli = pytest.mark.skipif(not shutil.which("kicad-cli"), reason="kicad-cli not installed")


def duo(ref, x, y, p, n, pitch=0.5, w=0.3, h=0.8):
    return PB.fp(ref, x, y, [PB.pad("1", 0, 0, p, w, h), PB.pad("2", pitch, 0, n, w, h)])


def three_pairs():
    return "".join(duo(f"U{k}", 20, y, a, b) + duo(f"J{k}", 60, y, a, b)
                   for k, (y, a, b) in enumerate(((20, "PCIE_TX_P", "PCIE_TX_N"), (30, "PCIE_RX_P", "PCIE_RX_N"), (40, "MIC_P", "MIC_N"))))


def test_unsettled_pair_is_reordered_only_inside_its_own_tier(tmp_path, monkeypatch):
    real = pairneg.negotiate

    def neg(*a, **k):
        out, unresolved, used = real(*a, **k)
        return out, set(unresolved) | {pr for pr in {k_[0] for k_ in out} if pr[0].startswith("PCIE")}, used      # both HS pairs "unresolved"
    monkeypatch.setattr(pairneg, "negotiate", neg)
    res = PR.plan(PB.write(tmp_path, three_pairs()), {"pairlay": {"workers": 1}, "preroute": {"signal_layers": ["F.Cu"]}}, None, plan_only=True)
    assert res["negotiation"]["unresolved"], res["negotiation"]
    assert [o.split("_")[0] for o in res["order"]] == ["PCIE", "PCIE", "MIC"], res["order"]      # unresolved PCIE pairs stay ahead of the audio pair


def _captured_ctx(tmp_path, monkeypatch):
    seen = {}
    real = PR._lay_pair

    def spy(ctx, pr, *a, **k):
        seen.setdefault("ctx", ctx)
        return real(ctx, pr, *a, **k)
    monkeypatch.setattr(PR, "_lay_pair", spy)
    body = duo("U1", 20, 40, "MIC_P", "MIC_N") + duo("J1", 60, 40, "MIC_P", "MIC_N")
    PR.plan(PB.write(tmp_path, body), {"preroute": {"signal_layers": ["F.Cu"]}}, None, plan_only=True)
    ctx = seen["ctx"]
    pr = next(iter(ctx["info"]))
    return ctx, pr, ctx["info"][pr], ctx["rules"][pr]


@pytest.mark.parametrize("rel,hard", [(-1, True), (0, False), (1, False)])      # the reserving pair is a higher tier / a peer / a lower tier
def test_reserved_corridor_is_hard_only_against_a_lower_tier(tmp_path, monkeypatch, rel, hard):
    ctx, pr, info, rule = _captured_ctx(tmp_path, monkeypatch)
    q = ("PCIE_RES_P", "PCIE_RES_N")
    own = ctx["tier"](pr)
    res_tier = (own[0], own[1] + rel)
    ctx["reserve"] = {q: [("seg", "F.Cu", (40.0, -10.0), (40.0, 90.0), 0.8)]}      # a wall across the straight line
    ctx["done"] = set()
    ctx["tier"] = lambda x: own if x == pr else res_tier
    out = PR._lay_pair(ctx, pr, info, rule, True)
    if hard:                                                  # a higher-tier pair's reservation closes the lane to a lower-tier pair
        assert not out["full"] and "corridor of PCIE_RES_P/PCIE_RES_N" in " ".join(out["e"].get("blockers", []) + [out["e"].get("blocked_by", "")])
    else:                                                     # a peer's or lower tier's reservation never blocks (soft cost only)
        assert out["full"] and out["e"]["status"] == "planned", (out["e"]["status"], out["e"].get("why"), out["e"]["sections"])


def _row(a, b, status, open_=(0, 0), state=None):
    return {"pair": [a, b], "status": status, "open": list(open_), "state": state, "tried": [], "key": [0, 0, 1.0], "blockers": [], "why": ""}


def test_fallback_trial_that_lays_a_pair_as_mismatch_does_not_leave_it_state_c(tmp_path):
    rep = {"pairs": [_row("X_P", "X_N", "open", (2, 2)), _row("Y_P", "Y_N", "open", (2, 2))]}
    out = tmp_path / "b.kicad_pcb"
    out.write_text("board")

    def run_fn(src, cons, spec, trial, only=None, fallback=True):                    # one trial lays X (mismatch) and Y (complete)
        open(trial, "w").write("trial")
        return {"pairs": [_row("X_P", "X_N", "mismatch"), _row("Y_P", "Y_N", "complete")]}
    FB.assign_states(rep)
    FB.ladder(rep, str(out), {}, None, PR.params({}), run_fn)
    st = {r["pair"][0]: r for r in rep["pairs"]}
    assert st["Y_P"]["state"] == "b(i)" and st["X_P"]["state"] == "a-mismatch" and st["X_P"]["status"] == "mismatch" and not st["X_P"].get("handoff")
    assert out.read_text() == "trial"                                                # the kept board carries both pairs' copper


def test_reconcile_regrades_every_row_and_hands_off_only_pairs_without_copper():
    rep = {"pairs": [_row("A_P", "A_N", "open", (2, 2), "c") | {"handoff": True}, _row("B_P", "B_N", "open", (2, 2), "c") | {"handoff": True},
                     _row("C_P", "C_N", "open", (2, 2), "c") | {"handoff": True}]}
    prof = {"length": 10.0, "vias": 1, "layers": ["F.Cu"]}
    aud = {("A_P", "A_N"): {"status": "mismatch", "why": "skew", "p": prof, "n": prof, "skew": 9.0},
           ("B_P", "B_N"): {"status": "open", "why": ""},
           ("C_P", "C_N"): {"status": "unknown", "why": "MEASURE_DISAGREE"}}
    opn = {"B_P": 2, "B_N": 2}
    FB.reconcile(rep, aud, opn)
    st = {r["pair"][0]: r for r in rep["pairs"]}
    assert (st["A_P"]["state"], st["A_P"]["handoff"], st["A_P"]["status"], st["A_P"]["open"]) == ("a-mismatch", False, "mismatch", [0, 0])
    assert (st["C_P"]["state"], st["C_P"]["handoff"]) == ("a-mismatch", False)         # ungraded copper is not an open pair
    assert (st["B_P"]["state"], st["B_P"]["handoff"], st["B_P"]["open"]) == ("c", True, [2, 2])


def test_unknown_with_both_legs_connected_is_copper_not_pending():
    rep = {"pairs": [_row("E_P", "E_N", "unknown", (0, 0)), _row("F_P", "F_N", "open", (1, 1))]}
    FB.assign_states(rep)
    assert [r["state"] for r in rep["pairs"]] == ["a-mismatch", None]


def test_recon_tolerance_grows_with_vias_and_pads():
    two = {"n_vias": 0, "n_pads": 2}
    eth = {"n_vias": 3, "n_pads": 4}
    assert netlen.recon_tol(two, two) == netlen.RECON_TOL_MM
    assert netlen.recon_tol(two, eth) > 0.0525 and netlen.recon_tol(eth, eth) < 0.2


@needs_cli
def test_eth_case_recon_miss_of_0_0525_mm_is_measured_not_unknown(tmp_path, monkeypatch):
    from test_pairskew import board
    B = board(tmp_path)
    real = netlen.pair_lengths
    monkeypatch.setattr(netlen, "pair_lengths", lambda *a, **k: {k_: dict(v, p=v["p"] + 0.0525) for k_, v in real(*a, **k).items()})
    r = netlen.pair_skew(B, "USB_DM", "USB_DP", {})
    assert r["status"] == "ok" and abs(r["recon"]["p"]) > netlen.RECON_TOL_MM and r["recon"]["tol"] > abs(r["recon"]["p"])
    a = PA.audit(B, [("USB_DM", "USB_DP")], {"pairs": {"USB_": {"skew_mm": 9.0}}}, set())[("USB_DM", "USB_DP")]
    assert a["status"] in ("complete", "mismatch")
    monkeypatch.setattr(netlen, "pair_lengths", lambda *a, **k: {k_: dict(v, p=v["p"] + 0.2) for k_, v in real(*a, **k).items()})
    assert netlen.pair_skew(B, "USB_DM", "USB_DP", {})["status"] == "MEASURE_DISAGREE"      # a real miss still is


def usbc_body():
    """J5: USB-C receptacle row, DM DP DM DP at 0.5 mm; U8 two pads 14 mm north."""
    row = [PB.pad(str(i + 1), 0.5 * i, 0, net, 0.3, 0.8) for i, net in enumerate(("USB_DM", "USB_DP", "USB_DM", "USB_DP"))]
    return PB.fp("J5", 40, 60, row) + duo("U8", 40.5, 45, "USB_DP", "USB_DM", pitch=0.8)


def test_usbc_interleaved_double_pads_make_one_terminal_with_a_twin(tmp_path):
    B = RD.load(PB.write(tmp_path, usbc_body()))
    raw, _ = PR.terminals(B, "USB_DP", "USB_DM", 16.0)
    merged, lone = PR.terminals(B, "USB_DP", "USB_DM", 16.0, merge_twins=True)
    assert len([t for t in raw if t["name"] == "J5"]) == 2 and len([t for t in merged if t["name"] == "J5"]) == 1 and not lone
    j5 = next(t for t in merged if t["name"] == "J5")
    assert j5["twin"]["p"]["net"] == "USB_DP" and j5["twin"]["n"]["net"] == "USB_DM" and j5["twin"]["p"] is not j5["p"]
    assert PR.has_twin_pads(PB.write(tmp_path, usbc_body(), name="c.kicad_pcb"), {}, None, PR.params({}))
    assert PR.params({})["reserve_exits"] is None and PR.params({"pairlay": {"reserve_exits": False}})["reserve_exits"] is False


def test_usbc_pair_joins_each_nets_two_pads_and_leaves_coupled(tmp_path):
    b = PB.write(tmp_path, usbc_body())
    out = str(tmp_path / "o.kicad_pcb")
    rep = PR.run(b, {"pairlay": {"workers": 1}}, None, out, fallback=False)
    row = rep["pairs"][0]
    assert row["plan"] == "laid" and row["open"] == [0, 0] and row["status"] in ("complete", "mismatch"), row
    assert any(s.get("vias") is not None for s in row["sections"])
    B = RD.load(out)
    from fluxplace import routeconn as RC
    assert not any(RC.text_open(B).get(n) for n in ("USB_DM", "USB_DP"))             # both pads of each net are one connected net


def test_usbc_reservation_for_fanout_is_on_by_default_and_carries_the_join(tmp_path):
    b = PB.write(tmp_path, usbc_body())
    r = PR.reserve_for_fanout(b, b, {"pairlay": {"workers": 1}}, None, str(tmp_path))
    assert r and set(r[2]) == {"USB_DM", "USB_DP"} and r[1]
    plain = PB.write(tmp_path, duo("U1", 20, 20, "A_P", "A_N") + duo("J1", 40, 20, "A_P", "A_N"), name="p.kicad_pcb")
    assert PR.reserve_for_fanout(plain, plain, {}, None, str(tmp_path)) is None              # no double pads: opt-in as before
