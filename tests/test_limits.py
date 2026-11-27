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
    assert lvl == "WARN" and "sourced hard limit 1.27" in txt and "http" in txt
    row = json.loads(open(tmp_path / DC.LOG).read().splitlines()[-1])
    assert row["choice"] == "FAIL->WARN" and row["evidence"]


def test_over_the_hard_limit_stays_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    lvl, txt = LM.regrade("pair_skew", "usb3", "P/N", 0.3, 0.1, "mm", 0.1, False, {}, None)
    assert lvl == "FAIL" and "0.127" in txt


def test_project_explicit_maximum_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    assert LM.regrade("pair_skew", "eth", "P/N", 1.0, 0.5, "mm", 0.15, True, {}, None) is None


def test_unknown_rule_without_research_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path))
    assert LM.regrade("pair_skew", "can", "P/N", 1.0, 0.5, "mm", 0.5, False, {}, None) is None


def test_research_with_sources_adds_a_hard_limit_and_regrades(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    ans = {"hard": 3.0, "unit": "mm", "confidence": "medium", "function": "link breaks at 3 mm",
           "sources": [{"url": "https://example.com/ds.pdf", "page": "p4", "quote": "skew must stay below 3 mm"}], "unverified": []}
    r = LM.regrade("pair_skew", "can", "P/N", 1.0, 0.5, "mm", 0.5, False, stub(ans), str(tmp_path))
    assert r[0] == "WARN" and "example.com" in r[1]
    assert LM.get("pair_skew", "can")["hard"] == 3.0                      # shared cache, next board needs no call
    ids = [json.loads(l)["id"] for l in open(tmp_path / DC.LOG)]
    assert "limit-research:pair_skew:can" in ids and any(i.startswith("limit:pair_skew:can") for i in ids)


def test_unsourced_answer_cannot_loosen_a_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("FLUXPLACE_LIMITS_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("FLUXPLACE_AI_OFFLINE", raising=False)
    ans = {"hard": 50.0, "unit": "mm", "function": "trust me", "sources": [], "unverified": ["from memory"]}
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
    prof = {"min_track_mm": 0.1, "_house": LM.fab_bounds({"vendor": "pcbway", "hard": {}}, 6)}
    objs = [("draw", None, ("C", 0.088), [(1.0, 2.0), (3.0, 2.0)]), ("draw", None, ("C", 0.2), [(1.0, 5.0), (3.0, 5.0)])]
    f = []
    GC._check_copper(objs, "F_Cu", dict(prof, min_clearance_mm=0.1), (0, 0, 5, 6), 0.05, f, lambda m: None)
    w = [x for x in f if x["code"] == "GERBER_MIN_WIDTH"]
    assert len(w) == 1 and w[0]["level"] == "FAIL" and "pcbway.com" in w[0]["msg"]
