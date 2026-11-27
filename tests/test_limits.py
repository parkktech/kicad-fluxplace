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
