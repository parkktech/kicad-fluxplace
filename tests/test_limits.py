"""w110-limitresearch: limits registry + limit-challenge.  The model is a stub command; no network."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fluxplace import limits as LM, decider as DC


def stub(answer):
    code = "import sys; sys.stdin.read(); print(sys.argv[1])"
    return {"limits": {"research": "on"}, "ai": {"decide_cmd": [sys.executable, "-c", code, json.dumps(answer)]}}


def test_seed_has_sourced_hard_values_for_the_four_pair_interfaces(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    for i in ("eth", "usb2", "usb3", "pcie"):
        e = LM.get("pair_skew", i)
        assert LM.sourced(e) and e["hard"] >= e["target"], i


def test_between_target_and_hard_is_a_warn_with_the_citation_and_is_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    lvl, txt = LM.regrade("pair_skew", "eth", "P/N", 1.0, 0.15, "mm", 0.15, False, {}, str(tmp_path))
    assert lvl == "WARN" and "sourced hard limit 112.7" in txt and "http" in txt
    row = json.loads(open(tmp_path / DC.LOG).read().splitlines()[-1])
    assert row["choice"] == "FAIL->WARN" and row["evidence"]


def test_over_the_hard_limit_stays_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    # D71fz (1688af9): USB3 / PCIe hard = 10 % UI = 2.8 mm (physics); the 5 mil vendor figure is guidance, over it is a WARN
    lvl, txt = LM.regrade("pair_skew", "usb3", "P/N", 3.0, 0.1, "mm", 0.1, False, {}, None)
    assert lvl == "FAIL" and "2.8" in txt
    lvl, txt = LM.regrade("pair_skew", "usb3", "P/N", 0.3, 0.1, "mm", 0.1, False, {}, None)
    assert lvl == "WARN" and "0.127" in txt and "2.8" in txt
    assert LM.regrade("pair_skew", "eth", "P/N", 150.0, 1.0, "mm", 0.15, False, {}, None)[0] == "FAIL"


def test_eth_over_guidance_inside_physics_hard_is_warn_with_emc_note(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    lvl, txt = LM.regrade("pair_skew", "eth", "L0", 24.97, 1.0, "mm", 0.15, False, {}, str(tmp_path))
    assert lvl == "WARN" and "vendor guidance 0.51-1.27" in txt and "EMC" in txt and "physics" in txt
    e = LM.get("pair_skew", "eth")
    assert e["physics"]["fraction"] == 0.10 and e["owner"]["date"] == "2026-11-27" and 112 < e["hard"] < 113


def test_guidance_gate_is_labelled_and_not_downgraded(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    # no seed entry is a guidance gate since D71fz (pcie is physics, see above); the basis itself still may not be downgraded
    e = LM.get("pair_skew", "pcie")
    assert e["basis"] == "physics" and e["hard"] == 2.8
    monkeypatch.setattr(LM, "get", lambda rule, iface: dict(e, basis="guidance-gate", hard=0.127))
    assert LM.regrade("pair_skew", "pcie", "P/N", 0.2, 0.1, "mm", 0.1, False, {}, None)[0] == "FAIL"


def test_research_hard_without_spec_or_physics_basis_is_guidance_only(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    ans = {"hard": 2.0, "basis": "none", "sources": [{"url": "https://x.example/a", "page": "p1", "quote": "within 2 mm"}]}
    assert LM.regrade("pair_skew", "can", "P/N", 3.0, 1.0, "mm", 1.0, False, stub(ans), None) is None
    e = LM.get("pair_skew", "can")
    assert e["hard"] is None and e["guidance"] == [2.0, 2.0]


def test_project_explicit_maximum_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    assert LM.regrade("pair_skew", "eth", "P/N", 1.0, 0.5, "mm", 0.15, True, {}, None) is None


def test_unknown_rule_without_research_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    assert LM.regrade("pair_skew", "can", "P/N", 1.0, 0.5, "mm", 0.5, False, {}, None) is None


def test_research_with_sources_adds_a_hard_limit_and_regrades(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    ans = {"hard": 3.0, "unit": "mm", "basis": "spec", "confidence": "medium", "function": "link breaks at 3 mm",
           "sources": [{"url": "https://example.com/ds.pdf", "page": "p4", "quote": "skew must stay below 3 mm"}], "unverified": []}
    r = LM.regrade("pair_skew", "can", "P/N", 1.0, 0.5, "mm", 0.5, False, stub(ans), str(tmp_path))
    assert r[0] == "WARN" and "example.com" in r[1]
    assert LM.get("pair_skew", "can")["hard"] == 3.0                      # shared cache, next board needs no call
    ids = [json.loads(l)["id"] for l in open(tmp_path / DC.LOG)]
    assert "limit-research:pair_skew:can" in ids and any(i.startswith("limit:pair_skew:can") for i in ids)


def test_unsourced_answer_cannot_loosen_a_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    ans = {"hard": 50.0, "unit": "mm", "basis": "spec", "function": "trust me", "sources": [], "unverified": ["from memory"]}
    assert LM.regrade("pair_skew", "can", "P/N", 1.0, 0.5, "mm", 0.5, False, stub(ans), None) is None
    e = LM.get("pair_skew", "can")
    assert e["hard"] is None and any("unsourced hard value" in u for u in e["unverified"])


def test_unsourced_cache_entry_never_replaces_a_sourced_seed(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    (tmp_path / "limits.json").write_text(json.dumps({"pair_skew:eth": {"hard": None, "unverified": ["x"]}}))
    assert LM.sourced(LM.get("pair_skew", "eth"))


def test_fab_rows_are_seeded_for_both_houses_with_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    for house in ("jlcpcb", "pcbway"):
        for f in LM.FAB_FEATURES:
            e = LM.get("fab_" + f, f"{house}-6l")
            assert e and e["direction"] == "min" and e["date"] and (e["hard"] is None or LM.sourced(e)), (house, f)


def test_grade_min_below_hard_fail_between_warn_above_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    assert LM.grade_min("fab_via_annular_min", "pcbway-6l", 0.1)[0] == "FAIL"
    assert LM.grade_min("fab_via_annular_min", "pcbway-6l", 0.14)[0] == "WARN"
    assert LM.grade_min("fab_via_annular_min", "pcbway-6l", 0.16)[0] is None
    assert LM.grade_min("fab_nothing", "x-6l", 1)[0] == "NONE"


def test_research_fab_drops_values_without_a_quote_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    ans = {"items": [{"feature": "track_min", "hard": 0.08, "recommended": 0.1, "url": "https://h.example/cap", "page": "row 3", "quote": "min trace 0.08"},
                     {"feature": "space_min", "hard": 0.05, "recommended": None, "url": "", "quote": ""}]}
    cons = {"ai": {"decide_cmd": [sys.executable, "-c", "import sys; sys.stdin.read(); print(sys.argv[1])", json.dumps(ans)]}}
    r = LM.research_fab("h", 6, cons, str(tmp_path))
    assert r["track_min"]["hard"] == 0.08 and r["space_min"]["hard"] is None and r["space_min"]["unverified"]
    assert LM.get("fab_track_min", "h-6l")["hard"] == 0.08


def test_gerbercheck_grades_tracks_against_the_house_row(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    from fluxplace import gerbercheck as GC
    prof = {"min_track_mm": 0.1, "_house": {"pcbway": LM.fab_bounds("pcbway", 6)}}
    objs = [("draw", None, ("C", 0.088), [(1.0, 2.0), (3.0, 2.0)]), ("draw", None, ("C", 0.2), [(1.0, 5.0), (3.0, 5.0)])]
    f = []
    GC._check_copper(objs, "F_Cu", dict(prof, min_clearance_mm=0.1), (0, 0, 5, 6), 0.05, f, lambda m: None)
    w = [x for x in f if x["code"] == "GERBER_MIN_WIDTH"]
    assert len(w) == 1 and w[0]["level"] == "FAIL" and "pcbway.com" in w[0]["msg"]


def test_no_house_configured_grades_the_strictest_row_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    h, note = LM.fab_houses({}, 6, {})
    assert list(h) == ["strictest"] and "strictest registry row" in note
    assert h["strictest"]["track_min"][0] == 0.1 and h["strictest"]["via_annular_min"][0] == 0.127
    h2, note2 = LM.fab_houses({}, 6, {"fab": {"houses": ["jlcpcb", "pcbway"]}})
    assert sorted(h2) == ["jlcpcb", "pcbway"] and not note2 and h2["jlcpcb"]["track_min"][0] == 0.09


def test_gerbercheck_one_verdict_line_per_house(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    from fluxplace import gerbercheck as GC
    res = {"status": "FAIL", "seconds": 0, "resolution_mm": 0.02, "copper_layers": 6, "holes": 0, "counts": {}, "findings": [],
           "houses": {"jlcpcb": "PASS", "pcbway": "FAIL"}}
    t = GC.format_report(res)
    assert "house jlcpcb: PASS" in t and "house pcbway: FAIL" in t


def test_fabcaps_rows_read_the_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    from fluxplace import fabcaps as FC
    lim, unv = FC.row_for("jlcpcb", "standard", 6)
    assert lim["via_annular_min"] == 0.125 and lim["track_min"] == 0.09          # design floor = recommended, not the 0.05 reject
    lim8, unv8 = FC.row_for("jlcpcb", "standard", 8)                       # no registry row at 8 layers: JSON number, flagged
    assert any("not in the limits registry" in u for u in unv8)


def test_design_floors_use_recommended_never_the_reject_value(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    from fluxplace import fabcaps as FC
    assert FC.limits("jlcpcb-standard", 6)["floors"]["min_annular_mm"] == 0.125          # JLC 6L via ring, 0.45 pad on a 0.2 drill
    assert FC.limits("pcbway-standard", 6)["floors"]["min_annular_mm"] == 0.15
    assert LM.get("fab_via_annular_min", "jlcpcb-6l")["hard"] == 0.05                    # the reject value stays for grading


W16 = {"fab": {"profile": ["pcbway-standard", "jlcpcb-standard"]}}


def test_project_via_floor_at_or_above_hard_wins_with_a_warn(tmp_path, monkeypatch):           # D71fs
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    from fluxplace import constraints as C
    fp = C.fab_profile(dict(W16, route={"min_via_mm": 0.46}), layers=6)
    assert fp["min_via_dia_mm"] == 0.46                                  # not lifted to the recommended 0.5 (0.2 drill + 2 x 0.15)
    assert any("min_via_dia_mm" in w and "0.46" in w and "0.5" in w and "0.454" in w for w in fp["floor_warns"])
    assert C.fab_profile(W16, layers=6)["min_via_dia_mm"] == 0.5         # no project value: the recommended floor


def test_project_floor_below_hard_is_lifted_to_hard(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    from fluxplace import constraints as C
    fp = C.fab_profile(dict(W16, route={"min_via_mm": 0.40, "min_track_mm": 0.05}), layers=6)
    assert fp["min_via_dia_mm"] == 0.454 and fp["min_track_mm"] == 0.1    # hard values: never below
    assert len(fp["floor_warns"]) == 2


def test_review_lists_the_floor_warn(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    from fluxplace import review as RV
    out = RV.check_fab_capability(dict(copper_layers=6, track_geom=[]), dict(W16, route={"min_via_mm": 0.46}))
    assert any(f["code"] == "FAB_FLOOR_BELOW_RECOMMENDED" and f["level"] == "WARN" for f in out)


def _pair_facts(stub):
    return {"board_path": None}


def test_tap_stub_info_fires_over_10_mm_not_at_5_9(monkeypatch):
    from fluxplace import review as RV, qualaudit as QA
    rows = [{"p": "ETH_P0_P", "n": "ETH_P0_N", "status": "complete", "causes": [], "stubs": [{"path": "a>b", "mm": 11.7}]},
            {"p": "ETH_P1_P", "n": "ETH_P1_N", "status": "complete", "causes": [], "stubs": [{"path": "a>b", "mm": 5.9}]}]
    out = _audit_with(rows, monkeypatch)
    tap = [f for f in out if f["code"] == "PAIR_TAP_STUB"]
    assert len(tap) == 1 and tap[0]["level"] == "INFO" and "ETH_P0_P" in tap[0]["msg"] and "11.7 mm" in tap[0]["msg"]


def test_skew_finding_carries_the_advisory_whole_net_note(monkeypatch):
    row = {"p": "ETH_P0_P", "n": "ETH_P0_N", "status": "mismatch", "causes": ["skew"], "stubs": [], "skew": 12.96, "limit": 1.0, "target": 1.0,
           "unit": "mm", "basis": "path", "net_mm": (63.7, 61.7), "why": "skew"}
    out = _audit_with([row], monkeypatch)
    f = [x for x in out if x["code"].startswith("PAIR_SKEW")]
    assert len(f) == 1 and "advisory, whole-net KiCad length" in f[0]["msg"] and "diff +2.00" in f[0]["msg"]
    row2 = dict(row, basis="kicad")
    assert "advisory" not in [x for x in _audit_with([row2], monkeypatch) if x["code"].startswith("PAIR_SKEW")][0]["msg"]


def _audit_with(rows, monkeypatch):
    """review._pairs_from_audit with the audit rows stubbed: only the finding assembly is under test."""
    import tempfile
    from fluxplace import review as RV, qualaudit as QA, routediag as RD, routeconn as RC, netclass as NC, pairuncoupled as PU
    f = tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False).name
    monkeypatch.setattr(RD, "load", lambda p: {})
    monkeypatch.setattr(NC, "classify", lambda B, c: {})
    monkeypatch.setattr(RC, "open_by_net", lambda p: [])
    monkeypatch.setattr(QA, "pair_rows", lambda *a, **k: rows)
    monkeypatch.setattr(PU, "rows", lambda *a, **k: [])
    monkeypatch.setattr(PU, "flagged", lambda r: [])
    monkeypatch.setattr(RV, "pair_gated", lambda cons, m: True)
    return RV._pairs_from_audit({"board_path": f}, {"limits": {"research": "off"}})
