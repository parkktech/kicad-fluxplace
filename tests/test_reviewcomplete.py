"""w76: review completes on cached facts; an UNCHECKED row names its reason (budget / missing dependency), never a bare failure."""
import json
import time

from fluxplace import lcsc as L, sourcing as S, review as R

ROW = {"lcsc": "C1", "stock": 500, "exact": True}


def test_cache_is_shared_across_board_folders(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(L, "lookup", lambda m, timeout=30: calls.append(m) or dict(ROW))
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    assert L.check(["X1"], cache_path=str(a / ".lcsc_cache.json"), log=lambda m: None)["X1"]["grade"] == "CN_OK"
    assert L.check(["X1"], cache_path=str(b / ".lcsc_cache.json"), log=lambda m: None)["X1"]["grade"] == "CN_OK"
    assert calls == ["X1"]                                  # the second folder reused the first one's fact


def test_budget_spent_part_is_unchecked_with_reason_and_retried_next_run(tmp_path, monkeypatch):
    seen = []

    def fake(m, timeout=30):
        seen.append(m)
        time.sleep(0.02)
        return dict(ROW)
    monkeypatch.setattr(L, "lookup", fake)
    cp = str(tmp_path / ".lcsc_cache.json")
    r = L.check(["A", "B", "C"], cache_path=cp, budget=0.01, log=lambda m: None)
    assert r["A"]["grade"] == "CN_OK" and r["C"]["grade"] == "ERR" and r["C"]["why"] == "lookup budget spent"
    r = L.check(["A", "B", "C"], cache_path=cp, budget=0, log=lambda m: None)
    assert {x["grade"] for x in r.values()} == {"CN_OK"}
    assert seen.count("A") == 1                              # the first run's answer was kept


def test_missing_curl_cffi_stops_at_once_and_says_so(tmp_path, monkeypatch):
    n = []

    def boom(m, timeout=30):
        n.append(m)
        raise ImportError("curl_cffi")
    monkeypatch.setattr(L, "lookup", boom)
    r = L.check(["A", "B", "C"], cache_path=str(tmp_path / ".lcsc_cache.json"), log=lambda m: None)
    assert len(n) == 1 and all("curl_cffi is not installed" in x["why"] for x in r.values())


def test_transport_failure_is_remembered_briefly(tmp_path, monkeypatch):
    n = []

    def boom(m, timeout=30):
        n.append(m)
        raise RuntimeError("JLCPCB HTTP 403")
    monkeypatch.setattr(L, "lookup", boom)
    cp = str(tmp_path / ".lcsc_cache.json")
    L.check(["A"], cache_path=cp, log=lambda m: None)
    r = L.check(["A"], cache_path=cp, log=lambda m: None)
    assert len(n) == 1 and "403" in r["A"]["why"]


def test_cn_unchecked_message_carries_the_reason(monkeypatch):
    monkeypatch.setattr(L, "check", lambda *a, **k: {"M": {"grade": "ERR", "row": None, "why": "lookup budget spent"}})
    f = R.check_china({"mpn_map": {"U1": "M"}}, {"sourcing": {"china": True}})
    assert f[0]["code"] == "CN_UNCHECKED" and "lookup budget spent" in f[0]["msg"]


def test_shared_cache_newest_row_wins(tmp_path):
    d = str(tmp_path / "d")
    S.cache_save("x.json", d, {"M": {"_t": 5, "v": 1}})
    S.cache_save("x.json", d, {"M": {"_t": 3, "v": 0}, "N": {"_t": 1}})
    got = S.cache_load("x.json", d)
    assert got["M"]["v"] == 1 and "N" in got
