"""D71cv: skew target vs maximum, per-path skew and spur length on a pair with more than two terminals, KiCad as length authority."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace import constraints as C, pairaudit as PA


def pad(ref, num, net, x, y):
    return {"ref": ref, "num": num, "net": net, "x": x, "y": y, "w": 0.4, "h": 0.4, "shape": "rect", "layers": ["F.Cu"], "angle": 0.0}


def seg(net, a, b):
    return {"net": net, "layer": "F.Cu", "a": a, "b": b, "w": 0.15}


def board():
    """U1 (one pad per net) -> junction -> two rows J1.A (short spur) and J1.B (spur 3 / 4 mm: N leg takes a 1 mm detour)."""
    pads = [pad("U1", "1", "USB_DP", 0, 0), pad("U1", "2", "USB_DM", 0, 0.8),
            pad("J1", "A6", "USB_DP", 10, 0), pad("J1", "A7", "USB_DM", 10, 0.8),
            pad("J1", "B6", "USB_DP", 10, 2), pad("J1", "B7", "USB_DM", 10, 2.8)]
    segs = [seg("USB_DP", (0, 0), (9, 0)), seg("USB_DP", (9, 0), (10, 0)), seg("USB_DP", (9, 0), (9, 2)), seg("USB_DP", (9, 2), (10, 2)),
            seg("USB_DM", (0, 0.8), (9, 0.8)), seg("USB_DM", (9, 0.8), (10, 0.8)), seg("USB_DM", (9, 0.8), (9, 2.8)),
            seg("USB_DM", (9, 2.8), (10.5, 2.8)), seg("USB_DM", (10.5, 2.8), (10, 2.8))]
    return {"pads": pads, "segs": segs, "vias": [], "copper": ["F.Cu", "B.Cu"], "zones": []}


def test_multi_terminal_pair_is_judged_per_path_and_spur():
    paths = PA.multi_paths(board(), "USB_DM", "USB_DP")
    assert paths is not None and len(paths["paths"]) == 2
    skews = sorted(p["skew"] for p in paths["paths"])
    assert skews[0] < 1e-6 and abs(skews[1] - 1.0) < 1e-6                 # row A matched, row B 1 mm off: the worst path is graded
    assert abs(paths["stub"] - 4.0) < 1e-6 and {s["at"] for s in paths["stubs"]} == {"J1.A7/A6", "J1.B7/B6"}


def test_two_terminal_pair_has_no_paths():
    B = board()
    B["pads"] = [q for q in B["pads"] if q["ref"] != "J1" or q["num"].startswith("A")]
    assert PA.multi_paths(B, "USB_DM", "USB_DP") is None


def test_target_warns_maximum_fails_and_spur_fails(capsys):
    base = {"pairs": {"USB_": {"skew_mm": 0.15, "skew_max_mm": 1.5, "stub_max_mm": 4.5}}}
    r = PA.audit(board(), [("USB_DM", "USB_DP")], base, set())[("USB_DM", "USB_DP")]
    assert r["status"] == "complete" and r["skew"] == 1.0 and r["target"] == 0.15 and r["limit"] == 1.5 and abs(r["over_target"] - 0.85) < 1e-6
    assert "PAIR_SKEW_OVER_TARGET USB_DM/USB_DP skew 1.000 mm, target 0.15 (+0.850 over), max 1.5: pass" in capsys.readouterr().err
    tight = {"pairs": {"USB_": {"skew_mm": 0.15, "skew_max_mm": 0.5}}}                   # past the maximum: fails; USB spur default 3.5: 4 > 3.5
    r = PA.audit(board(), [("USB_DM", "USB_DP")], tight, set())[("USB_DM", "USB_DP")]
    assert r["status"] == "mismatch" and set(r["causes"]) == {"skew", "stub"}


def test_no_maximum_means_maximum_is_the_target():
    cons = {"pairs": {"USB_": {"skew_mm": 0.15}}}
    assert C.skew_max_source(cons, "USB_DP")[0] == 0.15 == C.skew_limit_source(cons, "USB_DP")[0]
    assert C.skew_max_source({"pairs": {"USB_": {"skew_mm": 0.15, "skew_max_mm": 1.27}}}, "USB_DP") == (1.27, "[pairs.USB_] skew_max_mm")
    assert C.skew_max_source({"pairs": {"USB_": {"skew_mm": 0.15, "skew_max_mm": 0.05}}}, "USB_DP")[0] == 0.15      # never below the target
    ps = C.skew_max_source({"pairs": {"PCIE_": {"skew_ps": 10, "skew_max_ps": 20}}}, "PCIE_TX_P", ps_per_mm=7.0)[0]
    assert abs(ps - 20 / 7.0) < 1e-9


def test_stub_max_defaults_to_usb_and_is_configurable():
    assert C.stub_max_mm({}, "USB_DP") == 3.5 and C.stub_max_mm({}, "ETH_L0_P") is None and C.stub_max_mm({}, "USB3_RX_P") == 3.5
    assert C.stub_max_mm({"pairs": {"ETH_": {"stub_max_mm": 2.0}}}, "ETH_L0_P") == 2.0
    assert C.stub_max_mm({"si": {"stub_max_mm_by_kind": {"pcie": 1.0}}}, "PCIE_TX_P") == 1.0


def test_over_target_reaches_the_event_stream(tmp_path, monkeypatch):
    import json
    ev = tmp_path / "events.jsonl"
    monkeypatch.setenv("FLUXPLACE_EVENTS", str(ev))
    PA._NOTED.clear()
    cons = {"pairs": {"USB_": {"skew_mm": 0.15, "skew_max_mm": 1.5, "stub_max_mm": 4.5}}}
    PA.audit(board(), [("USB_DM", "USB_DP")], cons, set())
    rows = [json.loads(x) for x in ev.read_text().splitlines()]
    assert rows[-1]["kind"] == "pair_skew_over_target" and rows[-1]["verdict"] == "pass" and rows[-1]["over_mm"] == 0.85
