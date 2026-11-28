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


def seg(net, a, b):
    return {"net": net, "layer": "F.Cu", "a": a, "b": b, "w": 0.15}


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
        # the N leg's 0.5 mm detour sits clear of the pads (a detour over a pad body is joined at the pad, as KiCad does: 52d0e26)
        for net, a, b in [("USB_DP", (9, 0), (9, 2)), ("USB_DP", (9, 2), (10, 2)), ("USB_DM", (9, 0.8), (9, 1.6)),
                          ("USB_DM", (9, 1.6), (8.75, 1.6)), ("USB_DM", (8.75, 1.6), (8.75, 2.8)), ("USB_DM", (8.75, 2.8), (10, 2.8))]:
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


def test_review_fails_an_unmeasured_or_disagreeing_gated_pair(tmp_path, monkeypatch):
    from fluxplace import review as RV, qualaudit as QA
    b = PB.write(tmp_path, "")
    rows = [{"pair": "USB_DP/USB_DM", "p": "USB_DP", "n": "USB_DM", "status": "unknown", "causes": [], "why": "MEASURE_DISAGREE: p +0.2 mm",
             "code": "MEASURE_DISAGREE", "skew": None, "limit": None}]
    monkeypatch.setattr(QA, "pair_rows", lambda *a, **k: rows)
    out = RV._pairs_from_audit({"board_path": b}, {"pairs": {"USB_": {}}})
    assert [(f["level"], f["code"]) for f in out if f["code"] == "MEASURE_DISAGREE"] == [("FAIL", "MEASURE_DISAGREE")]



def _stack_file(tmp_path, via_lines):
    f = tmp_path / "s.kicad_pcb"
    f.write_text('(kicad_pcb\n\t(setup\n\t\t(stackup\n\t\t\t(layer "F.Cu" (type "copper") (thickness 0.035))\n'
                 '\t\t\t(layer "dielectric 1" (type "core") (thickness 1.5))\n\t\t\t(layer "B.Cu" (type "copper") (thickness 0.035))\n\t\t)\n\t)\n'
                 + via_lines + ")\n")
    return str(f)


def test_via_stub_is_reported_and_a_straight_through_via_is_removed(tmp_path):
    vias = ('\t(via\n\t\t(at 10 5)\n\t\t(size 0.46)\n\t\t(drill 0.2)\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(net "USB_DP")\n\t)\n'
            '\t(via\n\t\t(at 20 5)\n\t\t(size 0.46)\n\t\t(drill 0.2)\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(net "USB_DP")\n\t)\n')
    f = _stack_file(tmp_path, vias)
    B = {"file": f, "copper": ["F.Cu", "B.Cu"], "pads": [],
         "vias": [{"net": "USB_DP", "x": 10, "y": 5, "d": 0.46, "layers": {"F.Cu", "B.Cu"}},
                  {"net": "USB_DP", "x": 20, "y": 5, "d": 0.46, "layers": {"F.Cu", "B.Cu"}}],
         "segs": [seg("USB_DP", (0, 5), (10, 5)), seg("USB_DP", (10, 5), (15, 5)),                  # F.Cu in and out of the via at x=10: straight through
                  seg("USB_DP", (15, 5), (20, 5)), dict(seg("USB_DP", (20, 5), (30, 5)), layer="B.Cu")]}   # the via at x=20 changes layer: no stub
    vs = PA.via_stubs(B, ["USB_DP"])
    assert len(vs) == 1 and vs[0]["x"] == 10 and vs[0]["used"] == ["F.Cu"] and vs[0]["dead"] and abs(vs[0]["stub_mm"] - 1.535) < 1e-6
    text, dead = PA.strip_dead_vias(open(f).read(), B, ["USB_DP"])
    assert len(dead) == 1 and "(at 10 5)" not in text and "(at 20 5)" in text


def _tap_board(tmp_path, spur_p=3.0, spur_n=4.0, ox=30.0, oy=30.0):
    """Endpoints QA1 -> ZK2 (20 mm, both legs equal); a protection-like part ZQ9 (no J/P/X/U prefix, one pad per leg) hangs off the line
    by a spur of spur_p / spur_n mm.  No ref prefix or part kind says which part is the tap: the topology does."""
    body = PB.fp("QA1", ox, oy, [PB.pad("1", 0, 0, "E_P"), PB.pad("2", 0, 2, "E_N")]) + PB.fp("ZK2", ox, oy, [PB.pad("1", 20, 0, "E_P"), PB.pad("2", 20, 2, "E_N")])
    body += PB.fp("ZQ9", ox, oy, [PB.pad("1", 10, -spur_p, "E_P"), PB.pad("2", 10, 2 + spur_n, "E_N")])
    for net, y, sp, sgn in (("E_P", 0, spur_p, -1), ("E_N", 2, spur_n, 1)):
        body += _seg(net, (ox, oy + y), (ox + 10, oy + y)) + _seg(net, (ox + 10, oy + y), (ox + 20, oy + y)) + \
            _seg(net, (ox + 10, oy + y), (ox + 10, oy + y + sgn * sp))
    return RD.load(PB.write(tmp_path, body))


@needs_cli
def test_a_tap_is_a_stub_never_a_path_endpoint(tmp_path):
    r = netlen.pair_skew(_tap_board(tmp_path), "E_P", "E_N", {})
    assert r["status"] == "ok" and r["basis"] == "path" and set(r["ends"]) == {"QA1", "ZK2"}
    assert [(p["a"][:3], p["b"][:3]) for p in r["paths"]] == [("QA1", "ZK2")] and r["skew_mm"] < 0.05       # only the end-to-end path is graded
    assert [t["at"].split(".")[0] for t in r["taps"]] == ["ZQ9"] and 2.5 < r["taps"][0]["mm"] < 4.5       # the spur is the tap's stub
    assert r["stub_mm"] == r["taps"][0]["mm"]


@needs_cli
def test_orientations_do_not_depend_on_net_order(tmp_path):
    B = board(tmp_path)
    a = netlen.pair_skew(B, "USB_DM", "USB_DP", {})
    b = netlen.pair_skew(B, "USB_DP", "USB_DM", {})
    assert a["status"] == b["status"] == "ok" and a["ends"] == b["ends"]
    key = lambda r: sorted((q["a"].split(".")[0], q["b"].split(".")[0], round(abs(q["d"]), 3)) for q in r["paths"])          # noqa: E731
    assert key(a) == key(b) and abs(a["skew_mm"]) == pytest.approx(abs(b["skew_mm"]), abs=1e-3)
    assert [round(x["mm"], 2) for x in a["spurs"]] == [round(x["mm"], 2) for x in b["spurs"]] or sorted(x["mm"] for x in a["spurs"]) == sorted(x["mm"] for x in b["spurs"])


def test_review_with_a_board_file_never_falls_back_to_own_length_math(tmp_path, monkeypatch):
    """review 5: an audit that raises is one FAIL PAIR_UNMEASURED, not the facts-length walk; PAIR_DELAY_UNKNOWN is a FAIL per pair and
    does not hide the other pairs' FAILs."""
    from fluxplace import review as RV, qualaudit as QA, si as SI
    b = PB.write(tmp_path, "")
    monkeypatch.setattr(SI, "pair_skew_findings", lambda *a, **k: (_ for _ in ()).throw(AssertionError("own math used")))
    facts = {"board_path": b, "net_pads": {}, "net_tracks": {}}
    def boom(*a, **k):
        raise RuntimeError("audit died")
    monkeypatch.setattr(QA, "pair_rows", boom)
    out = RV.check_pairs(facts, {"pairs": {"USB_": {}}})
    assert [(f["level"], f["code"]) for f in out] == [("FAIL", "PAIR_UNMEASURED")]
    rows = [{"pair": "USB_DP/USB_DM", "p": "USB_DP", "n": "USB_DM", "status": "unknown", "causes": [], "why": "PAIR_DELAY_UNKNOWN (no plane); pair not graded",
             "code": "PAIR_DELAY_UNKNOWN", "skew": None, "limit": None},
            {"pair": "USB_HUB_DP/USB_HUB_DM", "p": "USB_HUB_DP", "n": "USB_HUB_DM", "status": "unknown", "causes": [], "why": "MEASURE_DISAGREE: p +0.2 mm",
             "code": "MEASURE_DISAGREE", "skew": None, "limit": None}]
    monkeypatch.setattr(QA, "pair_rows", lambda *a, **k: rows)
    got = sorted((f["level"], f["code"]) for f in RV.check_pairs(facts, {"pairs": {"USB_": {}}}))
    assert got == [("FAIL", "MEASURE_DISAGREE"), ("FAIL", "PAIR_DELAY_UNKNOWN")]


def test_a_wide_connector_mates_its_pads_with_terminal_max_mm_and_tune_needs_a_stackup_delay(tmp_path):
    """review 9: no unexplained 16 mm limit, no built-in 6 ps/mm."""
    from fluxplace import tune as TU
    assert netlen._pad_max({}, "X_P") == 16.0
    assert netlen._pad_max({"pairs": {"X_": {"terminal_max_mm": 40}}}, "X_P") == 40.0
    B = board(tmp_path)
    m = TU.measure_pair(B["file"], B, "USB_DM", "USB_DP", {"pairs": {"USB_": {"skew_ps": 1.0}}})       # a ps budget on a board with no stackup
    assert not m["ok"] and "PAIR_DELAY_UNKNOWN" in m["why"]
