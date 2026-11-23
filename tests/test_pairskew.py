"""D71cv: skew target vs maximum, per-path skew and spur length on a pair with more than two terminals, KiCad as length authority."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fluxplace import constraints as C, pairaudit as PA


import shutil

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planboards as PB
from fluxplace import netlen, routediag as RD

needs_cli = pytest.mark.skipif(not shutil.which("kicad-cli"), reason="kicad-cli not installed")


def _seg(net, a, b, layer="F.Cu"):
    return f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.15) (layer "{layer}") (net "{net}"))\n'


def board(tmp_path, two_terminal=False):
    """U1 (one pad per net) -> junction -> two rows J1.A (short spur) and J1.B (the N leg takes a 1 mm detour): a REAL board file,
    measured by kicad-cli (D71cx).  two_terminal drops row B."""
    rows = [PB.pad("A6", 10, 0, "USB_DP"), PB.pad("A7", 10, 0.8, "USB_DM")]
    if not two_terminal:
        rows += [PB.pad("B6", 10, 2, "USB_DP"), PB.pad("B7", 10, 2.8, "USB_DM")]
    body = PB.fp("U1", 0, 0, [PB.pad("1", 0, 0, "USB_DP"), PB.pad("2", 0, 0.8, "USB_DM")]) + PB.fp("J1", 0, 0, rows)
    for net, a, b in [("USB_DP", (0, 0), (9, 0)), ("USB_DP", (9, 0), (10, 0)), ("USB_DM", (0, 0.8), (9, 0.8)), ("USB_DM", (9, 0.8), (10, 0.8))]:
        body += _seg(net, a, b)
    if not two_terminal:
        for net, a, b in [("USB_DP", (9, 0), (9, 2)), ("USB_DP", (9, 2), (10, 2)), ("USB_DM", (9, 0.8), (9, 2.8)),
                          ("USB_DM", (9, 2.8), (10.5, 2.8)), ("USB_DM", (10.5, 2.8), (10, 2.8))]:
            body += _seg(net, a, b)
    return RD.load(PB.write(tmp_path, body))


@needs_cli
def test_multi_terminal_pair_is_judged_per_path_and_spur(tmp_path):
    r = netlen.pair_skew(board(tmp_path), "USB_DM", "USB_DP", {})
    assert r["status"] == "ok" and r["basis"] == "path" and len(r["paths"]) == 2          # joined: found by topology, not by a ref prefix
    skews = sorted(p["skew"] for p in r["paths"])
    assert skews[0] < 1e-6 and 0.4 < skews[1] < 0.6                    # row A matched; row B off by the detour (pad edge to pad edge): the worst is graded
    assert abs(r["skew_mm"]) == skews[1] and 3.0 < r["stub_mm"] < 3.5
    assert abs(r["recon"]["p"]) <= netlen.RECON_TOL_MM and abs(r["recon"]["n"]) <= netlen.RECON_TOL_MM     # own decomposition == KiCad totals


@needs_cli
def test_two_terminal_pair_is_kicads_number_with_no_paths_of_its_own(tmp_path):
    r = netlen.pair_skew(board(tmp_path, two_terminal=True), "USB_DM", "USB_DP", {})
    assert r["basis"] == "kicad" and r["status"] == "ok" and abs(r["skew_mm"] - (r["p_mm"] - r["n_mm"])) < 1e-6 and r["stub_mm"] == 0.0


def test_kicad_cli_missing_is_unmeasured_and_cannot_pass(tmp_path, monkeypatch):
    B = board(tmp_path)
    monkeypatch.setattr(netlen, "pair_lengths", lambda *a, **k: None)
    r = netlen.pair_skew(B, "USB_DM", "USB_DP", {})
    assert r["status"] == "unmeasured" and r["skew_mm"] is None
    a = PA.audit(B, [("USB_DM", "USB_DP")], {"pairs": {"USB_": {"skew_mm": 9.0}}}, set())[("USB_DM", "USB_DP")]
    assert a["status"] == "unknown" and a["code"] == "PAIR_UNMEASURED" and a["skew"] is None      # a huge limit never rescues it: no own-math fallback


@needs_cli
def test_identity_miss_is_measure_disagree_and_ungraded(tmp_path, monkeypatch):
    B = board(tmp_path)
    real = netlen.pair_lengths
    monkeypatch.setattr(netlen, "pair_lengths", lambda *a, **k: {k_: dict(v, p=v["p"] + 0.2) for k_, v in real(*a, **k).items()})
    r = netlen.pair_skew(B, "USB_DM", "USB_DP", {})
    assert r["status"] == "MEASURE_DISAGREE" and "p " in r["why"]
    a = PA.audit(B, [("USB_DM", "USB_DP")], {"pairs": {"USB_": {"skew_mm": 9.0}}}, set())[("USB_DM", "USB_DP")]
    assert a["status"] == "unknown" and a["code"] == "MEASURE_DISAGREE"


@needs_cli
def test_target_warns_maximum_fails_and_spur_fails(tmp_path, capsys):
    B = board(tmp_path)
    base = {"pairs": {"USB_": {"skew_mm": 0.15, "skew_max_mm": 1.5, "stub_max_mm": 4.5}}}
    r = PA.audit(B, [("USB_DM", "USB_DP")], base, set())[("USB_DM", "USB_DP")]
    assert r["status"] == "complete" and 0.4 < r["skew"] < 0.6 and r["target"] == 0.15 and r["limit"] == 1.5 and abs(r["over_target"] - (r["skew"] - 0.15)) < 1e-3
    assert "PAIR_SKEW_OVER_TARGET USB_DM/USB_DP skew" in capsys.readouterr().err
    tight = {"pairs": {"USB_": {"skew_mm": 0.15, "skew_max_mm": 0.4, "stub_max_mm": 3.0}}}          # past the maximum: fails; the 3.26 mm spur > 3.0
    r = PA.audit(B, [("USB_DM", "USB_DP")], tight, set())[("USB_DM", "USB_DP")]
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


@needs_cli
def test_over_target_reaches_the_event_stream(tmp_path, monkeypatch):
    import json
    ev = tmp_path / "events.jsonl"
    monkeypatch.setenv("FLUXPLACE_EVENTS", str(ev))
    PA._NOTED.clear()
    cons = {"pairs": {"USB_": {"skew_mm": 0.15, "skew_max_mm": 1.5, "stub_max_mm": 4.5}}}
    PA.audit(board(tmp_path), [("USB_DM", "USB_DP")], cons, set())
    rows = [json.loads(x) for x in ev.read_text().splitlines()]
    assert rows[-1]["kind"] == "pair_skew_over_target" and rows[-1]["verdict"] == "pass" and rows[-1]["unit"] == "mm" and 0.3 < rows[-1]["over"] < 0.4
